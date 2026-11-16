#!/usr/bin/env python3

"""
Tests for :mod:`regshape.cli.blob`.

Exercises blob head, get, delete, upload, and mount subcommands through the
Click test runner, with domain functions patched at the CLI import path.
"""

import io
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import click
import pytest
import requests
from click.testing import CliRunner

from regshape.cli.downloads import DownloadWarnings
from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.blobs import DownloadStats, DownloadStatus
from regshape.libs.errors import AuthError, BlobError
from regshape.libs.models.blob import BlobInfo
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - minimum Click fixture
from regshape.tests.test_artifact_pull_cli import ArtifactConsole, _console_streams

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
NAMESPACE = "myrepo/myimage"
REPO = f"{REGISTRY}/{NAMESPACE}"
DIGEST = "sha256:" + "a" * 64
CONTENT_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
SIZE = 4_194_304

_BLOB_INFO = BlobInfo(digest=DIGEST, content_type=CONTENT_TYPE, size=SIZE)


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "observer"])
@pytest.mark.parametrize("no_progress", [False, True])
def test_changed_etag_warning_follows_result_or_error(
    runner: CliRunner, outcome: str, no_progress: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defer one fixed warning beyond cleanup/output, even with no progress and NO_COLOR."""
    monkeypatch.setenv("NO_COLOR", "1")
    primary = RuntimeError("synthetic observer fault")
    status = DownloadStatus(
        "blob", DIGEST, "fallback", "full", "etag_changed", None, 2,
        DownloadStats(), scratch_root="https://invalid.example/private?token=synthetic",
    )
    order: list[str] = []
    original_close = ProgressDisplay.__exit__
    original_message = DownloadWarnings._message

    def download(
        *, download_status_callback: Callable[[DownloadStatus], None], **kwargs: Any,
    ) -> BlobInfo:
        """Deliver repeated fallback/final reasons without implementing the separate range engine."""
        download_status_callback(status)
        download_status_callback(status)
        assert order == []
        order.append("workers-cleaned")
        if outcome == "failure":
            raise BlobError("synthetic download failure")
        if outcome == "cancel":
            raise KeyboardInterrupt
        if outcome == "observer":
            raise primary
        download_status_callback(DownloadStatus(
            "blob", DIGEST, "completed", "full", "etag_changed", None, 2, DownloadStats(),
        ))
        return _BLOB_INFO

    def close(display: ProgressDisplay, *args: Any) -> bool:
        """Record the real display cleanup completing before deferred warning formatting."""
        result = original_close(display, *args)
        order.append("progress-closed")
        return result

    def message(warnings: DownloadWarnings, *, success: bool) -> str:
        """Require worker/display cleanup before generating the fixed final warning."""
        assert order == ["workers-cleaned", "progress-closed"]
        order.append("warning")
        return original_message(warnings, success=success)

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.cli.blob.get_blob", side_effect=download),
        patch.object(ProgressDisplay, "__exit__", autospec=True, side_effect=close),
        patch.object(DownloadWarnings, "_message", autospec=True, side_effect=message),
    ):
        result = runner.invoke(regshape, [
            "blob", "get", "--repo", REPO, "--digest", DIGEST,
            *(["--no-progress"] if no_progress else []),
        ])
    assert order == ["workers-cleaned", "progress-closed", "warning"]
    assert result.stderr.count("Warning: ETag changed") == 1
    assert "private" not in result.stderr and "\x1b" not in result.stderr
    if outcome == "success":
        assert result.exit_code == 0 and json.loads(result.stdout) == _BLOB_INFO.to_dict()
        assert result.stderr == (
            f"Warning: ETag changed during ranged download of {DIGEST}; restarted "
            "as a full-stream download and verified the digest.\n"
        )
    else:
        assert result.exit_code == 1 and not result.stdout
        assert "verified" not in result.stderr
        before = result.stderr.index("Warning:")
        assert ("Aborted!" if outcome == "cancel" else "Error") in result.stderr[:before]
        if outcome == "observer":
            assert result.exception is primary


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, EOFError])
def test_changed_etag_stdout_flush_failure_does_not_claim_verification(
    error_type: type[BaseException],
) -> None:
    """Descriptor-less output preserves flush failures without recovery claims or duplicate aborts."""
    output, errors = io.StringIO(), io.StringIO()
    primary = error_type("synthetic stdout flush failure")
    with (
        patch.object(output, "flush", side_effect=primary),
        patch("sys.stdout", SimpleNamespace(write=output.write, flush=output.flush)),
        patch("sys.stderr", errors),
        pytest.raises(OSError if error_type is OSError else click.exceptions.Exit) as caught,
        DownloadWarnings() as warnings,
    ):
        warnings.observe(DownloadStatus(
            "blob", DIGEST, "fallback", "full", "etag_changed", None, 2, DownloadStats(),
        ))
    if error_type is OSError:
        assert caught.value is primary
        assert errors.getvalue().startswith("Error: Download did not complete (OSError).\n")
    else:
        assert caught.value.exit_code == 1
        assert errors.getvalue().startswith("\nAborted!\n")
        assert errors.getvalue().count("Aborted!") == 1
    assert errors.getvalue().count("Warning: ETag changed") == 1
    assert "verified" not in errors.getvalue()


@pytest.mark.parametrize("boundary", ["write", "flush"])
def test_changed_etag_shared_result_sink_failure_is_not_retried(
    runner: CliRunner, boundary: str,
) -> None:
    """A failed final blob JSON writer cannot reuse an aliased stdout/stderr for its warning."""
    console = ArtifactConsole(shared=True, interactive=False)
    console.err = console.out
    primary = OSError("synthetic shared result sink failure")
    original_write, original_flush = console.out.write, console.out.flush
    failed = False
    after_failure: list[str] = []

    def download(
        *, download_status_callback: Callable[[DownloadStatus], None], **kwargs: Any,
    ) -> BlobInfo:
        """Return verified metadata after a validated fallback observation."""
        download_status_callback(DownloadStatus(
            "blob", DIGEST, "fallback", "full", "etag_changed", None, 2, DownloadStats(),
        ))
        return _BLOB_INFO

    def write(text: str) -> int:
        """Detect additional writes after the single result failure."""
        nonlocal failed
        if failed and text:
            after_failure.append(text)
        if boundary == "write" and '"digest"' in text and not failed:
            failed = True
            raise primary
        return original_write(text)

    def flush() -> None:
        """Fail once when Click flushes the completed JSON result."""
        nonlocal failed
        if boundary == "flush" and '"digest"' in console.out.text and not failed:
            failed = True
            raise primary
        original_flush()

    with (
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.cli.blob.get_blob", side_effect=download),
        patch.object(console.out, "write", side_effect=write),
        patch.object(console.out, "flush", side_effect=flush),
    ):
        result = runner.invoke(regshape, [
            "blob", "get", "--repo", REPO, "--digest", DIGEST, "--no-progress",
        ])
    assert failed and result.exception is primary
    assert not after_failure and "Warning:" not in console.err.text


@pytest.mark.parametrize("boundary", ["write", "flush"])
@pytest.mark.parametrize("shared", [False, True])
def test_changed_etag_result_failure_respects_distinct_stream_destinations(
    tmp_path: Path, boundary: str, shared: bool,
) -> None:
    """Do not retry a broken pipe through another descriptor, but retain healthy stderr."""
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb") as reader,
        os.fdopen(write_fd, "wb", buffering=0) as raw_output,
        io.TextIOWrapper(raw_output, write_through=boundary == "write") as output,
        (
            io.TextIOWrapper(
                os.fdopen(os.dup(write_fd), "wb", buffering=0), write_through=True,
            ) if shared else (tmp_path / "stderr.txt").open("w+", encoding="utf-8")
        ) as errors,
    ):
        reader.close()
        primary: OSError | None = None
        try:
            with (
                patch("sys.stdout", output),
                patch("sys.stderr", errors),
                patch.object(errors, "write", wraps=errors.write) as write,
                pytest.raises(OSError) as caught,
                DownloadWarnings() as warnings,
            ):
                warnings.observe(DownloadStatus(
                    "blob", DIGEST, "fallback", "full", "etag_changed", None, 2, DownloadStats(),
                ))
                try:
                    output.write("result")
                except OSError as error:
                    primary = error
                    warnings.result_failed()
                    raise
            if primary is not None:
                assert caught.value is primary
            writes = [call.args[0] for call in write.call_args_list if call.args[0]]
            if shared:
                assert not writes
                assert caught.value.__cause__ is None
            else:
                errors.seek(0)
                diagnostics = errors.read()
                assert diagnostics.startswith("Error: Download did not complete")
                assert diagnostics.count("Warning: ETag changed") == 1
                assert "verified" not in diagnostics
        finally:
            raw_output.close()


@pytest.mark.parametrize("mode", ["terminal", "no-progress", "structured", "nonterminal"])
@pytest.mark.parametrize("boundary", ["error-write", "error-flush", "telemetry"])
def test_changed_etag_post_display_stderr_failure_is_not_retried(
    runner: CliRunner, mode: str, boundary: str,
) -> None:
    """Track failed error/telemetry stderr after display close, regardless of animation mode."""
    console = ArtifactConsole(shared=False, interactive=mode != "nonterminal")
    primary = OSError("synthetic post-display stderr failure")
    original_write, original_flush = console.err.write, console.err.flush
    armed = failed = False
    after_failure: list[str] = []
    marker = "scenario" if boundary == "telemetry" else "Error ["

    def download(
        *, download_status_callback: Callable[[DownloadStatus], None], **kwargs: Any,
    ) -> BlobInfo:
        """Supply a validated fallback reason before command-owned reporting or scenario cleanup."""
        download_status_callback(DownloadStatus(
            "blob", DIGEST, "fallback", "full", "etag_changed", None, 2, DownloadStats(),
        ))
        if boundary != "telemetry":
            raise BlobError("synthetic operation failure")
        return _BLOB_INFO

    def write(text: str) -> int:
        """Fail once at the post-display boundary and detect any attempted warning/error reuse."""
        nonlocal armed, failed
        if failed and text:
            after_failure.append(text)
        if marker in text and not failed:
            assert console.displays and not console.displays[0]._active
            armed = True
            if boundary != "error-flush":
                failed = True
                raise primary
        return original_write(text)

    def flush() -> None:
        """Inject a real error-report flush fault without failing earlier progress cleanup."""
        nonlocal failed
        if boundary == "error-flush" and armed and not failed:
            failed = True
            raise primary
        original_flush()

    options = [
        *(["--time-scenarios"] if boundary == "telemetry" else []),
        *(["--no-progress"] if mode == "no-progress" else []),
        *(["--telemetry-format", "json"] if mode == "structured" else []),
    ]
    with (
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.cli.blob.get_blob", side_effect=download),
        patch("regshape.cli.blob.ProgressDisplay", side_effect=console.artifact_display),
        patch.object(console.err, "write", side_effect=write),
        patch.object(console.err, "flush", side_effect=flush),
    ):
        result = runner.invoke(regshape, [
            "blob", "get", "--repo", REPO, "--digest", DIGEST, *options,
        ])
    assert failed and result.exception is primary
    assert not after_failure and not console.out.text
    assert "Warning:" not in console.err.text
    assert not console.err.closed and not console.out.closed
    assert console.displays[0].config.output is console.err


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "click"])
@pytest.mark.parametrize("log_route", ["handler", "last-resort"])
def test_changed_etag_warning_sink_failure_preserves_the_primary(
    outcome: str, log_route: str,
) -> None:
    """Never retry a failed warning/diagnostic sink or replace an existing operation failure."""
    output, errors = io.StringIO(), io.StringIO()
    primary = (
        KeyboardInterrupt() if outcome == "cancel"
        else click.ClickException("synthetic download failure") if outcome == "click"
        else RuntimeError("synthetic download failure")
    )
    sink_failure = OSError("synthetic warning sink failure")
    handler = logging.StreamHandler(errors)
    expected = (
        click.exceptions.Exit if outcome in ("cancel", "click")
        else RuntimeError if outcome == "failure" else OSError
    )
    with (
        patch("sys.stdout", output),
        patch("sys.stderr", errors),
        patch.object(logging.getLogger(), "handlers", [handler] if log_route == "handler" else []),
        patch("logging.lastResort", handler),
        patch.object(errors, "write", side_effect=sink_failure) as write,
        pytest.raises(expected) as caught,
        DownloadWarnings() as warnings,
    ):
        warnings.observe(DownloadStatus(
            "blob", DIGEST, "fallback", "full", "etag_changed", None, 2, DownloadStats(),
        ))
        if outcome != "success":
            raise primary
    if outcome in ("cancel", "click"):
        assert caught.value.exit_code == 1
    else:
        assert caught.value is (primary if outcome == "failure" else sink_failure)
    if outcome != "success":
        assert caught.value.__cause__ is sink_failure
    assert sum(bool(call.args[0]) for call in write.call_args_list) == 1


def _runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# TestBlobHead
# ---------------------------------------------------------------------------


class TestBlobHead:

    def test_head_success(self):
        with patch("regshape.cli.blob.head_blob", return_value=_BLOB_INFO):
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["digest"] == DIGEST
        assert data["content_type"] == CONTENT_TYPE
        assert data["size"] == SIZE

    def test_head_short_flags(self):
        with patch("regshape.cli.blob.head_blob", return_value=_BLOB_INFO):
            result = _runner().invoke(
                regshape,
                ["blob", "head", "-r", REPO, "-d", DIGEST],
            )
        assert result.exit_code == 0, result.output

    def test_head_not_found_exits_1(self):
        with patch(
            "regshape.cli.blob.head_blob",
            side_effect=BlobError(f"Blob not found: {REGISTRY}/{NAMESPACE}@{DIGEST}", "HTTP 404"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_head_auth_error_exits_1(self):
        with patch(
            "regshape.cli.blob.head_blob",
            side_effect=AuthError(f"Authentication failed for {REGISTRY}", "HTTP 401"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_head_connection_error_exits_1(self):
        with patch(
            "regshape.cli.blob.head_blob",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_head_output_is_json(self):
        with patch("regshape.cli.blob.head_blob", return_value=_BLOB_INFO):
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 0
        # Must parse without error
        json.loads(result.output)

    def test_head_rejects_repo_with_tag(self):
        with patch("regshape.cli.blob.head_blob") as mock_head:
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", f"{REPO}:v1", "--digest", DIGEST],
            )
        assert result.exit_code == 1
        assert "plain" in result.output
        mock_head.assert_not_called()

    def test_head_rejects_repo_with_latest_tag(self):
        with patch("regshape.cli.blob.head_blob") as mock_head:
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", f"{REPO}:latest", "--digest", DIGEST],
            )
        assert result.exit_code == 1
        assert "plain" in result.output
        mock_head.assert_not_called()

    def test_head_rejects_repo_with_digest(self):
        with patch("regshape.cli.blob.head_blob") as mock_head:
            result = _runner().invoke(
                regshape,
                ["blob", "head", "--repo", f"{REPO}@{DIGEST}", "--digest", DIGEST],
            )
        assert result.exit_code == 1
        assert "plain" in result.output
        mock_head.assert_not_called()


# ---------------------------------------------------------------------------
# TestBlobGet
# ---------------------------------------------------------------------------


class TestBlobGet:

    def test_get_no_output_success(self):
        with patch("regshape.cli.blob.get_blob", return_value=_BLOB_INFO) as mock_get:
            result = _runner().invoke(
                regshape,
                ["blob", "get", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["digest"] == DIGEST
        # output_path must be None when --output is not given
        assert mock_get.call_args.kwargs["output_path"] is None

    def test_get_with_output_passes_path(self, tmp_path):
        output_file = str(tmp_path / "layer.tar.gz")
        with patch("regshape.cli.blob.get_blob", return_value=_BLOB_INFO) as mock_get:
            result = _runner().invoke(
                regshape,
                ["blob", "get", "--repo", REPO, "--digest", DIGEST, "--output", output_file],
            )
        assert result.exit_code == 0, result.output
        # Check that the domain function was called with the output path
        assert mock_get.call_args.kwargs["output_path"] == output_file

    def test_get_not_found_exits_1(self):
        with patch(
            "regshape.cli.blob.get_blob",
            side_effect=BlobError("Blob not found", "HTTP 404"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "get", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_get_digest_mismatch_exits_1(self):
        with patch(
            "regshape.cli.blob.get_blob",
            side_effect=BlobError(f"Digest mismatch: expected {DIGEST}", ""),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "get", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_get_connection_error_exits_1(self):
        with patch(
            "regshape.cli.blob.get_blob",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "get", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_get_chunk_size_respected(self):
        with patch("regshape.cli.blob.get_blob", return_value=_BLOB_INFO) as mock_get:
            _runner().invoke(
                regshape,
                ["blob", "get", "--repo", REPO, "--digest", DIGEST,
                 "--chunk-size", "131072"],
            )
        assert mock_get.call_args.kwargs["chunk_size"] == 131072


# ---------------------------------------------------------------------------
# TestBlobDelete
# ---------------------------------------------------------------------------


class TestBlobDelete:

    def test_delete_success(self):
        with patch("regshape.cli.blob.delete_blob", return_value=None):
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["digest"] == DIGEST
        assert data["status"] == "deleted"

    def test_delete_not_found_exits_1(self):
        with patch(
            "regshape.cli.blob.delete_blob",
            side_effect=BlobError("Blob not found", "HTTP 404"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_delete_not_supported_exits_1(self):
        with patch(
            "regshape.cli.blob.delete_blob",
            side_effect=BlobError("Operation not supported by this registry", "HTTP 405"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_delete_auth_error_exits_1(self):
        with patch(
            "regshape.cli.blob.delete_blob",
            side_effect=AuthError(f"Authentication failed for {REGISTRY}", "HTTP 401"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_delete_output_is_json(self):
        with patch("regshape.cli.blob.delete_blob", return_value=None):
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", REPO, "--digest", DIGEST],
            )
        assert result.exit_code == 0
        json.loads(result.output)

    def test_delete_rejects_repo_with_tag(self):
        with patch("regshape.cli.blob.delete_blob") as mock_delete:
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", f"{REPO}:v1", "--digest", DIGEST],
            )
        assert result.exit_code == 1
        assert "plain" in result.output
        mock_delete.assert_not_called()

    def test_delete_rejects_repo_with_latest_tag(self):
        with patch("regshape.cli.blob.delete_blob") as mock_delete:
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", f"{REPO}:latest", "--digest", DIGEST],
            )
        assert result.exit_code == 1
        assert "plain" in result.output
        mock_delete.assert_not_called()

    def test_delete_rejects_repo_with_digest(self):
        with patch("regshape.cli.blob.delete_blob") as mock_delete:
            result = _runner().invoke(
                regshape,
                ["blob", "delete", "--repo", f"{REPO}@{DIGEST}", "--digest", DIGEST],
            )
        assert result.exit_code == 1
        assert "plain" in result.output
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# TestBlobUpload
# ---------------------------------------------------------------------------


class TestBlobUpload:

    def test_upload_monolithic_success(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"test blob content")

        with patch("regshape.cli.blob.upload_blob_stream", return_value=DIGEST):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "upload",
                    "--repo", REPO,
                    "--file", str(test_file),
                    "--digest", DIGEST,
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["digest"] == DIGEST
        assert "location" in data

    def test_upload_monolithic_location_format(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"content")

        with patch("regshape.cli.blob.upload_blob_stream", return_value=DIGEST):
            result = _runner().invoke(
                regshape,
                ["blob", "upload", "--repo", REPO, "--file", str(test_file), "--digest", DIGEST],
            )
        data = json.loads(result.output)
        assert data["location"].startswith(f"/v2/{NAMESPACE}/blobs/")

    def test_upload_chunked_success(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"chunked test content")

        with patch("regshape.cli.blob.upload_blob_chunked", return_value=DIGEST):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "upload",
                    "--repo", REPO,
                    "--file", str(test_file),
                    "--digest", DIGEST,
                    "--chunked",
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["digest"] == DIGEST

    def test_upload_chunked_does_not_call_monolithic(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"content")

        with patch("regshape.cli.blob.upload_blob_stream") as mock_mono, \
             patch("regshape.cli.blob.upload_blob_chunked", return_value=DIGEST):
            _runner().invoke(
                regshape,
                ["blob", "upload", "--repo", REPO, "--file", str(test_file),
                 "--digest", DIGEST, "--chunked"],
            )
        mock_mono.assert_not_called()

    def test_upload_monolithic_does_not_call_chunked(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"content")

        with patch("regshape.cli.blob.upload_blob_stream", return_value=DIGEST), \
             patch("regshape.cli.blob.upload_blob_chunked") as mock_chunked:
            _runner().invoke(
                regshape,
                ["blob", "upload", "--repo", REPO, "--file", str(test_file), "--digest", DIGEST],
            )
        mock_chunked.assert_not_called()

    def test_upload_file_not_found_exits_2(self):
        # Click's Path(exists=True) raises UsageError (exit code 2) before the
        # command body runs when the file does not exist.
        result = _runner().invoke(
            regshape,
            ["blob", "upload", "--repo", REPO, "--file", "/nonexistent", "--digest", DIGEST],
        )
        assert result.exit_code == 2

    def test_upload_error_exits_1(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"content")

        with patch(
            "regshape.cli.blob.upload_blob_stream",
            side_effect=BlobError("Upload session not found: abc", "HTTP 404"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "upload", "--repo", REPO, "--file", str(test_file), "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_upload_auth_error_exits_1(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"content")

        with patch(
            "regshape.cli.blob.upload_blob_stream",
            side_effect=AuthError(f"Authentication failed for {REGISTRY}", "HTTP 401"),
        ):
            result = _runner().invoke(
                regshape,
                ["blob", "upload", "--repo", REPO, "--file", str(test_file), "--digest", DIGEST],
            )
        assert result.exit_code == 1

    def test_upload_media_type_passed_to_domain(self, tmp_path):
        test_file = tmp_path / "layer.tar.gz"
        test_file.write_bytes(b"content")
        custom_type = "application/vnd.oci.image.layer.v1.tar+gzip"

        with patch("regshape.cli.blob.upload_blob_stream", return_value=DIGEST) as mock_upload:
            _runner().invoke(
                regshape,
                [
                    "blob", "upload",
                    "--repo", REPO,
                    "--file", str(test_file),
                    "--digest", DIGEST,
                    "--media-type", custom_type,
                ],
            )
        args, kwargs = mock_upload.call_args
        assert kwargs.get("content_type") == custom_type


# ---------------------------------------------------------------------------
# TestBlobMount
# ---------------------------------------------------------------------------


class TestBlobMount:

    FROM_REPO = "sourcerepo/myimage"

    def test_mount_success(self):
        with patch("regshape.cli.blob.mount_blob", return_value=DIGEST):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "mount",
                    "--repo", REPO,
                    "--digest", DIGEST,
                    "--from-repo", self.FROM_REPO,
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["digest"] == DIGEST
        assert data["status"] == "mounted"
        assert f"/v2/{NAMESPACE}/blobs/" in data["location"]

    def test_mount_not_accepted_exits_1(self):
        with patch(
            "regshape.cli.blob.mount_blob",
            side_effect=BlobError(
                f"Blob mount not accepted for {REGISTRY}/{NAMESPACE}@{DIGEST}"
                ": registry returned 202 — retry with upload_blob or upload_blob_chunked",
                f"from_repo={self.FROM_REPO}",
            ),
        ):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "mount",
                    "--repo", REPO,
                    "--digest", DIGEST,
                    "--from-repo", self.FROM_REPO,
                ],
            )
        assert result.exit_code == 1

    def test_mount_auth_error_exits_1(self):
        with patch(
            "regshape.cli.blob.mount_blob",
            side_effect=AuthError(f"Authentication failed for {REGISTRY}", "HTTP 401"),
        ):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "mount",
                    "--repo", REPO,
                    "--digest", DIGEST,
                    "--from-repo", self.FROM_REPO,
                ],
            )
        assert result.exit_code == 1

    def test_mount_connection_error_exits_1(self):
        with patch(
            "regshape.cli.blob.mount_blob",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "mount",
                    "--repo", REPO,
                    "--digest", DIGEST,
                    "--from-repo", self.FROM_REPO,
                ],
            )
        assert result.exit_code == 1

    def test_mount_output_is_json(self):
        with patch("regshape.cli.blob.mount_blob", return_value=DIGEST):
            result = _runner().invoke(
                regshape,
                [
                    "blob", "mount",
                    "--repo", REPO,
                    "--digest", DIGEST,
                    "--from-repo", self.FROM_REPO,
                ],
            )
        assert result.exit_code == 0
        json.loads(result.output)

    def test_mount_from_repo_required(self):
        result = _runner().invoke(
            regshape,
            ["blob", "mount", "--repo", REPO, "--digest", DIGEST],
        )
        assert result.exit_code != 0

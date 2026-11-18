"""Screen-frame and command wiring regressions for shared transfer progress."""

import errno
import hashlib
import io
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

import click
import pytest
import requests
from click.testing import CliRunner

from regshape.cli.blob import _trusted_file_size
from regshape.cli.downloads import DownloadWarnings
from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.artifacts.models import ArtifactProgress
from regshape.libs.blobs import BlobDownload, DownloadOptions, get_blob, get_blobs
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.decorators.output import telemetry_write
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import BlobError
from regshape.libs.progress import (
    ProgressCallback,
    TransferDirection,
    TransferPhase,
    TransferProgress,
    _TransferProgress,
)
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.tests.artifact_fixtures import StreamResponse
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - minimum Click fixture

DIGEST = "sha256:" + "a" * 64


class Terminal(io.StringIO):
    """Interpret carriage returns and erase-line controls as a real terminal would."""

    def __init__(self, interactive: bool = True, encoding: str = "utf-8") -> None:
        """Track visible rows and reject writes unsupported by the selected output encoding."""
        super().__init__()
        self.interactive = interactive
        self._encoding = encoding
        self._rows = [""]
        self.row = 0
        self.column = 0
        self.width = 160
        self.fail_writes = False

    @property
    def encoding(self) -> str:
        """Expose the actual codec used to validate terminal writes."""
        return self._encoding

    def isatty(self) -> bool:
        """Expose a controlled terminal detection result."""
        return self.interactive

    @property
    def rows(self) -> list[str]:
        """Return the visible frame through the cursor or the last nonempty row."""
        last = max([self.row] + [i for i, row in enumerate(self._rows) if row])
        return self._rows[:last + 1]

    def resize(self, width: int, active_rows: int = 1) -> None:
        """Reflow the selected active block on shrink, preserving preceding hard-newline output."""
        start = self.row - active_rows + 1
        chunks = [
            chunk for text in self._rows[start:self.row + 1]
            for chunk in ([text[i:i + width] for i in range(0, len(text), width)] or [""])
        ]
        self._rows[start:self.row + 1] = chunks
        self.row = start + len(chunks) - 1
        self.column = len(chunks[-1])
        self.width = width

    def write(self, text: str) -> int:
        """Apply cursor controls and zero-width styling, retaining raw output for color assertions."""
        if self.fail_writes:
            raise OSError("secondary render failure")
        text.encode(self.encoding)
        result = super().write(text)
        while text:
            style = re.match(r"\x1b\[[0-9;]*m", text)
            if style is not None:
                text = text[style.end():]
                continue
            if text.startswith("\x1b[2K"):
                self._rows[self.row] = ""
                text = text[4:]
                continue
            if text.startswith("\x1b[1A"):
                self.row = max(0, self.row - 1)
                text = text[4:]
                continue
            char, text = text[0], text[1:]
            if char == "\r":
                self.column = 0
            elif char == "\n":
                self.row += 1
                if self.row == len(self._rows):
                    self._rows.append("")
                self.column = 0
            else:
                if self.column == self.width:
                    self.row += 1
                    if self.row == len(self._rows):
                        self._rows.append("")
                    self.column = 0
                row = self._rows[self.row].ljust(self.column)
                self._rows[self.row] = row[:self.column] + char + row[self.column + 1:]
                self.column += 1
        return result


@pytest.fixture(autouse=True)
def telemetry_reset():
    """Keep global telemetry configuration from leaking between command invocations."""
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(TelemetryConfig())


@pytest.fixture
def upload_source(tmp_path: Path, source_kind: str) -> Iterator[Path]:
    """Provide a regular file or a bounded, EOF-terminated real pipe descriptor path."""
    if source_kind != "pipe":
        path = tmp_path / "source"
        path.write_bytes(b"" if source_kind == "empty" else b"abcdef")
        yield path
        return
    if not Path("/dev/fd").is_dir():
        pytest.skip("Descriptor paths are not available on this platform")
    reader, writer = os.pipe()
    try:
        try:
            os.write(writer, b"abcdef")
        finally:
            os.close(writer)
        yield Path(f"/dev/fd/{reader}")
    finally:
        os.close(reader)


def _event(
    phase: TransferPhase, count: int = 0, total: int | None = 100, *,
    attempt: int = 1, digest: str = DIGEST,
) -> TransferProgress:
    """Create a valid immutable observation for renderer-only tests."""
    return TransferProgress("invocation", digest, TransferDirection.UPLOAD, phase, count, total, attempt)


def test_state_changes_overwrite_one_row_and_clear_suffixes() -> None:
    """Verification and final states replace the transfer bar rather than appending lines."""
    terminal = Terminal()
    now = [0.0]
    with ProgressDisplay(stream=terminal, total_blobs=2, clock=lambda: now[0], columns=lambda: 160) as display:
        display.observe(_event(TransferPhase.TRANSFERRING))
        now[0] = 1
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        assert len([r for r in terminal.rows if r.startswith("Blob")]) == 1
        assert "remaining" in terminal.rows[-1]
        display.observe(_event(TransferPhase.VERIFYING, 100))
        assert "Verifying" in terminal.rows[-1]
        assert "remaining" not in terminal.rows[-1] and "[" not in terminal.rows[-1]
        display.observe(_event(TransferPhase.COMPLETED, 100))
        assert len([r for r in terminal.rows if r.startswith("Blob")]) == 1
        assert "Uploaded" in terminal.rows[-2]
        display.observe(_event(TransferPhase.SKIPPED, digest="sha256:" + "b" * 64))
        display.observe(_event(TransferPhase.SKIPPED, digest=DIGEST))
        assert display.completed == 1 and display.skipped == 1
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 2 and "Uploaded" in rows[0] and "skipped" in rows[1]


def test_retry_failure_and_cancellation_reuse_the_row() -> None:
    """Retry resets and failure do not produce duplicate rows or successful labels."""
    terminal = Terminal()
    with ProgressDisplay(stream=terminal, columns=lambda: 120) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        display.observe(_event(TransferPhase.RETRYING, attempt=2))
        assert "Retrying attempt 2" in terminal.rows[-1] and "40 B" not in terminal.rows[-1]
        display.observe(_event(TransferPhase.TRANSFERRING, 10, attempt=2))
        display.observe(_event(TransferPhase.FAILED, 10, attempt=2))
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 1 and "Failed" in rows[0] and "Uploaded" not in rows[0]
    terminal = Terminal()
    with pytest.raises(KeyboardInterrupt), ProgressDisplay(stream=terminal) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 10))
        raise KeyboardInterrupt()
    assert "Cancelled" in terminal.getvalue() and "Uploaded" not in terminal.getvalue()


def test_unknown_total_throttling_and_final_flush() -> None:
    """Unknown sizes omit percentage/ETA, routine redraws are bounded, finals always flush."""
    terminal = Terminal()
    now = [0.0]
    with ProgressDisplay(stream=terminal, clock=lambda: now[0], columns=lambda: 160) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, total=None))
        for count in range(1, 100):
            now[0] += 0.001
            display.observe(_event(TransferPhase.TRANSFERRING, count, None))
        assert terminal.getvalue().count("\x1b[2K") == 1
        display.observe(_event(TransferPhase.COMPLETED, 99, None))
    assert terminal.getvalue().count("\x1b[2K") == 2
    assert "%" not in terminal.getvalue() and "remaining" not in terminal.getvalue()
    assert "99 B" in terminal.rows[-2]


def test_narrow_and_resized_terminal_never_wraps_active_row() -> None:
    """Drop optional fields and erase a previous wide state when the terminal shrinks."""
    terminal = Terminal()
    width = [120]
    with ProgressDisplay(stream=terminal, columns=lambda: width[0]) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 20))
        width[0] = 24
        terminal.resize(24)
        display.observe(_event(TransferPhase.VERIFYING, 100))
        assert len([r for r in terminal.rows if r]) == 1
        assert len(terminal.rows[-1]) <= 23
        assert "Verifying" in terminal.rows[-1]
        display.observe(_event(TransferPhase.COMPLETED, 100))
    assert len(terminal.rows[-2]) <= 23


@pytest.mark.parametrize("width", [24, 39, 40, 41, 80])
def test_narrow_transfer_keeps_state_and_changing_byte_count(width: int) -> None:
    """Optional fields yield to state and current bytes on both sides of the old boundary."""
    terminal, now = Terminal(), [0.0]
    terminal.resize(width)
    with ProgressDisplay(stream=terminal, clock=lambda: now[0], columns=lambda: width) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 2, 6))
        assert "Uploading" in terminal.rows[-1] and "2 B" in terminal.rows[-1]
        now[0] = 1
        display.observe(_event(TransferPhase.TRANSFERRING, 4, 6))
        assert "Uploading" in terminal.rows[-1] and "4 B" in terminal.rows[-1]
        assert len(terminal.rows[-1]) < width and len(terminal.rows) == 1
        display.observe(_event(TransferPhase.COMPLETED, 6, 6))


@pytest.mark.parametrize("columns", [0, -1])
def test_uninitialized_terminal_size_uses_a_positive_fallback(
    columns: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing terminal size must not be interpreted as a one-column reflow footprint."""
    terminal = Terminal()
    monkeypatch.setenv("COLUMNS", "80")
    with (
        patch.object(terminal, "fileno", return_value=2),
        patch("os.get_terminal_size", return_value=os.terminal_size((columns, 24))),
        ProgressDisplay(stream=terminal) as display,
    ):
        assert display.columns() == 80
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        display.observe(_event(TransferPhase.COMPLETED, 100))
    assert "\x1b[1A" not in terminal.getvalue()
    assert len([r for r in terminal.rows if r.startswith("Blob")]) == 1


@pytest.mark.parametrize("lines", [0, -1])
def test_uninitialized_terminal_height_keeps_interleaved_rows_visible(
    lines: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uninitialized height uses the fallback viewport rather than hiding all but one row."""
    terminal = Terminal()
    other = "sha256:" + "b" * 64
    monkeypatch.setenv("COLUMNS", "160")
    monkeypatch.setenv("LINES", "4")
    with (
        patch.object(terminal, "fileno", return_value=2),
        patch("os.get_terminal_size", return_value=os.terminal_size((160, lines))),
        ProgressDisplay(stream=terminal) as display,
    ):
        display.observe(_event(TransferPhase.TRANSFERRING, 20))
        display.observe(_event(TransferPhase.TRANSFERRING, 40, digest=other))
        assert len([row for row in terminal.rows if row.startswith("Blob")]) == 2
        display.observe(_event(TransferPhase.COMPLETED, 100, digest=other))
        display.observe(_event(TransferPhase.COMPLETED, 100))


def test_download_telemetry_does_not_flush_a_closed_borrowed_sink(tmp_path: Path) -> None:
    """A retained telemetry proxy can close after its borrowed stderr without flushing it."""
    with (tmp_path / "stderr.txt").open("w", encoding="utf-8") as terminal:
        with (
            patch.object(terminal, "isatty", return_value=True),
            patch("sys.stderr", terminal),
            DownloadWarnings() as warnings,
        ):
            assert warnings._output is not None
            config = TelemetryConfig(output=warnings._output)
            configure_telemetry(config)
            with ProgressDisplay():
                proxy = config.output
        terminal.write("borrowed stderr remains open\n")
    with patch.object(terminal, "flush", wraps=terminal.flush) as flush:
        proxy.close()
    flush.assert_not_called()


@pytest.mark.parametrize("reason", ["disabled", "structured", "nonterminal", "json-telemetry"])
def test_suppressed_progress_writes_no_controls_or_state_lines(reason: str) -> None:
    """Disabled/noninteractive/structured paths keep their original result channels clean."""
    terminal = Terminal(interactive=reason != "nonterminal")
    configure_telemetry(TelemetryConfig(output=terminal, output_format="json" if reason == "json-telemetry" else "text"))
    with ProgressDisplay(stream=terminal, disabled=reason == "disabled", structured=reason == "structured") as display:
        display.prepare("Preparing")
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        display.observe(_event(TransferPhase.VERIFYING, 100))
        display.observe(_event(TransferPhase.COMPLETED, 100))
    assert terminal.getvalue() == ""


def test_text_telemetry_is_written_without_duplicate_progress_or_log_controls() -> None:
    """Print-style partial writes clear/redraw one row while the log receives only messages."""
    terminal, logfile = Terminal(), io.StringIO()
    config = TelemetryConfig(output=terminal, log_file=logfile)
    configure_telemetry(config)
    with ProgressDisplay(stream=terminal, columns=lambda: 120) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        telemetry_write("GET registry path", config.output, config.log_file)
        assert terminal.rows[-2] == "GET registry path"
        assert sum(r.startswith("Blob") for r in terminal.rows) == 1
        display.observe(_event(TransferPhase.COMPLETED, 100))
    assert config.output is terminal
    assert logfile.getvalue() == "GET registry path\n"
    assert sum(r.startswith("Blob") for r in terminal.rows) == 1


def test_render_cleanup_preserves_original_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A second terminal-write failure cannot replace the original operation error."""
    terminal = Terminal()
    original = RuntimeError("original transfer")
    with pytest.raises(RuntimeError) as caught, ProgressDisplay(stream=terminal) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 10))
        terminal.fail_writes = True
        raise original
    assert caught.value is original
    assert "preserving original error" in caplog.text


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_failed_success_render_is_not_redrawn_as_success(error_type: type[BaseException]) -> None:
    """Transient output failure/cancellation during completion retains only a failed final row."""
    terminal = Terminal()
    original_write = terminal.write
    original = error_type("during completion")
    failed = False

    def write(text: str) -> int:
        """Fail once on the final label, then allow exception cleanup to render."""
        nonlocal failed
        if "Uploaded" in text and not failed:
            failed = True
            raise original
        return original_write(text)

    with (
        pytest.raises(error_type) as caught,
        patch.object(terminal, "write", side_effect=write),
        ProgressDisplay(stream=terminal) as display,
    ):
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        display.observe(_event(TransferPhase.COMPLETED, 100))
    assert caught.value is original
    assert display.completed == 0
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 1 and "Uploaded" not in rows[0]
    assert ("Failed" if error_type is OSError else "Cancelled") in rows[0]


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("direction", list(TransferDirection))
def test_final_newline_flush_failure_overwrites_original_row(
    error_type: type[BaseException], direction: TransferDirection,
) -> None:
    """Failure after a written final newline must repaint the preceding blob row."""
    terminal = Terminal()
    original_flush = terminal.flush
    original = error_type("after final newline")
    failed = False

    def flush() -> None:
        """Interrupt once only after the terminal cursor has advanced past the success row."""
        nonlocal failed
        if not failed and terminal.getvalue().endswith("\n"):
            failed = True
            raise original
        original_flush()

    with (
        pytest.raises(error_type) as caught,
        patch.object(terminal, "flush", side_effect=flush),
        ProgressDisplay(stream=terminal) as display,
    ):
        display.observe(replace(_event(TransferPhase.TRANSFERRING, 40), direction=direction))
        display.observe(replace(_event(TransferPhase.COMPLETED, 100), direction=direction))
    assert caught.value is original and display.completed == 0
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 1
    assert "Uploaded" not in rows[0] and "Downloaded" not in rows[0]
    assert ("Failed" if error_type is OSError else "Cancelled") in rows[0]


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("failure_point", ["label", "label-flush", "newline-flush"])
def test_nested_upload_telemetry_cannot_finalize_failed_success(
    runner: CliRunner, tmp_path: Path, chunked: bool, error_type: type[BaseException], failure_point: str,
) -> None:
    """Real command/library scenario finalizers must not redraw a failed completion as success."""
    data = b"abcdef"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    path = tmp_path / "source"
    path.write_bytes(data)
    terminal = Terminal()
    original_write, original_flush = terminal.write, terminal.flush
    original = error_type(2 if error_type is SystemExit else "during completion")
    failed = False

    def write(text: str) -> int:
        """Fail once when the success label is about to be written."""
        nonlocal failed
        if failure_point == "label" and not failed and "Uploaded" in text:
            failed = True
            raise original
        return original_write(text)

    def flush() -> None:
        """Fail once after the success label or its newline, never on earlier telemetry."""
        nonlocal failed
        has_success = any("Uploaded" in row for row in terminal.rows)
        after_newline = terminal.getvalue().endswith("\n")
        fail_here = (failure_point == "label-flush" and not after_newline) or (
            failure_point == "newline-flush" and after_newline
        )
        if not failed and has_success and fail_here:
            failed = True
            raise original
        original_flush()

    def display(*, disabled: bool) -> ProgressDisplay:
        """Coordinate real command telemetry with the same interactive destination."""
        get_telemetry_config().output = terminal
        return ProgressDisplay(stream=terminal, columns=lambda: 160, disabled=disabled)

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Accept both real upload protocols and consume the monolithic counted source."""
        response = requests.Response()
        response.status_code = 201 if method == "PUT" else 202
        response.headers["Location"] = "/v2/repo/blobs/uploads/session"
        response._content = b""
        response._content_consumed = True
        if method == "PUT":
            if not chunked:
                body = kwargs["data"]
                assert isinstance(body, io.RawIOBase)
                while body.read(2):
                    pass
            response.headers["Docker-Content-Digest"] = digest
        return response

    args = ["blob", "upload", "--repo", "registry.example.invalid/repo", "--file", str(path),
            "--digest", digest, "--time-scenarios"]
    if chunked:
        args.append("--chunked")
    with (
        patch.object(terminal, "write", side_effect=write),
        patch.object(terminal, "flush", side_effect=flush),
        patch("regshape.cli.blob.ProgressDisplay", side_effect=display),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
    ):
        result = runner.invoke(regshape, args)
    assert result.exit_code != 0 and result.stdout == "" and failed
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 1 and "Uploaded" not in rows[0]
    assert ("Failed" if error_type is OSError else "Cancelled") in rows[0]
    assert "scenario" in terminal.getvalue()


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("diagnostic_failure", ["clear-write", "clear-flush", "explicit-flush"])
@pytest.mark.parametrize("stderr_logging", [False, True])
def test_secondary_nested_telemetry_failure_preserves_primary_and_previous_rows(
    error_type: type[BaseException], diagnostic_failure: str,
    stderr_logging: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    """Distinct diagnostic errors cannot replace a callback error or erase a prior blob row."""
    terminal = Terminal()
    handler = logging.StreamHandler(terminal)
    logger = logging.getLogger("regshape.cli.progress")
    config = TelemetryConfig(output=terminal, time_scenarios_enabled=True)
    configure_telemetry(config)
    original_write, original_flush = terminal.write, terminal.flush
    primary = error_type("primary callback")
    secondary = RuntimeError("secondary diagnostic")
    primary_failed = secondary_failed = False

    def write(text: str) -> int:
        """Optionally fail before the diagnostic's cursor-clear write."""
        nonlocal secondary_failed
        if primary_failed and not secondary_failed and diagnostic_failure == "clear-write" and "\x1b[2K" in text:
            secondary_failed = True
            raise secondary
        return original_write(text)

    def flush() -> None:
        """Fail after the target newline, then optionally after a diagnostic cursor move."""
        nonlocal primary_failed, secondary_failed
        if not primary_failed and terminal.getvalue().endswith("\n") and any(
            "Blob 2" in row and "Uploaded" in row for row in terminal.rows
        ):
            primary_failed = True
            raise primary
        if primary_failed and not secondary_failed and diagnostic_failure == "clear-flush" and terminal.getvalue().endswith("\x1b[2K"):
            secondary_failed = True
            raise secondary
        if primary_failed and not secondary_failed and diagnostic_failure == "explicit-flush":
            secondary_failed = True
            raise secondary
        original_flush()

    @track_scenario("nested upload")
    def upload(display: ProgressDisplay) -> None:
        """Run the actual scenario finalizer while the original callback exception unwinds."""
        try:
            display.observe(_event(TransferPhase.TRANSFERRING, 40))
            display.observe(_event(TransferPhase.COMPLETED, 100))
        finally:
            if diagnostic_failure == "explicit-flush":
                config.output.flush()

    with (
        pytest.raises(error_type) as caught,
        patch.object(terminal, "write", side_effect=write),
        patch.object(terminal, "flush", side_effect=flush),
        patch.object(logger, "handlers", [handler] if stderr_logging else []),
        ProgressDisplay(stream=terminal, total_blobs=2) as display,
    ):
        display.observe(_event(TransferPhase.COMPLETED, 100, digest="sha256:" + "b" * 64))
        upload(display)
    assert caught.value is primary and secondary_failed
    assert "Telemetry output failed (RuntimeError); preserving original progress error" in caplog.text
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 2 and "Blob 1" in rows[0] and "Uploaded" in rows[0]
    assert "Blob 2" in rows[1] and "Uploaded" not in rows[1]
    assert ("Failed" if error_type is OSError else "Cancelled") in rows[1]
    assert display.completed == 1 and config.output is terminal
    if stderr_logging:
        assert terminal.rows[-2].startswith("Telemetry output failed")


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_diagnostic_error_without_pending_failure_propagates(operation: str) -> None:
    """Best-effort output is restricted to an already-failing progress callback."""
    terminal = Terminal()
    config = TelemetryConfig(output=terminal)
    configure_telemetry(config)
    error = OSError("ordinary diagnostic error")
    with (
        pytest.raises(OSError) as caught,
        patch.object(terminal, operation, side_effect=error),
        ProgressDisplay(stream=terminal),
    ):
        if operation == "write":
            config.output.write("diagnostic")
        else:
            config.output.flush()
    assert caught.value is error and config.output is terminal


@pytest.mark.parametrize("timing", [False, True])
@pytest.mark.parametrize("fallback_logging", [False, True])
@pytest.mark.parametrize("error_type", [requests.exceptions.ConnectionError, BlobError])
def test_transfer_and_rendering_errors_coordinate_library_logging(
    timing: bool, fallback_logging: bool, error_type: type[Exception],
) -> None:
    """The real library failure tracker must not log outside the active progress cursor."""
    terminal = Terminal()
    config = TelemetryConfig(output=terminal, time_scenarios_enabled=timing)
    configure_telemetry(config)
    handler = logging.StreamHandler(terminal)
    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    original_flush = terminal.flush
    primary = error_type("primary transfer error")
    failed = False

    def flush() -> None:
        """Fail once after the target failure row's newline, while a transfer error unwinds."""
        nonlocal failed
        if not failed and terminal.getvalue().endswith("\n") and any(
            "Blob 2" in row and "Failed" in row for row in terminal.rows
        ):
            failed = True
            raise OSError("secondary renderer error")
        original_flush()

    @track_scenario("nested transfer")
    def transfer(display: ProgressDisplay) -> None:
        """Invoke the actual library callback-failure logger under a primary transfer error."""
        with _TransferProgress(DIGEST, TransferDirection.UPLOAD, display.observe, 100) as progress:
            progress.advance(40)
            raise primary

    tracker_logger = logging.getLogger("regshape.libs.progress")
    original_fallback = logging.lastResort
    with (
        pytest.raises(error_type) as caught,
        patch.object(terminal, "flush", side_effect=flush),
        patch.object(logging.getLogger(), "handlers", [] if fallback_logging else [handler, log_handler]),
        patch.object(tracker_logger, "handlers", []),
        patch("sys.stderr", terminal),
        patch("logging.lastResort", original_fallback if fallback_logging else None),
        ProgressDisplay(stream=terminal, total_blobs=2) as display,
    ):
        display.observe(_event(TransferPhase.COMPLETED, 100, digest="sha256:" + "b" * 64))
        transfer(display)
    assert caught.value is primary and failed
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 2 and "Uploaded" in rows[0] and "Failed" in rows[1]
    assert "Progress observer failed during failure notification (OSError)" in terminal.getvalue()
    assert display.completed == 1 and handler.stream is terminal and config.output is terminal
    assert display._last_resort_proxy is None
    if not fallback_logging:
        assert "Progress observer failed" in log_output.getvalue()
        assert "\x1b" not in log_output.getvalue() and log_handler.stream is log_output


def test_fallback_logging_format_and_terminator_are_preserved() -> None:
    """Coordinating the standard fallback handler keeps its configured text contract."""
    terminal = Terminal()
    fallback = logging.StreamHandler(terminal)
    fallback.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    fallback.terminator = " [end]\n"
    logger = logging.getLogger("regshape.tests.progress")
    with (
        patch.object(logging.getLogger(), "handlers", []),
        patch.object(logger, "handlers", []),
        patch("logging.lastResort", fallback),
    ):
        with ProgressDisplay(stream=terminal) as display:
            display.observe(_event(TransferPhase.TRANSFERRING, 40))
            logger.warning("diagnostic")
            display.observe(_event(TransferPhase.COMPLETED, 100))
        assert logging.lastResort is fallback
    assert terminal.rows[0] == "WARNING: diagnostic [end]"
    assert len([r for r in terminal.rows if r.startswith("Blob")]) == 1


def test_terminal_event_during_partial_diagnostic_is_not_lost() -> None:
    """A lifecycle event or interrupt between print writes still flushes one final row."""
    terminal = Terminal()
    config = TelemetryConfig(output=terminal)
    configure_telemetry(config)
    with ProgressDisplay(stream=terminal) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        config.output.write("partial diagnostic")
        display.observe(_event(TransferPhase.COMPLETED, 100))
        config.output.write("\n")
    assert terminal.rows[0] == "partial diagnostic"
    assert len([r for r in terminal.rows if r.startswith("Blob")]) == 1
    assert "Uploaded" in terminal.rows[1]


def test_retry_resets_rate_and_empty_transfer_has_no_percentage() -> None:
    """Logical rates exclude earlier attempts and zero-byte totals never divide by zero."""
    terminal = Terminal()
    now = [0.0]
    with ProgressDisplay(stream=terminal, clock=lambda: now[0], columns=lambda: 160) as display:
        display.observe(_event(TransferPhase.TRANSFERRING))
        now[0] = 1
        display.observe(_event(TransferPhase.TRANSFERRING, 40))
        display.observe(_event(TransferPhase.RETRYING, attempt=2))
        now[0] = 2
        display.observe(_event(TransferPhase.TRANSFERRING, 10, attempt=2))
        assert "10 B/s" in terminal.rows[-1] and "~9s remaining" in terminal.rows[-1]
        display.observe(_event(TransferPhase.FAILED, 10, attempt=2))
    terminal = Terminal()
    with ProgressDisplay(stream=terminal) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 0, 0))
        display.observe(_event(TransferPhase.COMPLETED, 0, 0))
    assert "0 B/0 B" in terminal.getvalue() and "%" not in terminal.getvalue()


def test_interleaved_objects_keep_ordinals_and_finish_out_of_order() -> None:
    """Completing one object must retain another object's live row and verification gate."""
    terminal, now = Terminal(), [0.0]
    other = "sha256:" + "b" * 64
    with ProgressDisplay(stream=terminal, clock=lambda: now[0], columns=lambda: 160) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 20))
        display.observe(_event(TransferPhase.TRANSFERRING, 10, digest=other))
        assert sum(row.startswith("Blob") for row in terminal.rows) == 2
        now[0] = 1
        display.observe(_event(TransferPhase.TRANSFERRING, 100, digest=other))
        assert display.completed == 0 and "Uploaded" not in terminal.getvalue()
        display.observe(_event(TransferPhase.COMPLETED, 100, digest=other))
        assert display.completed == 1
        assert any("Blob 1" in row and "Uploading" in row for row in terminal.rows)
        display.observe(_event(TransferPhase.VERIFYING, 100))
        display.observe(_event(TransferPhase.COMPLETED, 100))
    rows = [row for row in terminal.rows if row.startswith("Blob")]
    assert len(rows) == 2 and all("Uploaded" in row for row in rows)
    assert {"Blob 1", "Blob 2"} == {row.split("/")[0] for row in rows}


def test_interleaved_attempts_reset_only_the_parent_blob() -> None:
    """Absolute range rollups replace counts; a drained fallback resets only its own attempt."""
    terminal, now = Terminal(), [0.0]
    other = "sha256:" + "b" * 64
    with ProgressDisplay(stream=terminal, clock=lambda: now[0], columns=lambda: 160) as display:
        display.observe(_event(TransferPhase.TRANSFERRING))
        display.observe(_event(TransferPhase.TRANSFERRING, digest=other))
        now[0] = 1
        display.observe(_event(TransferPhase.TRANSFERRING, 60))
        display.observe(_event(TransferPhase.TRANSFERRING, 40, digest=other))
        display.observe(_event(TransferPhase.PREPARING, attempt=2))
        assert any("Blob 1" in row and "Preparing 0 B" in row for row in terminal.rows)
        assert any("Blob 2" in row and "40 B/s" in row for row in terminal.rows)
        display.observe(_event(TransferPhase.TRANSFERRING, 10, attempt=2))
        display.observe(_event(TransferPhase.COMPLETED, 100, attempt=1))
        assert display.completed == 0
        now[0] = 2
        display.observe(_event(TransferPhase.TRANSFERRING, 20, attempt=2))
        assert any("Blob 1" in row and "20 B/s" in row for row in terminal.rows)
        assert sum(row.startswith("Blob") for row in terminal.rows) == 2
        display.observe(_event(TransferPhase.COMPLETED, 100, attempt=2))
        display.observe(_event(TransferPhase.COMPLETED, 100, digest=other))


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_interleaved_cleanup_finalizes_all_active_rows(error_type: type[BaseException]) -> None:
    """A primary failure marks every unfinished object without altering an earlier success."""
    terminal = Terminal()
    primary = error_type("operation stopped")
    with pytest.raises(error_type) as caught, ProgressDisplay(stream=terminal) as display:
        display.observe(_event(TransferPhase.COMPLETED, 100))
        for value in ("b", "c"):
            display.observe(_event(TransferPhase.TRANSFERRING, 10, digest="sha256:" + value * 64))
        raise primary
    assert caught.value is primary and display.completed == 1
    rows = [row for row in terminal.rows if row.startswith("Blob")]
    assert len(rows) == 3 and "Uploaded" in rows[0]
    assert all(("Cancelled" if error_type is KeyboardInterrupt else "Failed") in row for row in rows[1:])


def test_live_rows_and_redraw_work_exclude_completed_history() -> None:
    """A long-lived blob cannot accumulate full row/rate state for later finished objects."""
    terminal = Terminal()
    with ProgressDisplay(stream=terminal, show_counts=True, total_blobs=None) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 10))
        for index in range(50):
            digest = f"sha256:{index:064x}"
            display.observe(_event(TransferPhase.COMPLETED, 100, digest=digest))
            assert len(display._active) == len(display._starts) == 1
        with patch.object(display, "_text", wraps=display._text) as text:
            display.observe(_event(TransferPhase.VERIFYING, 100))
        assert text.call_count == 1 and display.completed == 50
        display.observe(_event(TransferPhase.COMPLETED, 100))


def test_interleaved_diagnostics_preserve_every_live_row() -> None:
    """Partial telemetry and out-of-order finals leave one diagnostic and one row per object."""
    terminal = Terminal()
    config = TelemetryConfig(output=terminal)
    configure_telemetry(config)
    with ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
        display.observe(_event(TransferPhase.TRANSFERRING, 20))
        display.observe(_event(TransferPhase.TRANSFERRING, 40, digest="sha256:" + "b" * 64))
        config.output.write("partial")
        display.observe(_event(TransferPhase.COMPLETED, 100))
        display.observe(_event(TransferPhase.COMPLETED, 100, digest="sha256:" + "b" * 64))
        config.output.write(" diagnostic\n")
    assert terminal.rows[0] == "partial diagnostic"
    rows = [row for row in terminal.rows if row.startswith("Blob")]
    assert len(rows) == 2 and all("Uploaded" in row for row in rows)
    assert display.completed == 2 and not display._active


@pytest.mark.parametrize("direction", list(TransferDirection))
def test_interleaved_resize_erases_only_the_live_block(direction: TransferDirection) -> None:
    """Styled download and legacy upload rows clear only their reflowed live block."""
    terminal, width = Terminal(), [160]
    terminal.write("prior diagnostic\n")
    other = "sha256:" + "b" * 64
    with ProgressDisplay(stream=terminal, columns=lambda: width[0]) as display:
        first = replace(_event(TransferPhase.TRANSFERRING, 20), direction=direction)
        second = replace(first, bytes_completed=40, digest=other)
        display.observe(first)
        display.observe(second)
        width[0] = 24
        terminal.resize(24, active_rows=2)
        display.observe(replace(first, phase=TransferPhase.VERIFYING, bytes_completed=100))
        assert terminal.rows[0] == "prior diagnostic"
        assert len([row for row in terminal.rows[1:] if row]) == 2
        display.observe(replace(second, phase=TransferPhase.COMPLETED, bytes_completed=100))
        display.observe(replace(first, phase=TransferPhase.COMPLETED, bytes_completed=100))
    assert terminal.rows[0] == "prior diagnostic"
    assert len([row for row in terminal.rows[1:] if row]) == 2


def test_interleaved_viewport_does_not_scroll_its_active_block() -> None:
    """Offscreen objects retain ordinals/state without making live redraw exceed terminal height."""
    terminal = Terminal()
    terminal.write("prior diagnostic\n")
    with (
        patch.object(terminal, "fileno", return_value=2),
        patch("os.get_terminal_size", return_value=os.terminal_size((160, 3))),
        ProgressDisplay(stream=terminal, total_blobs=None) as display,
    ):
        for index in range(4):
            display.observe(_event(TransferPhase.TRANSFERRING, digest=f"sha256:{index:064x}"))
            assert len(display._rendered_lines) <= 2
        for index in (3, 1, 0, 2):
            display.observe(_event(TransferPhase.COMPLETED, 100, digest=f"sha256:{index:064x}"))
    assert terminal.rows[0] == "prior diagnostic"
    rows = [row for row in terminal.rows if row.startswith("Blob")]
    assert len(rows) == 4 and all("Uploaded" in row for row in rows)


@pytest.mark.parametrize("backend", ["legacy", "pooled", "parallel", "ranged", "combined"])
def test_real_download_observations_share_the_renderer(backend: str, tmp_path: Path) -> None:
    """Full streams and genuine ranged/batch downloads render only invoking-thread parent rows."""
    from regshape.tests.test_blob_ranges import PAYLOAD, R, _range

    ranged = backend in ("ranged", "combined")
    payloads = [PAYLOAD, PAYLOAD[::-1]] if ranged else [b"first blob", b"second blob"]
    digests = ["sha256:" + hashlib.sha256(data).hexdigest() for data in payloads]
    terminal = Terminal()
    options = (
        DownloadOptions.legacy() if backend == "legacy"
        else DownloadOptions.sequential() if backend == "pooled"
        else DownloadOptions(
            concurrency=2, ranges_per_blob=2 if ranged else 1, range_size=R, scratch_dir=tmp_path,
        )
    )
    caller = threading.get_ident()
    statuses, observations = [], []

    def observe(event: TransferProgress) -> None:
        """Check serialization at the rendering boundary and retain absolute accepted snapshots."""
        assert threading.get_ident() == caller
        observations.append(event)
        display.observe(event)

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Return bounded entities without requiring overlapping first-request auth negotiation."""
        digest = url.rsplit("/", 1)[-1]
        data = payloads[digests.index(digest)]
        headers = kwargs.get("headers")
        if isinstance(headers, dict) and "Range" in headers:
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            return _range(data, start, end)
        return StreamResponse(data, headers={
            "Content-Length": str(len(data)), "Docker-Content-Digest": digest,
        })

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request", side_effect=respond),
        patch("requests.Session.request", side_effect=respond),
        ProgressDisplay(stream=terminal, total_blobs=None, columns=lambda: 160) as display,
    ):
        client = RegistryClient(TransportConfig("registry.example"))
        if backend in ("legacy", "pooled", "ranged"):
            info = get_blob(client, "repo", digests[0], download_options=options,
                            progress_callback=observe, chunk_size=4096,
                            download_status_callback=None if backend == "legacy" else statuses.append)
            assert info.digest == digests[0]
        else:
            result = get_blobs(
                client, "repo", [BlobDownload(digest, chunk_size=4096) for digest in digests],
                download_options=options, progress_callback=observe,
                download_status_callback=statuses.append,
            )
            assert [info.digest for info in result.blobs] == digests
    expected = 1 if backend in ("legacy", "pooled", "ranged") else 2
    assert display.completed == expected and not display._active
    rows = [row for row in terminal.rows if row.startswith("Blob")]
    assert len(rows) == expected and all("Downloaded" in row for row in rows)
    if ranged:
        completed = [event for event in statuses if event.event == "completed"]
        assert len(completed) == expected and all(event.strategy == "ranged" for event in completed)
        assert all(event.stats.bytes_accepted == len(PAYLOAD) for event in completed)
    assert len({event.digest for event in observations}) == expected


@pytest.mark.parametrize("encoding,cells", [("utf-8", "\u25a0\u25a1"), ("ascii", "=-")])
def test_download_columns_colors_and_encoding(
    encoding: str, cells: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Color/encoding variants have identical columns, neutral numbers/statuses and exact 20-cell bars."""
    monkeypatch.setenv("VTE_CJK_WIDTH", "1")
    events = [
        ArtifactProgress("index", "index", "verifying", "sha256:" + "1" * 64, None, 8500, 8500),
        ArtifactProgress("manifest", "manifest", "received", "sha512:" + "2" * 128, None, 1021, 1021),
        ArtifactProgress("config", "blob", "transferring", "sha256:" + "3" * 64, "config", 453, 453,
                         source="embedded"),
        ArtifactProgress("layer", "blob", "transferring", "sha256:" + "4" * 64, "layer", 24 * 1024**2, 48 * 1024**2),
        ArtifactProgress("other", "blob", "transferring", "sha256:" + "5" * 64, None, 0, None),
    ]
    frames = []
    for no_color in (None, "", "1"):
        colored = not no_color
        if no_color is None:
            monkeypatch.delenv("NO_COLOR", raising=False)
        else:
            monkeypatch.setenv("NO_COLOR", no_color)
        terminal = Terminal(encoding=encoding)
        with ProgressDisplay(stream=terminal, columns=lambda: 160, clock=lambda: 0) as display:
            for event in events:
                display.observe(event)
            frames.append(list(terminal.rows))
            for row, event in zip(terminal.rows, events):
                assert event.digest is not None
                assert row[20:42] == event.digest[:19] + "..."
                assert re.fullmatch(rf"\[[{cells}]{{20}}\]", row[44:66])
                assert row[66:68] == row[91:93] == "  "
                assert row[93:] in ("Verifying", "Received", "Storing (embedded)", "Downloading")
                assert row[68:91] == row[68:91].strip().rjust(23)
                assert "\t" not in row
            assert len(terminal.rows) == 5 and not display._finished
            raw = terminal.getvalue()
            styled = re.findall(r"\x1b\[(9[3-6])m([^\x1b]*)\x1b\[0m", raw)
            if colored:
                assert {code for code, _ in styled} == {"93", "94", "95", "96"}
                assert all(
                    text in {"Index", "Manifest", "Blob", "(config)", "(layer)"}
                    or re.fullmatch(r"sha(?:256|512):[1-5]{12}\.\.\.", text)
                    for _, text in styled
                )
                assert "\x1b[95mIndex\x1b[0m 1" in raw
                assert "\x1b[93m(config)\x1b[0m" in raw
            else:
                assert not styled
            assert click.unstyle(raw).count("[") >= 5
    assert frames[0] == frames[1] == frames[2]


@pytest.mark.parametrize("width", [80, 96])
def test_standard_terminal_retains_aligned_download_bars(width: int) -> None:
    """Ordinary terminals retain 20 cells and totals across mixed rows, retries and final states."""
    terminal = Terminal(encoding="ascii")
    terminal.resize(width)
    blob = replace(
        _event(TransferPhase.TRANSFERRING, 24 * 1024**2, 48 * 1024**2),
        direction=TransferDirection.DOWNLOAD,
    )
    events = [
        ArtifactProgress("index", "index", "verifying", DIGEST, None, 8500, 8500),
        ArtifactProgress("config", "blob", "transferring", DIGEST, "config", 453, 453,
                         source="embedded"),
        ArtifactProgress("unknown", "manifest", "received", DIGEST, None, 1021, None),
        blob,
    ]
    with ProgressDisplay(stream=terminal, total_blobs=3, columns=lambda: width, clock=lambda: 0) as display:
        for event in events:
            display.observe(event)
        rows = terminal.rows
        assert len(rows) == 4 and rows[-1].startswith("Blob 1/3")
        for row, status in zip(rows, ("Verifying", "Storing", "Received", "Downloading")):
            assert row[20:42] == "[" + row[21:41] + "]"
            assert re.fullmatch(r"[=-]{20}", row[21:41])
            assert row[44:63] == row[44:63].strip().rjust(19)
            assert row[65:] == status and len(row) < width
            assert "sha256:" not in row
        assert rows[-1][44:63] == "24.0 MiB / 48.0 MiB"
        assert "1021 B / ?" in rows[2]
        display.observe(replace(blob, phase=TransferPhase.RETRYING, bytes_completed=0, attempt=2))
        assert terminal.rows[-1][65:] == "Retrying"
        display.observe(replace(blob, phase=TransferPhase.VERIFYING, bytes_completed=48 * 1024**2, attempt=2))
        assert terminal.rows[-1][20:42] == "[" + "=" * 20 + "]"
        assert terminal.rows[-1][65:] == "Verifying"
        display.observe(replace(blob, phase=TransferPhase.COMPLETED, bytes_completed=48 * 1024**2, attempt=2))
        assert terminal.rows[0][65:] == "Downloaded"
        assert all(len(row) < width for row in terminal.rows) and len(terminal.rows) == 4


@pytest.mark.parametrize("phase", [TransferPhase.FAILED, TransferPhase.CANCELLED, TransferPhase.COMPLETED])
@pytest.mark.parametrize("artifact", [False, True])
def test_empty_download_failure_does_not_show_a_full_bar(phase: TransferPhase, artifact: bool) -> None:
    """A zero-byte failed/cancelled download has an empty bar; a verified empty body is full."""
    terminal = Terminal(encoding="ascii")
    event = (
        ArtifactProgress("empty", "blob", "stored" if phase == TransferPhase.COMPLETED else phase.value,
                         DIGEST, "layer", 0, 0)
        if artifact else
        replace(_event(phase, 0, 0), direction=TransferDirection.DOWNLOAD)
    )
    with ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
        display.observe(event)
    row = terminal.rows[0]
    assert row[44:66] == "[" + ("=" if phase == TransferPhase.COMPLETED else "-") * 20 + "]"
    assert "0 B / 0 B" in row
    assert row[93:] == {
        TransferPhase.FAILED: "Failed", TransferPhase.CANCELLED: "Cancelled",
        TransferPhase.COMPLETED: "Downloaded",
    }[phase]


@pytest.mark.parametrize("language,hint,squares", [
    ("ja_JP.UTF-8", None, False),
    ("zh_CN.UTF-8", None, False),
    ("ko_KR.UTF-8", None, False),
    ("en_US.UTF-8", "2", False),
    ("ja_JP.UTF-8", "1", True),
])
def test_ambiguous_width_terminal_uses_safe_bar_cells(
    language: str, hint: str | None, squares: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """East Asian width hints/locales cannot silently double a fixed-width download bar."""
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", language)
    monkeypatch.delenv("VTE_CJK_WIDTH", raising=False)
    if hint is not None:
        monkeypatch.setenv("VTE_CJK_WIDTH", hint)
    terminal = Terminal()
    event = replace(_event(TransferPhase.VERIFYING, 100), direction=TransferDirection.DOWNLOAD)
    with ProgressDisplay(stream=terminal, columns=lambda: 120) as display:
        display.observe(event)
        row = terminal.rows[0]
        assert row[44:66] == "[" + ("\u25a0" if squares else "=") * 20 + "]"
        assert row[93:] == "Verifying" and len(row) < 120


@pytest.mark.parametrize("errors", ["strict", "backslashreplace"])
@pytest.mark.parametrize("artifact", [False, True])
def test_download_warning_adapter_preserves_real_ascii_encoding(
    errors: str, artifact: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual command warning wrapper must not hide a strict or escaping ASCII sink's codec."""
    monkeypatch.setenv("VTE_CJK_WIDTH", "1")
    raw = io.BytesIO()
    event = (
        ArtifactProgress("config", "blob", "verifying", DIGEST, "config", 50, 100)
        if artifact else
        replace(_event(TransferPhase.VERIFYING, 50), direction=TransferDirection.DOWNLOAD)
    )
    with io.TextIOWrapper(raw, encoding="ascii", errors=errors, write_through=True) as sink:
        with (
            patch.object(sink, "isatty", return_value=True),
            patch("sys.stderr", sink),
            DownloadWarnings(artifact=artifact) as warnings,
            ProgressDisplay(columns=lambda: 120) as display,
        ):
            warnings.display = display
            display.observe(event)
        text = raw.getvalue().decode("ascii")
    terminal = Terminal()
    terminal.write(text)
    row = terminal.rows[0]
    assert row[44:66] == "[" + "=" * 10 + "-" * 10 + "]"
    assert row[93:] == "Verifying" and len(row) < 120
    assert r"\u25" not in text


@pytest.mark.parametrize("width", [18, 24, 40])
def test_compact_mixed_kinds_and_multi_digit_ordinals_stay_distinct(width: int) -> None:
    """Kind prefixes disambiguate per-kind ordinals, sharing columns across one/two-digit labels."""
    terminal = Terminal()
    terminal.resize(width)
    events = [
        ArtifactProgress("index", "index", "verifying", DIGEST, None, 2, 6),
        ArtifactProgress("manifest", "manifest", "verifying", DIGEST, None, 2, 6),
        *[
            ArtifactProgress(f"blob-{index}", "blob", "verifying", DIGEST, "layer", 2, 6)
            for index in range(10)
        ],
    ]
    with ProgressDisplay(stream=terminal, columns=lambda: width) as display:
        for event in events:
            display.observe(event)
        rows = terminal.rows
        assert len(rows) == 12
        assert [row.split()[0] for row in rows] == ["I1", "M1", *[f"B{i}" for i in range(1, 11)]]
        status = "Verify" if width == 18 else "Verifying"
        assert len({row.index(status) for row in rows}) == 1
        assert all("2 B" in row and len(row) < width for row in rows)


def test_wide_download_fields_realign_every_live_row() -> None:
    """A wider accepted count grows shared columns even during a throttled byte-only update."""
    terminal = Terminal()
    terminal.width = 200
    initial = replace(_event(TransferPhase.TRANSFERRING, 1, 10**50), direction=TransferDirection.DOWNLOAD)
    other = replace(initial, digest="sha256:" + "b" * 64, total_bytes=None)
    with ProgressDisplay(stream=terminal, columns=lambda: 200, clock=lambda: 0) as display:
        display.observe(initial)
        display.observe(other)
        display.observe(replace(initial, bytes_completed=10**50))
        rows = terminal.rows
        assert len(rows) == 2
        assert len(rows[0]) > 104
        assert rows[0].index("Downloading") == rows[1].index("Downloading")
        assert rows[0].rindex("  Downloading") == rows[1].rindex("  Downloading")
        assert " / ?" in rows[1] and " / " in rows[0]
        assert all(len(row) < 200 for row in rows)
        assert not display.completed


@pytest.mark.parametrize("failure", [TransferPhase.FAILED, TransferPhase.CANCELLED])
def test_download_attempt_rollups_preserve_verification_and_other_rows(failure: TransferPhase) -> None:
    """Duplicate ranges/stale attempts never inflate counts or turn a full bar into verified success."""
    terminal, now = Terminal(), [0.0]
    initial = replace(_event(TransferPhase.TRANSFERRING, 60), direction=TransferDirection.DOWNLOAD)
    other = replace(initial, digest="sha256:" + "b" * 64, bytes_completed=40)
    with ProgressDisplay(stream=terminal, columns=lambda: 160, clock=lambda: now[0]) as display:
        display.observe(initial)
        display.observe(other)
        display.observe(initial)
        retry = replace(initial, attempt=2, phase=TransferPhase.RETRYING, bytes_completed=0)
        display.observe(retry)
        assert "0 B / 100 B" in terminal.rows[0] and "40 B / 100 B" in terminal.rows[1]
        display.observe(replace(initial, phase=TransferPhase.COMPLETED, bytes_completed=100))
        assert display.completed == 0
        now[0] = 1
        retry = replace(retry, phase=TransferPhase.VERIFYING, bytes_completed=100)
        display.observe(retry)
        assert any(cell * 20 in terminal.rows[0] for cell in ("\u25a0", "="))
        assert "Verifying" in terminal.rows[0]
        assert display.completed == 0 and not display._finished
        display.observe(replace(other, phase=TransferPhase.COMPLETED, bytes_completed=100))
        display.observe(replace(retry, phase=failure))
    rows = [row for row in terminal.rows if row]
    assert len(rows) == 2 and "Blob 2" in rows[0] and "Downloaded" in rows[0]
    assert "Blob 1" in rows[1] and failure.value.title() in rows[1]
    assert display.completed == 1


@pytest.mark.parametrize("fault", ["retry", "fallback", "corrupt", "cancel"])
def test_ranged_download_faults_keep_one_truthful_row(tmp_path: Path, fault: str) -> None:
    """Actual retries/fallback reset one parent; corrupt bytes and cancellation cannot render success."""
    from regshape.tests.test_blob_batch import _client
    from regshape.tests.test_blob_ranges import PAYLOAD, R, _options, _range, _registry

    terminal = Terminal()
    observations, statuses = [], []
    injected = False

    def modify(start: int, end: int, headers: dict[str, str], response: requests.Response) -> requests.Response:
        """Inject one retry, changed validator or incorrect part after the initial real range."""
        nonlocal injected
        if headers.get("Range") and start == R and not injected:
            injected = True
            if fault == "retry":
                return StreamResponse(b"", status=503, headers={"Retry-After": "0"})
            if fault == "fallback":
                return _range(PAYLOAD, start, end, etag='"changed"')
            if fault == "corrupt":
                return _range(b"x" * len(PAYLOAD), start, end)
        return response

    def observe(event: TransferProgress) -> None:
        """Deliver real immutable rollups, optionally interrupting after accepted range bytes."""
        observations.append(event)
        display.observe(event)
        if fault == "cancel" and event.phase == TransferPhase.TRANSFERRING and event.bytes_completed:
            raise KeyboardInterrupt()

    get, calls, _ = _registry(modify=modify)
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        if fault in ("corrupt", "cancel"):
            error = BlobError if fault == "corrupt" else KeyboardInterrupt
            with pytest.raises(error), ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
                get_blob(_client(), "repo", "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
                         download_options=_options(tmp_path), progress_callback=observe,
                         download_status_callback=statuses.append)
        else:
            with ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
                get_blob(_client(), "repo", "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
                         download_options=_options(tmp_path), progress_callback=observe,
                         download_status_callback=statuses.append)
    rows = [row for row in terminal.rows if row]
    assert len(rows) == 1 and rows[0].startswith("Blob 1/1 ")
    assert ("Downloaded" in rows[0]) is (fault in ("retry", "fallback"))
    assert all(event.bytes_completed <= len(PAYLOAD) for event in observations)
    assert len({event.digest for event in observations}) == 1
    if fault == "fallback":
        assert any("Range" not in call for call in calls)
        assert any(event.attempt == 2 and event.bytes_completed == 0 for event in observations)
    if fault == "retry":
        assert statuses[-1].stats.retry_count == 1
    assert not list(tmp_path.iterdir())


def test_preparation_label_cannot_inject_terminal_controls() -> None:
    """Caller-supplied preparation text is escaped before it reaches the terminal."""
    terminal = Terminal()
    with ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
        display.prepare("image\n\x1b[31m")
        display.finish_preparation()
    assert "\x1b[31m" not in terminal.getvalue()
    assert len([r for r in terminal.rows if r]) == 1


@pytest.mark.parametrize("chunked,source_kind", [
    (False, "regular"), (True, "regular"), (True, "pipe"),
    (True, "virtual-zero"), (True, "virtual-oversized"), (True, "virtual-undersized"),
    (False, "empty"), (True, "empty"),
])
@pytest.mark.parametrize("mode", ["tty", "disabled", "structured"])
def test_blob_upload_real_library_consumption(
    runner: CliRunner, upload_source: Path, chunked: bool, source_kind: str, mode: str,
) -> None:
    """Both real upload protocols produce intermediate observations through command wiring."""
    data = b"" if source_kind == "empty" else b"abcdef"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    observations: list[TransferProgress] = []
    methods: list[str] = []
    payloads: list[bytes] = []
    frames: list[list[str]] = []
    terminal, now = Terminal(), [0.0]
    observe = ProgressDisplay.observe
    original_stat = os.fstat

    def stat_source(descriptor: int) -> os.stat_result:
        """Model virtual-file size hints while retaining an actual file-backed reader."""
        result = original_stat(descriptor)
        if source_kind.startswith("virtual-"):
            fields = list(result)
            fields[stat.ST_SIZE] = {
                "virtual-zero": 0, "virtual-oversized": 4096, "virtual-undersized": 2,
            }[source_kind]
            return os.stat_result(fields)
        return result

    def display(*, disabled: bool) -> ProgressDisplay:
        """Supply a real terminal and controlled clock without bypassing command options."""
        return ProgressDisplay(stream=terminal, clock=lambda: now[0], columns=lambda: 160, disabled=disabled)

    def record(display: ProgressDisplay, event: TransferProgress) -> None:
        """Record all observations without bypassing the actual renderer."""
        observations.append(event)
        observe(display, event)
        frames.append(terminal.rows)

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Consume a streamed PUT or accept buffered PATCHes without a live registry."""
        now[0] += 0.2
        methods.append(method)
        response = requests.Response()
        response.status_code = 201 if method == "PUT" else 202
        response.headers["Location"] = "/v2/repo/blobs/uploads/session"
        response._content = b""
        response._content_consumed = True
        if method == "PATCH":
            payload = kwargs["data"]
            assert isinstance(payload, bytes)
            payloads.append(payload)
            response.headers["Range"] = f"0-{len(b''.join(payloads)) - 1}"
        if method == "PUT":
            if not chunked:
                body = kwargs["data"]
                assert isinstance(body, io.RawIOBase)
                while True:
                    now[0] += 0.2
                    piece = body.read(2)
                    if not piece:
                        break
                    payloads.append(piece)
            response.headers["Docker-Content-Digest"] = digest
        return response

    args = ["blob", "upload", "--repo", "registry.example.invalid/repo", "--file", str(upload_source),
            "--digest", digest, "--chunk-size", "2"]
    if chunked:
        args.append("--chunked")
    if mode == "disabled":
        args.append("--no-progress")
    if mode == "structured":
        args.extend(["--telemetry-format", "json"])
    with (
        patch("regshape.cli.blob.ProgressDisplay", side_effect=display),
        patch.object(ProgressDisplay, "observe", record),
        patch("regshape.cli.blob.os.fstat", side_effect=stat_source),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
    ):
        result = runner.invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert b"".join(payloads) == data
    assert json.loads(result.stdout)["digest"] == digest
    assert methods == (["POST"] + ["PATCH"] * ((len(data) + 1) // 2) + ["PUT"] if chunked else ["POST", "PUT"])
    if data:
        assert any(e.phase == TransferPhase.TRANSFERRING and 0 < e.bytes_completed < 6 for e in observations)
    known_size = source_kind in ("regular", "empty")
    assert all(
        e.total_bytes == (len(data) if known_size else None)
        for e in observations if e.phase != TransferPhase.PREPARING
    )
    assert observations[-1].phase == TransferPhase.COMPLETED and result.stderr == ""
    if mode == "tty":
        if data:
            assert any("Uploading 2 B" in row for frame in frames for row in frame)
        assert len([row for row in terminal.rows if row.startswith("Blob")]) == 1
        assert "Uploaded" in terminal.rows[0]
    else:
        assert terminal.getvalue() == ""


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_blob_upload_stream_wiring_preserves_json(
    runner: CliRunner, tmp_path: Path, chunked: bool, disabled: bool,
) -> None:
    """The CLI selects the matching file-backed protocol and passes a real observer."""
    path = tmp_path / "blob"
    path.write_bytes(b"abcdef")
    seen: list[TransferProgress] = []

    def upload(*, source: BinaryIO, progress_callback: ProgressCallback, **kwargs: object) -> str:
        """Consume the borrowed file and emit the same lifecycle as the library."""
        assert source.read() == b"abcdef"
        for event in (_event(TransferPhase.TRANSFERRING, 0, 6), _event(TransferPhase.COMPLETED, 6, 6)):
            progress_callback(event)
            seen.append(event)
        return DIGEST

    args = ["blob", "upload", "--repo", "registry.example.invalid/repo", "--file", str(path), "--digest", DIGEST]
    if chunked:
        args.append("--chunked")
    if disabled:
        args.append("--no-progress")
    name = "upload_blob_chunked" if chunked else "upload_blob_stream"
    with (
        patch("regshape.cli.blob.RegistryClient"),
        patch("regshape.cli.blob." + name, side_effect=upload),
    ):
        result = runner.invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["size"] == 6 and len(seen) == 2
    assert result.stderr == ""


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ESPIPE, errno.EIO])
def test_size_probe_unsupported_seek_and_io_errors(
    tmp_path: Path, error_number: int, caplog: pytest.LogCaptureFixture,
) -> None:
    """Unsupported length probes use unknown totals; genuine I/O errors remain explicit."""
    path = tmp_path / "source"
    path.write_bytes(b"abcdef")
    error = OSError(error_number, "synthetic probe error")
    caplog.set_level(logging.DEBUG, logger="regshape.cli.blob")
    with path.open("rb") as source:
        original_seek = source.seek

        def seek(offset: int, whence: int = 0) -> int:
            """Reject only the boundary probe while retaining valid reads and rewinds."""
            if offset == 6:
                raise error
            return original_seek(offset, whence)

        with patch.object(source, "seek", side_effect=seek):
            if error_number == errno.EIO:
                with pytest.raises(OSError) as caught:
                    _trusted_file_size(source)
                assert caught.value is error
            else:
                assert _trusted_file_size(source) is None
                assert "File size probe unsupported" in caplog.text
        assert source.tell() == 0 and source.read() == b"abcdef"


@pytest.mark.parametrize("disabled", [False, True])
def test_blob_download_real_library_wiring(runner: CliRunner, disabled: bool) -> None:
    """Exercise the pooled bounded receiver with a real urllib3 stream, without sockets."""
    from regshape.tests.artifact_fixtures import StreamResponse

    data = b"abcdef"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    response = StreamResponse(data, headers={"Content-Length": "6"})
    terminal = Terminal()

    def display(*, disabled: bool) -> ProgressDisplay:
        """Make stderr interactive while preserving the command's opt-out argument."""
        return ProgressDisplay(stream=terminal, disabled=disabled)

    args = ["blob", "get", "--repo", "registry.example.invalid/repo", "--digest", digest, "--chunk-size", "2"]
    if disabled:
        args.append("--no-progress")
    with (
        patch("regshape.cli.blob.ProgressDisplay", side_effect=display),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", return_value=response) as http,
        patch("requests.Session.request", side_effect=http),
    ):
        result = runner.invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["digest"] == digest
    assert result.stderr == "" and http.call_count == 1
    assert ("Downloaded" in terminal.getvalue()) is not disabled

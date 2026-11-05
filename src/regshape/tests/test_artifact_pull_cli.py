"""Actual artifact command/library/client flows using synthetic HTTP and safe temporary files.

The shared terminal emulator checks visible frames, not control-character counts.
Only HTTP/credential boundaries and terminal/file fault sinks are substituted;
native filesystem functionality is skipped on unsupported hosts, whose fail-closed
command guards remain executable.
"""

import base64
import errno
import io
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
import requests
from click.testing import CliRunner, Result

from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.artifacts import (
    ArtifactFormat,
    ArtifactLimits,
    ArtifactPhase,
    ArtifactProgress,
    preflight_artifact_pull,
    pull_artifact,
    snapshot_layout,
)
from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST
from regshape.libs.artifacts.native import NativeFilesystem
from regshape.libs.artifacts.storage import SafeDirectory, SafeFile
from regshape.libs.errors import ArtifactError
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.tests.artifact_fixtures import (
    StreamResponse,
    address,
    document,
    edge,
    nested_registry,
)
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - minimum Click fixture
from regshape.tests.test_artifact_push import ObjectRegistry, _graph
from regshape.tests.test_auth_challenge_origin import (
    BASE,
    CDN,
    TOKEN_ORIGIN,
    WireAdapter,
    WireResponse,
    challenge,
)
from regshape.tests.test_auth_challenge_origin import (
    wire as wire,  # noqa: PLC0414 - real Requests redirect machinery, no sockets
)
from regshape.tests.test_manifest_cli_progress import (
    Console,
    Frames,
)
from regshape.tests.test_manifest_cli_progress import (
    reset_telemetry as reset_telemetry,  # noqa: PLC0414 - isolate shared telemetry
)

_NATIVE = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="Artifact publication requires Linux/Darwin native safe storage",
)
_LIMITS = (
    "--max-manifest-bytes", "--max-objects", "--max-depth",
    "--max-total-bytes", "--max-archive-bytes", "--max-archive-input-bytes",
)
_SOURCE = "registry.example/source:moving"


class ArtifactConsole(Console):
    """Use the existing emulator with complete-command stream lifetimes, including final stdout."""

    def artifact_display(self, **kwargs: Any) -> ProgressDisplay:
        """Construct the real normalized renderer after telemetry config is installed."""
        display = ProgressDisplay(
            **kwargs, stream=self.err, columns=lambda: self.terminal.width, clock=self.clock,
        )
        self.displays.append(display)
        return display


@contextmanager
def _console_streams(runner: CliRunner, console: ArtifactConsole) -> Iterator[None]:
    """Borrow terminal channels for the whole Click isolation, including telemetry close/result."""
    original = runner.isolation

    @contextmanager
    def isolate(*args: Any, **kwargs: Any) -> Iterator[Any]:
        """Nest stream substitution inside Click's own setup/restore on all supported versions."""
        with (
            original(*args, **kwargs) as streams,
            patch("sys.stdout", console.out),
            patch("sys.stderr", console.err),
        ):
            yield streams

    with patch.object(runner, "isolation", side_effect=isolate):
        yield


def _invoke(
    runner: CliRunner, console: ArtifactConsole, registry: ObjectRegistry,
    output: Path, options: list[str] | None = None, *,
    source: str = _SOURCE, global_options: list[str] | None = None,
) -> tuple[Result, MagicMock]:
    """Run the actual registered command and clients, replacing HTTP/credentials and console only."""
    with (
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request", side_effect=registry.respond) as send,
        patch("regshape.cli.artifact.ProgressDisplay", side_effect=console.artifact_display),
    ):
        result = runner.invoke(regshape, [
            *(global_options or []), "artifact", "pull", "-a", source,
            "-o", str(output), *(options or []),
        ])
    return result, send


def _rows(console: ArtifactConsole) -> list[str]:
    """Select retained object rows, excluding diagnostics, local phases and the result summary."""
    return [row for row in console.terminal.rows if row.startswith(("Index ", "Manifest ", "Blob "))]


def _origin(url: str | None) -> tuple[str, str | None, int | None]:
    """Compare synthetic request origins by parsed scheme/host/port, never URL prefixes."""
    parsed = urlsplit(url or "")
    return parsed.scheme, parsed.hostname, parsed.port


def _observation(
    *, identity: str = "root", kind: str = "unknown", phase: str = "preparing",
    digest: str | None = None, role: str | None = None, count: int = 0,
    total: int | None = None, source: str = "network", attempt: int = 1,
) -> ArtifactProgress:
    """Build validated observations for deterministic renderer-only tests."""
    return ArtifactProgress(identity, kind, phase, digest, role, count, total, attempt, source)


@_NATIVE
@pytest.mark.parametrize("kind", ["simple", "index"])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
@pytest.mark.parametrize("structured", [False, True])
def test_four_roots_exact_results_and_push_roundtrip(
    runner: CliRunner, tmp_path: Path, kind: str, pinned: bool,
    output_format: str, structured: bool,
) -> None:
    """Each tag/digest x manifest/index root downloads and republishes byte-identical complete output."""
    root, objects = _graph(kind, "sha256")
    registry = ObjectRegistry(root, objects)
    console = ArtifactConsole(shared=False)
    source = f"registry.example/source@{address(root)}" if pinned else _SOURCE
    output = tmp_path / "artifact.any-suffix"
    options = ["--format", output_format, *(["--json"] if structured else [])]
    result, send = _invoke(runner, console, registry, output, options, source=source)
    assert result.exit_code == 0, result.exception
    assert all(call.args[0] == "GET" and call.kwargs["stream"] for call in send.call_args_list)
    assert len(registry.calls) == len(objects)
    assert len(set(registry.calls)) == len(objects)
    assert all(response.close_calls == 1 for response in registry.responses)
    with snapshot_layout(output) as snapshot:
        assert snapshot.graph.roots[0].digest == address(root)
        assert {obj.digest for obj in snapshot.graph.objects} == objects.keys()
        for obj in snapshot.graph.objects:
            algorithm, encoded = obj.digest.split(":")
            with snapshot.store.algorithms[algorithm].read_file(encoded) as stream:
                assert stream.read() == objects[obj.digest]
        wrapper = json.loads(snapshot.wrapper_bytes)
        assert ("annotations" in wrapper["manifests"][0]) is not pinned
    if structured:
        data = json.loads(console.out.text)
        assert data == {
            "source": source, "resolved_reference": f"registry.example/source@{address(root)}",
            "root": wrapper["manifests"][0], "output_path": str(output), "output_format": output_format,
            "indexes": int(kind == "index"), "manifests": 1, "blobs": 2,
            "downloaded_objects": len(objects), "embedded_objects": 0, "reused_references": 0,
            "bytes_downloaded": sum(map(len, objects.values())), "bytes_stored": sum(map(len, objects.values())),
        }
        assert not console.err.text
    else:
        assert console.out.text == (
            "Downloaded complete artifact.\n"
            f"Source: {source}\nResolved: registry.example/source@{address(root)}\n"
            f"Output: {output}\nFormat: {output_format}\n"
            f"Objects: {int(kind == 'index')} indexes, 1 manifests, 2 blobs\n"
            "Shared references reused: 0\n"
        )
        assert len(_rows(console)) == len(objects)
        assert all("Downloaded" in row for row in _rows(console))
        assert "Publishing artifact ... done" in console.err.text
        assert ("Packaging OCI archive ... done" in console.err.text) == (output_format == "oci-tar-gzip")
    assert "\x1b" not in console.out.text
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request", side_effect=registry.respond),
    ):
        pushed = runner.invoke(regshape, [
            "layout", "push", "--path", str(output), "--dest", "registry.example/dest:copy", "--json",
        ])
    assert pushed.exit_code == 0, pushed.exception
    assert registry.references["copy"] == address(root)
    assert registry.objects == objects
    restored = tmp_path / "restored"
    registry.source = registry.objects
    restored_console = ArtifactConsole()
    result, _ = _invoke(runner, restored_console, registry, restored, ["--json"], source=f"registry.example/dest@{address(root)}")
    assert result.exit_code == 0, result.exception
    with snapshot_layout(restored) as snapshot:
        assert {obj.digest for obj in snapshot.graph.objects} == objects.keys()


@_NATIVE
@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
def test_nested_shared_tiny_config_has_real_intermediate_and_one_final_row(
    runner: CliRunner, tmp_path: Path, output_format: str,
) -> None:
    """Every unique object, even a one-read config, is visible without delays or duplicate role rows."""
    _, root, objects = nested_registry()
    registry, console = ObjectRegistry(root, objects), ArtifactConsole(width=200)
    result, _ = _invoke(runner, console, registry, tmp_path / "out", ["--format", output_format])
    assert result.exit_code == 0, result.exception
    frames = ["\n".join(frame) for frame in console.terminal.frames]
    assert any("Manifest 1 Preparing 0 B (total unknown)" in frame for frame in frames)
    assert len(_rows(console)) == len(objects) == 7
    assert len([row for row in _rows(console) if row.startswith("Index ")]) == 3
    assert len([row for row in _rows(console) if row.startswith("Manifest ")]) == 2
    assert len([row for row in _rows(console) if "(config)" in row]) == 1
    for digest, raw in objects.items():
        short = digest[:11] + "..."
        matching = [row for row in _rows(console) if short in row]
        assert len(matching) == 1 and "Downloaded" in matching[0]
        assert f"{len(raw)} B/{len(raw)} B" in matching[0] or len(raw) >= 1024
        assert any(short in row and ("Verifying" in row or "Downloading" in row)
                   for frame in console.terminal.frames for row in frame)
    assert any("(config)" in row and "Downloading" in row
               for frame in console.terminal.frames for row in frame)
    assert "Shared references reused: 5" in console.out.text
    assert len(registry.calls) == 7 and all("referrers" not in path for _, path in registry.calls)
    assert len(console.displays[0]._indices) == 7
    assert not any("overall" in frame.lower() for frame in frames)


@_NATIVE
@pytest.mark.parametrize("embedded_role", ["config", "layer"])
def test_embedded_objects_are_stored_without_fictitious_network(
    runner: CliRunner, tmp_path: Path, embedded_role: str,
) -> None:
    """Inline content occupies one stored row; duplicate references do not make a request or inflate totals."""
    config, layer = b"{}", b"local inline layer"
    config_edge, layer_edge = edge(config), edge(layer)
    selected, raw = (config_edge, config) if embedded_role == "config" else (layer_edge, layer)
    selected["data"] = base64.b64encode(raw).decode("ascii")
    root = document(config=config_edge, layers=[layer_edge, selected])
    objects = {address(value): value for value in (root, config, layer)}
    registry, console = ObjectRegistry(root, objects), ArtifactConsole(width=200)
    result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exit_code == 0, result.exception
    short = address(raw)[:11] + "..."
    rows = [row for row in _rows(console) if short in row]
    assert len(rows) == 1 and "Stored (embedded)" in rows[0]
    relevant = [row for frame in console.terminal.frames for row in frame if short in row]
    assert not any("Downloading" in row or "/s" in row or "%" in row for row in relevant)
    assert len(registry.calls) == 2 and len(_rows(console)) == 3
    assert all(address(raw) not in path for _, path in registry.calls)
    second_console = ArtifactConsole()
    result, _ = _invoke(runner, second_console, registry, tmp_path / "json", ["--json"])
    data = json.loads(second_console.out.text)
    assert result.exit_code == 0 and data["embedded_objects"] == 1
    assert data["downloaded_objects"] == 2 and data["reused_references"] == 1
    assert data["bytes_stored"] == sum(map(len, objects.values()))
    assert data["bytes_downloaded"] == data["bytes_stored"] - len(raw)


@pytest.mark.parametrize("known", [False, True])
def test_normalized_rows_upgrade_identity_rate_and_throttle(known: bool) -> None:
    """Unknown root upgrades on one row; actual-attempt bytes drive rates and only known totals permit percent."""
    terminal, now = Frames(), [0.0]
    terminal.width = 200
    digest = address(b"root")
    total = 100 if known else None
    with ProgressDisplay(stream=terminal, columns=lambda: terminal.width, clock=lambda: now[0]) as display:
        display.observe(_observation())
        assert terminal.rows == ["Manifest 1 Preparing 0 B (total unknown)"]
        display.observe(_observation(kind="index", phase="transferring", total=total))
        for count in range(1, 21):
            now[0] += 0.001
            display.observe(_observation(kind="index", phase="transferring", count=count, total=total))
        assert terminal.rows == [
            "Index 1 Downloading 0 B/100 B [------------] 0%" if known
            else "Index 1 Downloading 0 B (total unknown)"
        ]
        now[0] = 2
        display.observe(_observation(kind="index", phase="transferring", count=50, total=total))
        assert "25 B/s" in terminal.rows[-1]
        assert ("50%" in terminal.rows[-1]) is known
        assert ("~2s remaining" in terminal.rows[-1]) is known
        display.observe(_observation(kind="index", phase="verifying", digest=digest, count=100, total=100))
        display.observe(_observation(kind="index", phase="stored", digest=digest, count=100, total=100))
        display.observe(_observation(kind="index", phase="stored", digest=digest, count=100, total=100))
        assert len(display._indices) == 1
    assert [row for row in terminal.rows if row] == [f"Index 1 {digest[:11]}... Downloaded 100 B/100 B"]
    assert not any("Manifest 1" in row for row in terminal.rows)


@pytest.mark.parametrize("source", ["embedded", "local"])
def test_local_source_has_no_network_percentage_or_rate(source: str) -> None:
    """The normalized adapter distinguishes local storage even for caller-supplied transferring states."""
    terminal = Frames()
    with ProgressDisplay(stream=terminal, columns=lambda: 180, clock=lambda: 2) as display:
        event = _observation(identity="config", kind="blob", role="config", source=source,
                             digest=address(b"{}"), total=2)
        display.observe(event)
        display.observe(replace(event, phase="transferring", bytes_completed=2))
        display.observe(replace(event, phase="stored", bytes_completed=2))
    assert "Stored (" + source + ")" in terminal.rows[-2]
    assert not any("%" in row or "/s" in row or "Downloading" in row
                   for frame in terminal.frames for row in frame)


@pytest.mark.parametrize("width", [18, 24, 40, 80, 160])
def test_artifact_rows_preserve_state_on_reflow(width: int) -> None:
    """Geometry changes reflow the existing shared active row rather than leaving stale root/config copies."""
    terminal = Frames()
    terminal.width = 200
    with ProgressDisplay(stream=terminal, columns=lambda: terminal.width, clock=lambda: 0) as display:
        event = _observation(identity="config", kind="blob", role="config",
                             digest=address(b"config"), phase="transferring", count=20, total=100)
        display.observe(event)
        terminal.resize(width)
        display.observe(replace(event, bytes_completed=30))
        assert len([row for row in terminal.rows if row]) == 1
        assert len(terminal.rows[-1]) < width and "30 B" in terminal.rows[-1]
        display.observe(replace(event, phase="stored", bytes_completed=100))
    assert len([row for row in terminal.rows if row]) == 1
    assert ("Stored" if width == 18 else "Downloaded") in terminal.rows[-2]


@_NATIVE
@pytest.mark.parametrize("options,interactive", [
    (["--json"], True), (["--no-progress"], True), ([], False),
    (["--telemetry-format", "json"], True),
])
def test_all_object_kinds_respect_suppression(
    runner: CliRunner, tmp_path: Path, options: list[str], interactive: bool,
) -> None:
    """Nonterminal/structured/disabled modes stream the same graph with no progress controls or phase text."""
    root, objects = _graph("nested", "sha256")
    registry, console = ObjectRegistry(root, objects), ArtifactConsole(interactive=interactive)
    result, _ = _invoke(runner, console, registry, tmp_path / "out", options)
    assert result.exit_code == 0, result.exception
    assert console.err.text == ""
    assert len(registry.calls) == len(objects)
    assert "\x1b" not in console.out.text


@pytest.mark.parametrize("limit", _LIMITS)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "not-an-int"])
def test_limits_reject_bad_choices_before_credentials(
    runner: CliRunner, tmp_path: Path, limit: str, value: str,
) -> None:
    """Every exposed budget is a positive integer parsed before command, credentials or filesystem work."""
    with patch("regshape.libs.transport.client.resolve_credentials") as credentials, patch("requests.request") as send:
        result = runner.invoke(regshape, ["artifact", "pull", "-a", _SOURCE, "-o", str(tmp_path / "out"), limit, value])
    assert result.exit_code == 2
    credentials.assert_not_called()
    send.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source", [
    "repo:tag", "registry.example/repo@tag", "registry.example/repo@sha256:nope",
    "registry.example/../repo:tag", "registry.example/repo:bad tag",
    "registry.example/repo:bad#fragment", "registry.example/repo:bad?query",
    "registry.example/repo:bad%2ftag", "registry.example/repo@sha1:" + "a" * 40,
    "https://synthetic-user:synthetic-secret@registry.example/repo:tag",
    "synthetic-user@registry.example/repo:tag", "registry.example:badport/repo:tag",
])
def test_source_preflight_rejects_before_credentials(
    runner: CliRunner, tmp_path: Path, source: str,
) -> None:
    """Strict complete-pull grammar rejects unsafe/unsupported selectors without echoing URL credentials."""
    with patch("regshape.libs.transport.client.resolve_credentials") as credentials, patch("requests.request") as send:
        result = runner.invoke(regshape, ["artifact", "pull", "-a", source, "-o", str(tmp_path / "out")])
    assert result.exit_code == 2, result.exception
    assert "synthetic-secret" not in result.output
    credentials.assert_not_called()
    send.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("options", [[], ["-a", _SOURCE], ["-o", "out"], ["-a", _SOURCE, "-o", "out", "--format", "tar"]])
def test_required_arguments_and_format_choices(runner: CliRunner, options: list[str]) -> None:
    """Missing required flags and unsupported formats are usage errors before credentials."""
    with patch("regshape.libs.transport.client.resolve_credentials") as credentials, patch("requests.request") as send:
        result = runner.invoke(regshape, ["artifact", "pull", *options])
    assert result.exit_code == 2
    credentials.assert_not_called()
    send.assert_not_called()


@_NATIVE
@pytest.mark.parametrize("destination", ["existing-file", "existing-directory", "symlink", "missing-parent", "untrusted-parent", "parent-component"])
def test_destination_preflight_before_credentials(
    runner: CliRunner, tmp_path: Path, destination: str,
) -> None:
    """Unsafe/non-new destinations are rejected without credential lookup and without damaging existing bytes."""
    output = tmp_path / "out"
    if destination == "existing-file":
        output.write_bytes(b"keep")
    elif destination == "existing-directory":
        output.mkdir()
    elif destination == "symlink":
        output.symlink_to(tmp_path / "absent")
    elif destination == "missing-parent":
        output = tmp_path / "missing" / "out"
    elif destination == "untrusted-parent":
        parent = tmp_path / "unsafe"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        output = parent / "out"
    else:
        output = tmp_path / "missing" / ".." / "out"
    with patch("regshape.libs.transport.client.resolve_credentials") as credentials, patch("requests.request") as send:
        result = runner.invoke(regshape, ["artifact", "pull", "-a", _SOURCE, "-o", str(output)])
    assert result.exit_code == 1, result.exception
    credentials.assert_not_called()
    send.assert_not_called()
    if destination == "existing-file":
        assert output.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".regshape-*"))


@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
def test_unsupported_platform_fails_closed_before_credentials(
    runner: CliRunner, tmp_path: Path, output_format: str,
) -> None:
    """Windows is not presented as a supported native backend; real and simulated hosts reject before auth."""
    with (
        patch("sys.platform", "win32"),
        patch("regshape.libs.transport.client.resolve_credentials") as credentials,
        patch("requests.request") as send,
    ):
        result = runner.invoke(regshape, [
            "artifact", "pull", "-a", _SOURCE, "-o", str(tmp_path / "out"), "--format", output_format,
        ])
    assert result.exit_code == 1 and "unsupported" in result.stderr.lower()
    credentials.assert_not_called()
    send.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_registered_help_exposes_limits_and_honest_platform_boundary(runner: CliRunner) -> None:
    """Help names required selectors, all policy defaults and unsupported native platforms without #112 options."""
    result = runner.invoke(regshape, ["artifact", "pull", "--help"])
    assert result.exit_code == 0
    for option in (*_LIMITS, "--artifact", "--image-ref", "--output", "--format", "--no-progress", "--json"):
        assert option in result.stdout
    help_text = " ".join(result.stdout.split())
    assert "Deprecated in 0.2" in help_text and "Windows" in help_text and "fails closed" in help_text
    assert "--part" not in result.stdout and "--platform" not in result.stdout and "--raw" not in result.stdout
    assert ":returns:" not in result.stdout
    assert "artifact" in runner.invoke(regshape, ["--help"]).stdout


@_NATIVE
def test_multichunk_frames_for_every_kind_and_unknown_root(runner: CliRunner, tmp_path: Path) -> None:
    """Observe actual 64-KiB reads for every kind, without turning unknown lengths into percentages."""
    config, layer = b'{"padding":"' + b"x" * 140000 + b'"}', b"y" * 140000

    def padded(**kwargs: Any) -> bytes:
        """Pad valid fixture documents without changing their declared edges."""
        body = json.loads(document(**kwargs))
        body["padding"] = "z" * 140000
        return json.dumps(body).encode()

    manifest = padded(config=edge(config), layers=[edge(layer)])
    nested = padded(children=[edge(manifest, OCI_MANIFEST)])
    root = padded(children=[edge(nested, OCI_INDEX)])
    objects = {address(raw): raw for raw in (config, layer, manifest, nested, root)}
    console, registry = ArtifactConsole(width=200), ObjectRegistry(root, objects)
    result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exit_code == 0, result.exception
    for raw in (config, layer, manifest, nested):
        short = address(raw)[:11] + "..."
        for size in ("64.0 KiB", "128.0 KiB"):
            assert any(short in row and f"Downloading {size}" in row
                       for frame in console.terminal.frames for row in frame)
    root_receives = [
        row for frame in console.terminal.frames for row in frame
        if row.startswith("Manifest 1 ") and "Downloading" in row and "sha256:" not in row
    ]
    assert any("128.0 KiB" in row and "total unknown" in row for row in root_receives)
    assert not any("%" in row or "remaining" in row for row in root_receives)
    assert len(_rows(console)) == len(objects)


@_NATIVE
@pytest.mark.parametrize("failure", ["missing", "corrupt", "unsupported"])
@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
def test_graph_failures_keep_no_complete_output_or_success_record(
    runner: CliRunner, tmp_path: Path, failure: str, output_format: str,
) -> None:
    """Missing/corrupt required content and unsupported documents fail closed through the actual graph."""
    root, objects = _graph("nested", "sha256")
    registry, console = ObjectRegistry(root, objects), ArtifactConsole(width=200)
    original = registry.respond
    config_digest = next(digest for digest, raw in objects.items() if raw == b'{"opaque":"config"}')

    def respond(method: str, url: str, **kwargs: Any) -> StreamResponse:
        """Inject one bad HTTP body/status, not a mocked graph or storage result."""
        if failure == "unsupported" and url.endswith("/moving"):
            return registry.response(200, document(children=[], media_type="application/vnd.unsupported"))
        if url.endswith("/" + config_digest):
            return registry.response(404) if failure == "missing" else registry.response(200, b"x" * len(objects[config_digest]))
        return original(method, url, **kwargs)

    with patch.object(registry, "respond", side_effect=respond):
        result, _ = _invoke(runner, console, registry, tmp_path / "out", ["--format", output_format])
    assert result.exit_code == 1
    assert console.out.text == "" and "Error [" in console.err.text
    assert any("Failed" in row for row in _rows(console))
    assert not list(tmp_path.iterdir())
    assert all(response.close_calls == 1 for response in registry.responses)


@_NATIVE
@pytest.mark.parametrize("limit", _LIMITS)
def test_command_forwards_each_budget_to_the_real_operation(
    runner: CliRunner, tmp_path: Path, limit: str,
) -> None:
    """A positive but insufficient budget fails at its real read/graph/packaging boundary."""
    root, objects = _graph("nested", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    result, _ = _invoke(runner, console, registry, tmp_path / "out", [
        "--format", "oci-tar-gzip", limit, "1",
    ])
    assert result.exit_code == 1 and console.out.text == ""
    assert not list(tmp_path.iterdir())
    assert "Error [" in console.err.text
    if limit in ("--max-archive-bytes", "--max-archive-input-bytes"):
        assert all("Downloaded" in row for row in _rows(console))
        assert "Packaging OCI archive ... failed" in console.err.text
        assert "Publishing artifact" not in console.err.text


@_NATIVE
@pytest.mark.parametrize("fault", ["cancel", "timeout", "rate-limit", "terminal"])
def test_network_cancel_and_terminal_failures_preserve_error_and_cleanup(
    runner: CliRunner, tmp_path: Path, fault: str,
) -> None:
    """Cancellation aborts, actual transport failures report nonzero, and terminal exceptions retain identity."""
    root, objects = _graph("simple", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    original = registry.respond
    primary = OSError("synthetic terminal sink fault")
    if fault == "terminal":
        console.err.failure = primary
        console.err.failure_when = lambda text: "Downloading" in text

    def respond(method: str, url: str, **kwargs: Any) -> StreamResponse:
        """Fail after a root was stored, so completed rows and an active blob exercise different states."""
        if "/blobs/" in url and fault != "terminal":
            if fault == "cancel":
                raise KeyboardInterrupt
            if fault == "timeout":
                raise requests.Timeout("synthetic timeout")
            return registry.response(429)
        return original(method, url, **kwargs)

    with patch.object(registry, "respond", side_effect=respond):
        result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exit_code == 1
    if fault == "terminal":
        assert result.exception is primary
    if fault == "cancel":
        assert isinstance(result.exception, SystemExit)
        assert "Aborted!" in console.err.text
    assert any(("Cancelled" if fault == "cancel" else "Failed") in row for row in _rows(console))
    assert console.out.text == "" and not list(tmp_path.iterdir())
    assert all(response.close_calls == 1 for response in registry.responses)


@_NATIVE
@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
@pytest.mark.parametrize("fault", ["stdout", "diagnostic", "final-terminal"])
def test_failure_after_publication_retains_complete_output(
    runner: CliRunner, tmp_path: Path, output_format: str, fault: str,
) -> None:
    """A failing result/diagnostic/phase sink is nonzero but cannot delete the committed artifact."""
    root, objects = _graph("nested", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    primary = OSError("synthetic post-publication output failure")
    options = ["--format", output_format]
    if fault == "stdout":
        console.out.failure = primary
    else:
        console.err.failure = primary
        if fault == "diagnostic":
            console.err.failure_when = lambda text: "telemetry" in text
            options.append("--time-scenarios")
        else:
            console.err.failure_when = lambda text: "Publishing artifact ... done" in text
    output = tmp_path / "out"
    result, _ = _invoke(runner, console, registry, output, options)
    assert result.exit_code == 1 and result.exception is primary
    assert console.out.text == ""
    with snapshot_layout(output) as snapshot:
        assert snapshot.graph.roots[0].digest == address(root)
        assert {obj.digest for obj in snapshot.graph.objects} == objects.keys()
    assert not list(tmp_path.glob(".regshape-*"))


class FaultLog(io.StringIO):
    """A synthetic owned telemetry file that can fail writes, flushes or close after publication."""

    def __init__(self, fault: str, error: BaseException) -> None:
        """Choose one sink boundary while preserving error identity and real string buffering."""
        super().__init__()
        self.fault, self.error = fault, error
        self.close_attempted = False

    def write(self, text: str) -> int:
        """Raise the configured write failure; otherwise retain diagnostic bytes."""
        if self.fault == "write":
            raise self.error
        return super().write(text)

    def flush(self) -> None:
        """Raise a configured flush failure instead of claiming buffered diagnostics reached storage."""
        if self.fault == "flush":
            raise self.error
        super().flush()

    def close(self) -> None:
        """Record ownership cleanup, raising the configured close error only once."""
        self.close_attempted = True
        if self.fault == "close":
            self.fault = ""
            raise self.error
        if self.fault == "flush":
            self.fault = ""
            raise self.error
        super().close()


@_NATIVE
@pytest.mark.parametrize("fault", ["write", "flush", "close"])
def test_log_failure_after_publication_precedes_success_json(
    runner: CliRunner, tmp_path: Path, fault: str,
) -> None:
    """Owned telemetry file errors are not hidden by the result writer, even with progress disabled."""
    root, objects = _graph("simple", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    primary = OSError("synthetic log failure")
    log = FaultLog(fault, primary)
    real_open = open
    log_path, output = tmp_path / "log", tmp_path / "out"

    def open_log(path: Any, *args: Any, **kwargs: Any) -> Any:
        """Replace only the selected diagnostic sink, leaving all actual storage operations intact."""
        return log if path == str(log_path) else real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=open_log):
        result, _ = _invoke(runner, console, registry, output, ["--json", "--time-scenarios"],
                            global_options=["--log-file", str(log_path)])
    assert result.exit_code == 1 and result.exception is primary
    assert console.out.text == "" and log.close_attempted
    with snapshot_layout(output) as snapshot:
        assert snapshot.graph.roots[0].digest == address(root)
    log.close()


@_NATIVE
def test_secondary_diagnostics_do_not_replace_primary_http_callback_error(
    runner: CliRunner, tmp_path: Path,
) -> None:
    """A transport-boundary caller exception survives scenario diagnostics and display cleanup failures."""
    root, objects = _graph("simple", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    primary = RuntimeError("synthetic primary callback failure")
    console.err.failure = OSError("synthetic secondary diagnostic failure")
    console.err.failure_when = lambda text: "telemetry" in text
    with patch.object(registry, "respond", side_effect=primary):
        result, _ = _invoke(runner, console, registry, tmp_path / "out", ["--time-scenarios"])
    assert result.exception is primary and result.exit_code == 1
    assert console.out.text == "" and not list(tmp_path.iterdir())
    assert any("Failed" in row for row in _rows(console))


@_NATIVE
@pytest.mark.parametrize("structured_telemetry", [False, True])
def test_diagnostics_and_log_never_contain_progress_controls(
    runner: CliRunner, tmp_path: Path, structured_telemetry: bool,
) -> None:
    """Diagnostics and existing logging coordinate around rows; JSON/file channels contain no control sequences."""
    root, objects = _graph("nested", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    handler = logging.StreamHandler(console.err)
    test_logger = logging.getLogger("artifact-command-fixture")
    old_level = test_logger.level
    test_logger.setLevel(logging.WARNING)
    test_logger.addHandler(handler)
    original = registry.respond

    def respond(method: str, url: str, **kwargs: Any) -> StreamResponse:
        """Write an ordinary warning during transfer to exercise the production logging redirection."""
        test_logger.warning("synthetic registry warning")
        return original(method, url, **kwargs)

    log_path = tmp_path / "telemetry.log"
    options = ["--debug-calls", "--time-scenarios", "--time-methods", "--metrics"]
    if structured_telemetry:
        options += ["--telemetry-format", "json"]
    try:
        with patch.object(registry, "respond", side_effect=respond):
            result, _ = _invoke(runner, console, registry, tmp_path / "out", options,
                                global_options=["--log-file", str(log_path)])
    finally:
        test_logger.removeHandler(handler)
        test_logger.setLevel(old_level)
    assert result.exit_code == 0, result.exception
    assert "\x1b" not in log_path.read_text() and "\x1b" not in console.out.text
    assert "synthetic registry warning" in console.err.text
    if structured_telemetry:
        assert "\x1b" not in console.err.text and not _rows(console)
    else:
        assert len(_rows(console)) == len(objects)
        assert all("Downloaded" in row for row in _rows(console))
    assert handler.stream is console.err


@_NATIVE
@pytest.mark.parametrize("foreign", [False, True])
def test_registry_external_token_realm_and_foreign_challenge_guard(
    runner: CliRunner, tmp_path: Path, wire: WireAdapter, foreign: bool,
) -> None:
    """Real Requests redirects allow registry-issued external realms but reject foreign-origin challenges."""
    config = b"{}"
    root = document(config=edge(config))
    root_url = BASE + "/v2/source/manifests/moving"
    blob_url = BASE + "/v2/source/blobs/" + address(config)

    def registry_request(request: requests.PreparedRequest) -> WireResponse:
        """Issue the challenge only from the actual registry, then serve the root after authentication."""
        if "Authorization" not in request.headers:
            return WireResponse(request, 401, headers={"WWW-Authenticate": challenge(TOKEN_ORIGIN)})
        return WireResponse(request, 200, root)

    wire.routes[root_url] = registry_request
    wire.reply(TOKEN_ORIGIN + "/token", 200, b'{"token":"synthetic-token"}')
    wire.reply(blob_url, 307, headers={"Location": CDN + "/config?signature=synthetic"})
    wire.reply(CDN + "/config", 401 if foreign else 200, b"" if foreign else config,
               {"WWW-Authenticate": challenge("https://foreign.example")} if foreign else {})
    console, output = ArtifactConsole(), tmp_path / "out"
    with (
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=("synthetic-user", "synthetic-pass")),
        patch("regshape.cli.artifact.ProgressDisplay", side_effect=console.artifact_display),
    ):
        result = runner.invoke(regshape, ["artifact", "pull", "-a", _SOURCE, "-o", str(output)])
    assert result.exit_code == int(foreign), result.exception
    assert len([item for item in wire.sent if _origin(item.url) == _origin(TOKEN_ORIGIN)]) == 1
    assert all("Authorization" not in item.headers for item in wire.sent if _origin(item.url) == _origin(CDN))
    assert all(_origin(item.url) != _origin("https://foreign.example") for item in wire.sent)
    assert "synthetic-pass" not in console.err.text + console.out.text
    if foreign:
        assert "response origin" in console.err.text and not list(tmp_path.iterdir())
        assert console.out.text == ""
    else:
        assert output.is_dir() and len(_rows(console)) == 2
        assert all("Downloaded" in row for row in _rows(console))


@_NATIVE
@pytest.mark.parametrize("output_format", list(ArtifactFormat))
@pytest.mark.parametrize("fail_at", [None, ArtifactPhase.PACKAGING, ArtifactPhase.PUBLISHING])
def test_phase_callback_announces_actual_local_work_and_preserves_errors(
    tmp_path: Path, output_format: ArtifactFormat, fail_at: ArtifactPhase | None,
) -> None:
    """The additive terminal-free phase hook reports no fake byte progress and aborts before announced work."""
    root, objects = _graph("simple", "sha256")
    registry, seen = ObjectRegistry(root, objects), []
    primary = RuntimeError("synthetic phase observer failure")
    output = tmp_path / "out"

    def phase(event: ArtifactPhase) -> None:
        """Check that the namespace is still private at each phase boundary and optionally fail."""
        seen.append(event)
        assert not output.exists()
        if event == fail_at:
            raise primary

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request", side_effect=registry.respond),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example"))
        fails = fail_at == ArtifactPhase.PUBLISHING or (
            fail_at == ArtifactPhase.PACKAGING and output_format == ArtifactFormat.OCI_TAR_GZIP
        )
        if fails:
            with pytest.raises(RuntimeError) as caught:
                pull_artifact(client, "source", "moving", output, output_format=output_format, phase_callback=phase)
            assert caught.value is primary and not list(tmp_path.iterdir())
        else:
            result = pull_artifact(client, "source", "moving", output, output_format=output_format, phase_callback=phase)
            assert result.root.digest == address(root)
    expected = [ArtifactPhase.PUBLISHING] if output_format == ArtifactFormat.OCI else [
        ArtifactPhase.PACKAGING, ArtifactPhase.PUBLISHING,
    ]
    assert seen == (expected[:1] if fail_at == ArtifactPhase.PACKAGING else expected)


@_NATIVE
def test_preflight_validates_without_reserving_and_race_is_rejected(tmp_path: Path) -> None:
    """Preflight leaves no destination/reservation; a subsequent library check rejects a newly occupied name."""
    output = tmp_path / "out"
    preflight_artifact_pull("registry.example", "source", "moving", output)
    assert not list(tmp_path.iterdir())
    output.write_bytes(b"racing owner")
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request") as send,
    ):
        client = RegistryClient(TransportConfig(registry="registry.example"))
        with pytest.raises(ArtifactError, match="already exists"):
            pull_artifact(client, "source", "moving", output)
    send.assert_not_called()
    assert output.read_bytes() == b"racing owner"


@pytest.mark.parametrize("value", [0, -1, True, 1.5, None])
def test_library_preflight_policy_values_are_strict(value: Any, tmp_path: Path) -> None:
    """CLI positivity does not weaken the underlying non-boolean integer policy contract."""
    with pytest.raises(ValueError, match="positive integer"):
        ArtifactLimits(max_total_bytes=value)
    assert not list(tmp_path.iterdir())


def test_artifact_attempt_restart_has_no_stale_rate_or_double_completion() -> None:
    """Absolute per-attempt counters reset the clock, while skipped/duplicate observations add no rows."""
    terminal, now = Frames(), [0.0]
    with ProgressDisplay(stream=terminal, columns=lambda: 200, clock=lambda: now[0]) as display:
        initial = _observation(kind="blob", identity="blob", digest=address(b"blob"),
                               phase="transferring", total=100)
        display.observe(initial)
        now[0] = 2
        display.observe(replace(initial, bytes_completed=40))
        assert "20 B/s" in terminal.rows[-1]
        now[0] = 3
        retry = replace(initial, attempt=2)
        display.observe(retry)
        assert "0 B/100 B" in terminal.rows[-1] and "/s" not in terminal.rows[-1]
        now[0] = 5
        display.observe(replace(retry, bytes_completed=10))
        assert "5 B/s" in terminal.rows[-1]
        display.observe(replace(retry, phase="stored", bytes_completed=100))
        display.observe(replace(retry, phase="skipped"))
        assert len(display._finished) == 1
    assert len([row for row in terminal.rows if row]) == 1
    assert "Downloaded 100 B/100 B" in terminal.rows[-2]


@_NATIVE
@pytest.mark.parametrize("structured", [False, True])
def test_partial_summary_error_preserves_bytes_and_complete_artifact(
    runner: CliRunner, tmp_path: Path, structured: bool,
) -> None:
    """A partially written final result is never erased by a late progress repaint or output rollback."""
    root, objects = _graph("simple", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    primary = OSError("synthetic partial stdout failure")
    console.out.failure, console.out.partial = primary, 12
    output = tmp_path / "out"
    result, _ = _invoke(runner, console, registry, output, ["--json"] if structured else [])
    assert result.exception is primary and result.exit_code == 1
    assert len(console.out.text) == 12 and "\x1b" not in console.out.text
    assert console.terminal.rows[-1].endswith(console.out.text.split("\n")[-1])
    assert len(_rows(console)) == (0 if structured else len(objects))
    with snapshot_layout(output) as snapshot:
        assert snapshot.graph.roots[0].digest == address(root)


@_NATIVE
@pytest.mark.parametrize("alias", ["--artifact", "-a", "--image-ref", "-i"])
def test_alias_and_sha512_insecure_command_contract(
    runner: CliRunner, tmp_path: Path, alias: str,
) -> None:
    """The registered command retains shared aliases, strict SHA-512 sources and the global HTTP option."""
    root, objects = _graph("simple", "sha512")
    registry = ObjectRegistry(root, objects, "sha512")
    output = tmp_path / "out"
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request", side_effect=registry.respond) as send,
    ):
        result = runner.invoke(regshape, [
            "--insecure", "artifact", "pull", alias,
            "registry.example/source@" + address(root, "sha512"), "-o", str(output), "--json",
        ])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["root"]["digest"] == address(root, "sha512")
    assert all(_origin(call.args[1]) == _origin("http://registry.example") for call in send.call_args_list)


def test_conflicting_aliases_do_not_construct_a_client(runner: CliRunner, tmp_path: Path) -> None:
    """A conflicting legacy/preferred selector fails before auth rather than choosing an arbitrary source."""
    with patch("regshape.libs.transport.client.resolve_credentials") as credentials:
        result = runner.invoke(regshape, [
            "artifact", "pull", "-a", _SOURCE, "-i", _SOURCE + "different",
            "-o", str(tmp_path / "out"),
        ])
    assert result.exit_code == 2 and "Conflicting reference options" in result.stderr
    credentials.assert_not_called()
    assert not list(tmp_path.iterdir())


class StorageWriteFault:
    """Borrow a real binary file and fail only its write boundary; delegate cleanup unchanged."""

    def __init__(self, stream: Any, error: OSError) -> None:
        """Retain the real opened file and exact simulated disk failure."""
        self.stream, self.error = stream, error

    def write(self, data: bytes) -> int:
        """Simulate a full disk without accepting any payload bytes."""
        raise self.error

    def __getattr__(self, name: str) -> Any:
        """Expose the real file's identity, flush and close for production cleanup."""
        return getattr(self.stream, name)


@_NATIVE
@pytest.mark.parametrize("stage", ["receive", "metadata", "packaging", "publication"])
@pytest.mark.parametrize("structured", [False, True])
def test_operation_owned_filesystem_errors_use_command_diagnostics(
    runner: CliRunner, tmp_path: Path, stage: str, structured: bool,
) -> None:
    """Real command execution formats disk faults while owned cleanup removes incomplete output."""
    root, objects = _graph("simple", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    primary = OSError(errno.ENOSPC, "synthetic disk full")
    real_fdopen = os.fdopen
    real_create = SafeDirectory.create_file
    real_move = NativeFilesystem.move

    def fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        """Keep real file acquisition/identity, injecting failure only at received-body writes."""
        stream = real_fdopen(fd, mode, *args, **kwargs)
        return StorageWriteFault(stream, primary) if stage == "receive" and mode == "wb" else stream

    def create_file(directory: SafeDirectory, name: str) -> SafeFile:
        """Fail selected metadata/archive file creation without replacing the graph or publication operation."""
        if (stage == "metadata" and name == "oci-layout") or (stage == "packaging" and name == "output.tar.gz"):
            raise primary
        return real_create(directory, name)

    def move(native: NativeFilesystem, source_fd: int, source: str, target_fd: int, target: str) -> None:
        """Leave native probes intact and fail only installation of the final caller-selected name."""
        if stage == "publication" and target == "out":
            raise primary
        real_move(native, source_fd, source, target_fd, target)

    options = ["--json"] if structured else []
    if stage == "packaging":
        options += ["--format", "oci-tar-gzip"]
    with (
        patch("os.fdopen", side_effect=fdopen),
        patch.object(SafeDirectory, "create_file", new=create_file),
        patch.object(NativeFilesystem, "move", new=move),
    ):
        result, send = _invoke(runner, console, registry, tmp_path / "out", options)
    assert send.called and result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert f"Error [{_SOURCE}]: [Errno {errno.ENOSPC}] synthetic disk full" in console.err.text
    assert "Traceback" not in console.err.text and console.out.text == ""
    assert not list(tmp_path.iterdir())


@_NATIVE
@pytest.mark.parametrize("options", [[], ["--json"], ["--no-progress"], ["--telemetry-format", "json"]])
def test_signed_redirect_transport_failure_never_prints_query_credentials(
    runner: CliRunner, tmp_path: Path, wire: WireAdapter, options: list[str],
) -> None:
    """Requests failures from an actual signed redirect emit only a safe type, never their raw URL message."""
    config = b"{}"
    root = document(config=edge(config))
    signed_url = CDN + "/config?signature=synthetic-signed-secret"
    wire.reply(BASE + "/v2/source/manifests/moving", 200, root)
    wire.reply(BASE + "/v2/source/blobs/" + address(config), 307, headers={"Location": signed_url})

    def failed_connection(request: requests.PreparedRequest) -> WireResponse:
        """Fail at the HTTP adapter boundary with the sensitive URL Requests exceptions can carry."""
        raise requests.exceptions.SSLError(f"synthetic TLS connection failure for {request.url}")

    wire.routes[CDN + "/config"] = failed_connection
    console, output, log_path = ArtifactConsole(), tmp_path / "out", tmp_path / "telemetry.log"
    with (
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.cli.artifact.ProgressDisplay", side_effect=console.artifact_display),
    ):
        result = runner.invoke(regshape, [
            "--log-file", str(log_path), "artifact", "pull", "-a", _SOURCE,
            "-o", str(output), "--time-scenarios", *options,
        ])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert f"Error [{_SOURCE}]: Transport failed (SSLError)" in console.err.text
    assert console.out.text == "" and not output.exists()
    diagnostics = console.err.text + log_path.read_text()
    assert "synthetic-signed-secret" not in diagnostics and signed_url not in diagnostics
    assert "Traceback" not in diagnostics
    assert any(_origin(item.url) == _origin(CDN) for item in wire.sent)
    assert not list(tmp_path.glob(".regshape-*"))


@_NATIVE
def test_request_exception_from_progress_sink_preserves_identity(runner: CliRunner, tmp_path: Path) -> None:
    """The operation error formatter must not capture a same-typed error raised by the observer sink."""
    root, objects = _graph("simple", "sha256")
    console, registry = ArtifactConsole(), ObjectRegistry(root, objects)
    primary = requests.ConnectionError("synthetic observer sink failure")
    console.err.failure = primary
    console.err.failure_when = lambda text: "Downloading" in text
    result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exit_code == 1 and result.exception is primary
    assert console.out.text == "" and not list(tmp_path.iterdir())

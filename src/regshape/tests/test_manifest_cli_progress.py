"""Visible manifest frames and real command/library flows with synthetic HTTP and terminals."""

import gzip
import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import IO, Literal
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner, Result

from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.progress import (
    ManifestKind,
    ManifestPhase,
    ManifestProgress,
    TransferDirection,
    TransferPhase,
    TransferProgress,
)
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - shared Click compatibility fixture
from regshape.tests.test_manifest_cli import _INDEX_JSON, _MANIFEST_JSON
from regshape.tests.test_manifest_progress import StreamResponse
from regshape.tests.test_progress_display import Terminal

REFERENCE = "registry.example.invalid/team/package:v1"
DIGEST = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def reset_telemetry() -> Iterator[None]:
    """Keep global telemetry independent of neighboring command invocations."""
    previous = get_telemetry_config()
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(previous)


class Frames(Terminal):
    """Record every visible terminal frame while retaining the existing ANSI emulator."""

    def __init__(self) -> None:
        """Start with an interactive terminal and an empty frame history."""
        super().__init__()
        self.frames: list[list[str]] = []

    def write(self, text: str) -> int:
        """Capture the screen after each real write, including control sequences."""
        result = super().write(text)
        self.frames.append(list(self.rows))
        return result


class Channel(io.TextIOBase, IO[str]):
    """Capture one output channel while optionally sharing a physical terminal."""

    def __init__(self, terminal: Frames | None, *, interactive: bool = True) -> None:
        """Borrow terminal state; keep captured bytes and one-shot faults channel-local."""
        super().__init__()
        self.terminal = terminal
        self.interactive = interactive
        self.writes: list[str] = []
        self.failure: BaseException | None = None
        self.failure_when: Callable[[str], bool] = bool
        self.partial = 0
        self.before_failure: Callable[[], None] | None = None

    @property
    def text(self) -> str:
        """Return exactly what this channel wrote, independent of screen interpretation."""
        return "".join(self.writes)

    def writable(self) -> bool:
        """Expose a text output sink to Click."""
        return True

    def isatty(self) -> bool:
        """Control stderr suppression independently from stdout redirection."""
        return self.interactive and self.terminal is not None

    def write(self, text: str) -> int:
        """Write normally or publish a prefix before raising a configured one-shot error."""
        if not isinstance(text, str):
            raise TypeError("Text channel requires str")
        error = self.failure if self.failure_when(text) else None
        if error is not None:
            self.failure = None
            text = text[: self.partial]
        self.writes.append(text)
        if self.terminal is not None:
            self.terminal.write(text)
        if error is not None:
            if self.before_failure is not None:
                self.before_failure()
            raise error
        return len(text)

    def flush(self) -> None:
        """Flush the borrowed terminal only while it remains open."""
        if self.terminal is not None and not self.terminal.closed:
            self.terminal.flush()


class DisplayScope:
    """Install shared test streams inside Click isolation and restore them before it exits."""

    def __init__(self, display: ProgressDisplay, out: Channel, err: Channel) -> None:
        """Borrow the production display and channel sinks without subclassing the final class."""
        self.display = display
        self.out = out
        self.err = err
        self.stdout_patch = patch("sys.stdout", out)
        self.stderr_patch = patch("sys.stderr", err)
        self.old_output = display.config.output

    def __enter__(self) -> ProgressDisplay:
        """Bind diagnostics and stdout to the same simulated console when requested."""
        self.stdout_patch.start()
        self.stderr_patch.start()
        self.display.config.output = self.err
        try:
            return self.display.__enter__()
        except BaseException:
            self.display.config.output = self.old_output
            self.stderr_patch.stop()
            self.stdout_patch.stop()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Run production cleanup before restoring Click's captures, preserving exceptions."""
        try:
            return self.display.__exit__(exc_type, exc, traceback)
        finally:
            self.display.config.output = self.old_output
            self.stderr_patch.stop()
            self.stdout_patch.stop()


class Console:
    """Create production displays with deterministic clocks and separately captured channels."""

    def __init__(
        self, *, shared: bool = True, interactive: bool = True, width: int = 160
    ) -> None:
        """Choose shared/redirected stdout and terminal width without changing command behavior."""
        self.terminal = Frames()
        self.terminal.width = width
        self.out = Channel(self.terminal if shared else None)
        self.err = Channel(self.terminal, interactive=interactive)
        self.displays: list[ProgressDisplay] = []
        self.time = 0.0

    def clock(self) -> float:
        """Advance fake time so integration tests expose intermediate reads without sleeping."""
        self.time += 0.2
        return self.time

    def display(self, *, disabled: bool) -> DisplayScope:
        """Wrap a real display only to bind synthetic streams after Click enters isolation."""
        display = ProgressDisplay(
            disabled=disabled,
            stream=self.err,
            columns=lambda: self.terminal.width,
            clock=self.clock,
        )
        self.displays.append(display)
        return DisplayScope(display, self.out, self.err)


def event(
    phase: ManifestPhase,
    count: int = 0,
    total: int | None = 100,
    *,
    identity: str = "opaque",
    kind: ManifestKind = ManifestKind.MANIFEST,
) -> ManifestProgress:
    """Build valid manifest snapshots for renderer-only lifecycle tests."""
    return ManifestProgress(identity, kind, phase, count, total)


def invoke(
    runner: CliRunner,
    console: Console,
    response: requests.Response,
    options: list[str] | None = None,
    *,
    reference: str = REFERENCE,
) -> tuple[Result, MagicMock]:
    """Execute the actual command, library and transport with only HTTP/credentials mocked."""
    with (
        patch(
            "regshape.libs.transport.client.resolve_credentials",
            return_value=(None, None),
        ),
        patch("requests.request", return_value=response) as send,
        patch("regshape.cli.manifest.ProgressDisplay", side_effect=console.display),
    ):
        result = runner.invoke(
            regshape, ["manifest", "get", "--artifact", reference, *(options or [])]
        )
    return result, send


def test_received_is_not_completion_and_kind_updates_one_row() -> None:
    """One opaque identity survives kind discovery and all command-owned stages."""
    terminal = Frames()
    with ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
        display.observe(
            event(ManifestPhase.PREPARING, total=None, kind=ManifestKind.UNKNOWN)
        )
        display.observe(event(ManifestPhase.TRANSFERRING, 50, kind=ManifestKind.INDEX))
        display.observe(event(ManifestPhase.RECEIVED, 100, kind=ManifestKind.INDEX))
        assert (
            "Received" in terminal.rows[-1] and "Downloaded" not in terminal.getvalue()
        )
        with pytest.raises(RuntimeError):
            display.observe(event(ManifestPhase.PREPARING, identity="concurrent"))
        display.processing_manifest()
        assert "Processing" in terminal.rows[-1]
        display.writing_manifest()
        assert "Writing" in terminal.rows[-1]
        display.finish_manifest()
    rows = [row for row in terminal.rows if row]
    assert len(rows) == 1 and rows[0].startswith("Index 1 Downloaded")
    assert display.completed == display.skipped == 0


def test_bare_receipt_never_auto_completes() -> None:
    """Leaving a display without explicit output completion retains only the neutral receipt."""
    terminal = Frames()
    with ProgressDisplay(stream=terminal) as display:
        display.observe(event(ManifestPhase.RECEIVED, 100))
    assert "Received" in terminal.getvalue() and "Downloaded" not in terminal.getvalue()


@pytest.mark.parametrize("stage", ["missing", "preparing", "received", "processing"])
def test_completion_requires_writing(stage: str) -> None:
    """Missing or premature completion raises instead of fabricating command success."""
    with ProgressDisplay(disabled=True) as display:
        if stage == "preparing":
            display.observe(event(ManifestPhase.PREPARING))
        elif stage in ("received", "processing"):
            display.observe(event(ManifestPhase.RECEIVED, 100))
            if stage == "processing":
                display.processing_manifest()
        with pytest.raises(RuntimeError):
            display.finish_manifest()


def test_manifest_namespace_preserves_blob_counts_and_deduplication() -> None:
    """A manifest ID equal to a blob digest is still independent and does not change blob counts."""
    terminal = Frames()
    blob = TransferProgress(
        "blob-id", DIGEST, TransferDirection.DOWNLOAD, TransferPhase.COMPLETED, 2, 2
    )
    with ProgressDisplay(
        stream=terminal, total_blobs=2, show_counts=True, columns=lambda: 160
    ) as display:
        display.observe(blob)
        display.observe(event(ManifestPhase.RECEIVED, 100, identity=DIGEST))
        display.writing_manifest()
        display.finish_manifest()
        display.observe(blob)
        display.observe(
            replace(
                blob,
                digest="sha256:" + "b" * 64,
                phase=TransferPhase.SKIPPED,
                bytes_completed=0,
            )
        )
    assert display.completed == 1 and display.skipped == 1
    assert len([row for row in terminal.rows if row]) == 3
    assert any(row.startswith("Manifest 1 Downloaded") for row in terminal.rows)
    assert any(row.startswith("Blob 2/2") for row in terminal.rows)


def test_meaningful_rates_and_unknown_total_changes() -> None:
    """Known sizes show percentage/remaining time; invalidated totals immediately remove them."""
    terminal = Frames()
    now = [0.0]
    with ProgressDisplay(
        stream=terminal, clock=lambda: now[0], columns=lambda: 160
    ) as display:
        display.observe(event(ManifestPhase.TRANSFERRING, 0, 131072))
        now[0] = 2.0
        display.observe(event(ManifestPhase.TRANSFERRING, 65536, 131072))
        assert "50%" in terminal.rows[-1] and "32.0 KiB/s" in terminal.rows[-1]
        assert "~2s remaining" in terminal.rows[-1]
        display.observe(event(ManifestPhase.TRANSFERRING, 65536, None))
        assert "total unknown" in terminal.rows[-1]
        assert "%" not in terminal.rows[-1] and "remaining" not in terminal.rows[-1]


def test_manifest_routine_redraws_are_bounded_and_final_state_is_forced() -> None:
    """At most ten routine redraws per second, plus the initial frame, survive rapid byte updates."""
    terminal = Frames()
    now = [0.0]
    with ProgressDisplay(
        stream=terminal, clock=lambda: now[0], columns=lambda: 160
    ) as display:
        display.observe(event(ManifestPhase.TRANSFERRING, 0, 100))
        for count in range(1, 101):
            now[0] = count / 100
            display.observe(event(ManifestPhase.TRANSFERRING, count, 100))
        draws = [
            frame for frame in terminal.frames if frame and "Downloading" in frame[-1]
        ]
        assert 1 <= len(draws) <= 11
        display.observe(event(ManifestPhase.RECEIVED, 100, 100))
        assert "Received 100 B/100 B" in terminal.rows[-1]
        display.writing_manifest()
        display.finish_manifest()
    assert "Downloaded 100 B/100 B" in terminal.rows[-2]


@pytest.mark.parametrize("total", [None, 0])
def test_empty_unknown_and_zero_totals_are_honest(total: int | None) -> None:
    """A zero-byte response never divides by zero or invents a rate/percentage."""
    terminal = Frames()
    with ProgressDisplay(stream=terminal) as display:
        display.observe(event(ManifestPhase.TRANSFERRING, 0, total))
        assert "%" not in terminal.rows[-1] and "/s" not in terminal.rows[-1]
        display.observe(event(ManifestPhase.RECEIVED, 0, total))
        display.writing_manifest()
        display.finish_manifest()
    assert "Downloaded 0 B" in terminal.rows[-2]


@pytest.mark.parametrize(
    "body,media,options",
    [
        (_MANIFEST_JSON, "application/vnd.oci.image.manifest.v1+json", []),
        (_INDEX_JSON, "application/vnd.oci.image.index.v1+json", []),
        ("raw text\nsecond line", "application/vnd.example.raw", ["--raw"]),
        (
            _MANIFEST_JSON,
            "application/vnd.oci.image.manifest.v1+json",
            ["--part", "annotations"],
        ),
    ],
)
@pytest.mark.parametrize("shared", [False, True])
def test_real_command_output_and_final_row(
    runner: CliRunner,
    body: str,
    media: str,
    options: list[str],
    shared: bool,
) -> None:
    """The real library drives receipt; output bytes remain clean and the final row is unique."""
    console = Console(shared=shared)
    payload = body.encode()
    response = StreamResponse(
        payload, headers={"Content-Type": media, "Content-Length": str(len(payload))}
    )
    result, send = invoke(runner, console, response, options)
    assert result.exit_code == 0, result.exception
    assert send.call_count == 1 and send.call_args.kwargs["stream"] is True
    assert response.close_calls == 1 and response.content_reads == 0
    if "--raw" in options:
        assert console.out.text == body + "\n"
    elif "--part" in options:
        assert json.loads(console.out.text) == json.loads(body)["annotations"]
        assert (
            console.out.text
            == json.dumps(json.loads(body)["annotations"], indent=2) + "\n"
        )
    else:
        assert json.loads(console.out.text) == json.loads(body)
    assert "\x1b" not in console.out.text
    rows = [
        row for row in console.terminal.rows if row.startswith(("Manifest ", "Index "))
    ]
    assert len(rows) == 1 and "Downloaded" in rows[0] and "Blob" not in rows[0]
    assert console.displays[0].completed == console.displays[0].skipped == 0


def test_large_index_has_live_frames_before_eof(runner: CliRunner) -> None:
    """A real multi-chunk index produces intermediate bytes before parsing/output begins."""
    data = json.loads(_INDEX_JSON)
    data["annotations"] = {"large": "x" * 180000}
    payload = json.dumps(data).encode()
    response = StreamResponse(
        payload,
        headers={
            "Content-Type": "application/vnd.oci.image.index.v1+json",
            "Content-Length": str(len(payload)),
        },
    )
    console = Console(shared=False)
    result, _ = invoke(
        runner,
        console,
        response,
        reference=f"registry.example.invalid/team/package@{DIGEST}",
    )
    assert result.exit_code == 0, result.exception
    frames = ["\n".join(frame) for frame in console.terminal.frames]
    assert any("Downloading 64.0 KiB/" in frame for frame in frames)
    assert any("Downloading 128.0 KiB/" in frame for frame in frames)
    assert any("Received" in frame and "Downloaded" not in frame for frame in frames)
    assert json.loads(console.out.text) == data


@pytest.mark.parametrize("compressed", [False, True])
def test_command_unknown_total_counts_actual_decoded_payload(
    runner: CliRunner, compressed: bool
) -> None:
    """Missing and compressed lengths stay unknown while the real command receives the full body."""
    payload = _MANIFEST_JSON.encode()
    headers = {"Content-Type": "application/vnd.oci.image.manifest.v1+json"}
    raw_headers = None
    wire = payload
    if compressed:
        wire = gzip.compress(payload)
        headers.update({"Content-Encoding": "gzip", "Content-Length": str(len(wire))})
        raw_headers = {"Content-Encoding": "gzip"}
    console = Console(shared=False)
    result, _ = invoke(
        runner, console, StreamResponse(wire, headers=headers, raw_headers=raw_headers)
    )
    assert result.exit_code == 0, result.exception
    assert json.loads(console.out.text) == json.loads(_MANIFEST_JSON)
    frames = ["\n".join(frame) for frame in console.terminal.frames]
    assert any("total unknown" in frame for frame in frames)
    assert not any("%" in frame or "remaining" in frame for frame in frames)


def test_custom_accept_and_legacy_help_are_retained(runner: CliRunner) -> None:
    """Adding progress neither changes negotiation nor removes/deprecates the preferred artifact flag."""
    console = Console()
    result, send = invoke(
        runner,
        console,
        StreamResponse(b"raw"),
        ["--raw", "--accept", "application/vnd.example.raw"],
    )
    assert result.exit_code == 0
    assert send.call_args.kwargs["headers"]["Accept"] == "application/vnd.example.raw"
    help_result = runner.invoke(regshape, ["manifest", "get", "--help"])
    assert "--no-progress" in help_result.stdout
    assert "--artifact" in help_result.stdout and "--image-ref" in help_result.stdout
    assert "Deprecated in 0.2" in help_result.stdout


@pytest.mark.parametrize(
    "options,interactive",
    [
        (["--no-progress"], True),
        ([], False),
        (["--telemetry-format", "json"], True),
    ],
)
def test_suppression_keeps_streaming_and_clean_output(
    runner: CliRunner,
    options: list[str],
    interactive: bool,
) -> None:
    """Opt-out, nonterminal stderr and structured telemetry do not switch consumption behavior."""
    console = Console(interactive=interactive)
    response = StreamResponse(_MANIFEST_JSON.encode())
    result, send = invoke(runner, console, response, options)
    assert result.exit_code == 0
    assert send.call_args.kwargs["stream"] is True and response.content_reads == 0
    assert console.err.text == ""
    assert json.loads(console.out.text)["schemaVersion"] == 2


@pytest.mark.parametrize("options", [[], ["--raw"], ["--part", "annotations"]])
def test_output_file_has_no_progress_controls(
    runner: CliRunner,
    tmp_path: Path,
    options: list[str],
) -> None:
    """File helpers finish and close before success; neither stdout nor file receives controls."""
    output = tmp_path / "manifest.json"
    console = Console()
    result, _ = invoke(
        runner,
        console,
        StreamResponse(_MANIFEST_JSON.encode()),
        [*options, "--output", str(output)],
    )
    assert result.exit_code == 0, result.exception
    assert console.out.text == "" and "\x1b" not in output.read_text()
    assert "Downloaded" in console.terminal.rows[-2]


@pytest.mark.parametrize(
    "body,options,status",
    [
        ("bad JSON", [], 1),
        (_MANIFEST_JSON, ["--part", "subject"], 2),
        (_INDEX_JSON, ["--part", "config"], 2),
    ],
)
def test_processing_errors_are_failed_not_cancelled(
    runner: CliRunner,
    body: str,
    options: list[str],
    status: int,
) -> None:
    """Successful receipt must not finalize a parsing or field-selection failure."""
    console = Console()
    result, _ = invoke(runner, console, StreamResponse(body.encode()), options)
    assert result.exit_code == status
    assert console.out.text == ""
    assert "Failed" in console.terminal.rows[-2]
    assert "Downloaded" not in console.err.text and "Cancelled" not in console.err.text
    assert "Error [" in result.stderr


@pytest.mark.parametrize("status", [404, 500])
def test_http_error_uses_the_same_failed_row(runner: CliRunner, status: int) -> None:
    """Real library errors terminate the existing row before the standard command diagnostic."""
    console = Console()
    response = StreamResponse(
        b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"missing"}]}', status
    )
    result, _ = invoke(runner, console, response)
    assert result.exit_code == 1 and console.out.text == ""
    assert response.close_calls == 1
    assert (
        "Failed" in console.terminal.rows[-2] and "Downloaded" not in console.err.text
    )
    assert "Error [" in result.stderr


@pytest.mark.parametrize(
    "error",
    [requests.exceptions.ChunkedEncodingError("stream failed"), KeyboardInterrupt()],
)
def test_stream_failure_or_interrupt_cleans_up_without_result(
    runner: CliRunner,
    error: BaseException,
) -> None:
    """Actual command-to-library cancellation/failure closes its response and never writes a result."""
    console = Console()
    response = StreamResponse()
    with patch.object(response, "iter_content", side_effect=error):
        result, _ = invoke(runner, console, response)
    assert (
        result.exit_code != 0 and console.out.text == "" and response.close_calls == 1
    )
    assert (
        "Cancelled" if isinstance(error, KeyboardInterrupt) else "Failed"
    ) in console.terminal.rows[-2]
    assert "Downloaded" not in console.err.text


@pytest.mark.parametrize("width", [12, 24, 80])
def test_stdout_success_after_terminal_resize_preserves_payload(
    runner: CliRunner, width: int
) -> None:
    """Reflowed progress is erased before result output, not by guessing result line counts."""
    console = Console(width=160)
    original_write = console.out.write

    def write(text: str) -> int:
        """Resize only once output is suspended, before writing the result."""
        if text:
            console.terminal.resize(width)
        return original_write(text)

    with patch.object(console.out, "write", side_effect=write):
        result, _ = invoke(
            runner, console, StreamResponse(b"alpha\nbeta\ngamma"), ["--raw"]
        )
    assert result.exit_code == 0
    assert console.out.text == "alpha\nbeta\ngamma\n"
    assert all(value in console.terminal.rows for value in ("alpha", "beta", "gamma"))
    assert all(
        len(row) < width
        for row in console.terminal.rows
        if row.startswith(("Manifest", "#1"))
    )


@pytest.mark.parametrize("error", [OSError("stdout failed"), KeyboardInterrupt()])
@pytest.mark.parametrize("resize", [False, True])
def test_partial_stdout_survives_failure_and_resize(
    runner: CliRunner,
    error: BaseException,
    resize: bool,
) -> None:
    """Never move upward across or erase already-written payload when output stops mid-line."""
    console = Console()
    console.out.failure = error
    console.out.partial = 15
    if resize:
        console.out.before_failure = lambda: console.terminal.resize(12)
    payload = "first line\nsecond line\nthird"
    result, _ = invoke(runner, console, StreamResponse(payload.encode()), ["--raw"])
    assert result.exit_code != 0
    assert console.out.text == payload[:15]
    visible = "\n".join(console.terminal.rows)
    assert "first line" in visible and "seco" in visible
    assert ("Cancel" if isinstance(error, KeyboardInterrupt) else "Failed") in visible
    assert "Downloaded" not in console.err.text and "\x1b" not in console.out.text


@pytest.mark.parametrize("state", ["Downloaded", "Writing"])
def test_render_failure_preserves_payload_and_overwrites_success(
    runner: CliRunner,
    state: str,
) -> None:
    """Recoverable terminal writes cannot leave a success label or erase the result."""
    console = Console()
    failure = OSError("terminal failed")
    console.err.failure = failure
    console.err.failure_when = lambda text: state in text
    console.err.partial = 12
    result, _ = invoke(runner, console, StreamResponse(b"payload"), ["--raw"])
    assert result.exception is failure
    assert any("Failed" in row for row in console.terminal.rows)
    assert not any("Downloaded" in row for row in console.terminal.rows)
    assert console.out.text == ("payload\n" if state == "Downloaded" else "")
    if state == "Downloaded":
        assert "payload" in console.terminal.rows


@pytest.mark.parametrize("boundary", ["label-flush", "newline-write", "newline-flush"])
def test_final_flush_failures_leave_one_failed_row(
    runner: CliRunner, boundary: str
) -> None:
    """Failures after output must repair only the progress row, including a written final newline."""
    console = Console()
    primary = OSError("final terminal failure")
    write = console.err.write
    flush = console.err.flush
    completed = False
    newline = False
    failed = False

    def write_once(text: str) -> int:
        """Track the final label/newline and inject one pre-write newline failure."""
        nonlocal completed, newline, failed
        if "Downloaded" in text:
            completed = True
        if completed and text == "\n" and not failed:
            if boundary == "newline-write":
                failed = True
                raise primary
            newline = True
        return write(text)

    def flush_once() -> None:
        """Fail after a known label or newline write, then allow recovery."""
        nonlocal failed
        if (
            completed
            and not failed
            and (boundary == "label-flush" or (boundary == "newline-flush" and newline))
        ):
            failed = True
            raise primary
        flush()

    with (
        patch.object(console.err, "write", side_effect=write_once),
        patch.object(console.err, "flush", side_effect=flush_once),
    ):
        result, _ = invoke(runner, console, StreamResponse(b"payload"), ["--raw"])
    assert result.exception is primary
    assert console.out.text == "payload\n" and "payload" in console.terminal.rows
    rows = [row for row in console.terminal.rows if row.startswith("Manifest ")]
    assert len(rows) == 1 and "Failed" in rows[0]
    assert console.displays[0].completed == 0


@pytest.mark.parametrize("write_fails", [False, True])
def test_file_cleanup_preserves_the_original_output_error(
    runner: CliRunner,
    write_fails: bool,
) -> None:
    """The result file must close, and its close error must not replace an earlier write failure."""
    console = Console()
    primary = OSError("file write failed")
    secondary = OSError("file close failed")
    file = MagicMock(spec=io.TextIOBase)
    file.__enter__.return_value = file
    file.write.side_effect = primary if write_fails else None
    file.close.side_effect = secondary

    def close_on_exit(*args: object) -> None:
        """Model the existing file context's fallible close operation."""
        file.close()

    file.__exit__.side_effect = close_on_exit
    with patch("regshape.cli.formatting.open", create=True, return_value=file):
        result, _ = invoke(
            runner,
            console,
            StreamResponse(b"payload"),
            ["--raw", "--output", "synthetic-output"],
        )
    assert result.exception is (primary if write_fails else secondary)
    file.close.assert_called_once()
    assert console.out.text == "" and "Failed" in console.terminal.rows[-2]
    assert "Downloaded" not in console.err.text


@pytest.mark.parametrize(
    "flags",
    [
        ["--time-methods"],
        ["--time-scenarios"],
        ["--metrics"],
        ["--debug-calls", "--time-methods", "--time-scenarios", "--metrics"],
    ],
)
def test_telemetry_and_result_share_terminal_safely(
    runner: CliRunner, flags: list[str]
) -> None:
    """Existing diagnostic blocks share the renderer without contaminating stdout or duplicating rows."""
    console = Console()
    result, _ = invoke(runner, console, StreamResponse(b"payload"), ["--raw", *flags])
    assert result.exit_code == 0, result.exception
    assert console.out.text == "payload\n" and "\x1b" not in console.out.text
    assert "payload" in console.terminal.rows
    assert (
        len([row for row in console.terminal.rows if row.startswith("Manifest ")]) == 1
    )
    assert "Downloaded" in console.terminal.rows[-2]


def test_logging_and_log_file_never_receive_progress_controls(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A configured stderr handler coordinates rows while the telemetry log remains plain text."""
    console = Console()
    handler = logging.StreamHandler(console.err)
    log = logging.getLogger("regshape.tests.manifest-command")
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    response = StreamResponse(b"payload")
    original_iterator = response.iter_content

    def chunks(chunk_size: int = 1, decode_unicode: bool = False) -> Iterator[bytes]:
        """Emit an ordinary log message between headers and the real payload read."""
        assert not decode_unicode
        log.warning("synthetic transfer diagnostic")
        yield from original_iterator(chunk_size=chunk_size)

    path = tmp_path / "telemetry.log"
    try:
        with (
            patch(
                "regshape.libs.transport.client.resolve_credentials",
                return_value=(None, None),
            ),
            patch("requests.request", return_value=response),
            patch.object(response, "iter_content", side_effect=chunks),
            patch("regshape.cli.manifest.ProgressDisplay", side_effect=console.display),
        ):
            result = runner.invoke(
                regshape,
                [
                    "--log-file",
                    str(path),
                    "manifest",
                    "get",
                    "-a",
                    REFERENCE,
                    "--raw",
                    "--debug-calls",
                    "--time-methods",
                ],
            )
    finally:
        log.removeHandler(handler)
        handler.close()
    assert result.exit_code == 0, result.exception
    assert console.out.text == "payload\n"
    assert "synthetic transfer diagnostic" in console.terminal.rows
    assert (
        len([row for row in console.terminal.rows if row.startswith("Manifest ")]) == 1
    )
    assert "\x1b" not in path.read_text() and "Manifest 1" not in path.read_text()


def test_scenario_diagnostic_failure_preserves_output_error(runner: CliRunner) -> None:
    """A secondary scenario formatter failure cannot replace the original stdout failure."""
    console = Console()
    primary = OSError("primary stdout")
    console.out.failure = primary
    console.out.partial = 3
    with patch(
        "regshape.libs.decorators.output.print_telemetry_block",
        side_effect=RuntimeError("secondary telemetry"),
    ):
        result, _ = invoke(
            runner, console, StreamResponse(b"payload"), ["--raw", "--time-scenarios"]
        )
    assert result.exception is primary
    assert console.out.text == "pay"
    assert (
        "Failed" in console.terminal.rows[-2] and "Downloaded" not in console.err.text
    )


def test_scenario_failure_preserves_missing_part_exit_code(runner: CliRunner) -> None:
    """Even failed diagnostics must retain the expected missing-part diagnostic and exit 2."""
    console = Console()
    with patch(
        "regshape.libs.decorators.output.print_telemetry_block",
        side_effect=OSError("secondary telemetry"),
    ):
        result, _ = invoke(
            runner,
            console,
            StreamResponse(_MANIFEST_JSON.encode()),
            ["--part", "subject", "--time-scenarios"],
        )
    assert result.exit_code == 2
    assert "manifest has no subject field" in result.stderr
    assert "Failed" in console.terminal.rows[-2]


def test_outer_telemetry_cleanup_preserves_primary(runner: CliRunner) -> None:
    """Command-wrapper cleanup must not replace a primary output failure after display cleanup."""
    console = Console()
    primary = OSError("primary output")
    console.out.failure = primary
    with patch(
        "regshape.libs.decorators.output.flush_telemetry",
        side_effect=RuntimeError("cleanup"),
    ):
        result, _ = invoke(runner, console, StreamResponse(b"payload"), ["--raw"])
    assert result.exception is primary


@pytest.mark.parametrize(
    "flags", [["--time-methods"], ["--metrics"], ["--time-scenarios"]]
)
def test_success_diagnostic_failure_prevents_downloaded(
    runner: CliRunner,
    flags: list[str],
) -> None:
    """Pending method/metric/scenario diagnostics must succeed before the final success row."""
    console = Console()
    error = OSError("telemetry failed")
    with patch(
        "regshape.libs.decorators.output.print_telemetry_block", side_effect=error
    ) as emit:
        result, _ = invoke(
            runner, console, StreamResponse(b"payload"), ["--raw", *flags]
        )
    assert result.exception is error and emit.call_count == 1
    assert console.out.text == "payload\n"
    assert (
        "Failed" in console.terminal.rows[-2] and "Downloaded" not in console.err.text
    )


def test_log_file_closes_even_when_flush_and_command_fail(runner: CliRunner) -> None:
    """Wrapper-owned log cleanup runs after failed flushing without replacing the command error."""
    console = Console()
    primary = OSError("payload failure")
    console.out.failure = primary
    logfile = MagicMock(spec=io.TextIOBase)
    logfile.close.side_effect = OSError("close failed")
    with (
        patch("regshape.libs.decorators.open", create=True, return_value=logfile),
        patch(
            "regshape.libs.decorators.output.flush_telemetry",
            side_effect=RuntimeError("flush failed"),
        ),
        patch(
            "regshape.libs.transport.client.resolve_credentials",
            return_value=(None, None),
        ),
        patch("requests.request", return_value=StreamResponse(b"payload")),
        patch("regshape.cli.manifest.ProgressDisplay", side_effect=console.display),
    ):
        result = runner.invoke(
            regshape,
            [
                "--log-file",
                "synthetic.log",
                "manifest",
                "get",
                "-a",
                REFERENCE,
                "--raw",
            ],
        )
    assert result.exception is primary
    logfile.close.assert_called_once()

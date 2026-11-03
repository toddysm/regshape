"""Screen-frame and command wiring tests for single-row progress rendering."""

import hashlib
import io
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

import pytest
import requests
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.decorators.output import telemetry_write
from regshape.libs.progress import (
    ProgressCallback,
    TransferDirection,
    TransferPhase,
    TransferProgress,
)

DIGEST = "sha256:" + "a" * 64


class Terminal(io.StringIO):
    """Interpret carriage returns and erase-line controls as a real terminal would."""

    def __init__(self, interactive: bool = True) -> None:
        """Track visible rows separately from raw terminal-control writes."""
        super().__init__()
        self.interactive = interactive
        self._rows = [""]
        self.row = 0
        self.column = 0
        self.width = 160
        self.fail_writes = False

    def isatty(self) -> bool:
        """Expose a controlled terminal detection result."""
        return self.interactive

    @property
    def rows(self) -> list[str]:
        """Return the visible frame through the cursor or the last nonempty row."""
        last = max([self.row] + [i for i, row in enumerate(self._rows) if row])
        return self._rows[:last + 1]

    def resize(self, width: int) -> None:
        """Reflow the active logical line as a terminal does when its columns shrink."""
        text = self._rows[self.row]
        chunks = [text[i:i + width] for i in range(0, len(text), width)] or [""]
        self._rows[self.row:self.row + 1] = chunks
        self.row += len(chunks) - 1
        self.column = len(chunks[-1])
        self.width = width

    def write(self, text: str) -> int:
        """Apply each write to the visible frame, preserving raw output for assertions."""
        if self.fail_writes:
            raise OSError("secondary render failure")
        result = super().write(text)
        while text:
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
    if source_kind == "regular":
        path = tmp_path / "source"
        path.write_bytes(b"abcdef")
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
    tmp_path: Path, chunked: bool, error_type: type[BaseException], failure_point: str,
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
        result = CliRunner().invoke(regshape, args)
    assert result.exit_code != 0 and result.stdout == "" and failed
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 1 and "Uploaded" not in rows[0]
    assert ("Failed" if error_type is OSError else "Cancelled") in rows[0]
    assert "scenario" in terminal.getvalue()


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


def test_preparation_label_cannot_inject_terminal_controls() -> None:
    """Caller-supplied preparation text is escaped before it reaches the terminal."""
    terminal = Terminal()
    with ProgressDisplay(stream=terminal, columns=lambda: 160) as display:
        display.prepare("image\n\x1b[31m")
        display.finish_preparation()
    assert "\x1b[31m" not in terminal.getvalue()
    assert len([r for r in terminal.rows if r]) == 1


@pytest.mark.parametrize("chunked,source_kind", [(False, "regular"), (True, "regular"), (True, "pipe")])
@pytest.mark.parametrize("mode", ["tty", "disabled", "structured"])
def test_blob_upload_real_library_consumption(
    upload_source: Path, chunked: bool, source_kind: str, mode: str,
) -> None:
    """Both real upload protocols produce intermediate observations through command wiring."""
    data = b"abcdef"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    observations: list[TransferProgress] = []
    methods: list[str] = []
    payloads: list[bytes] = []
    frames: list[list[str]] = []
    terminal, now = Terminal(), [0.0]
    observe = ProgressDisplay.observe

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
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
    ):
        result = CliRunner().invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert b"".join(payloads) == data
    assert json.loads(result.stdout)["digest"] == digest
    assert methods == (["POST", "PATCH", "PATCH", "PATCH", "PUT"] if chunked else ["POST", "PUT"])
    assert any(e.phase == TransferPhase.TRANSFERRING and 0 < e.bytes_completed < 6 for e in observations)
    assert all(
        e.total_bytes == (None if source_kind == "pipe" else 6)
        for e in observations if e.phase != TransferPhase.PREPARING
    )
    assert observations[-1].phase == TransferPhase.COMPLETED and result.stderr == ""
    if mode == "tty":
        assert any("Uploading 2 B" in row for frame in frames for row in frame)
        assert len([row for row in terminal.rows if row.startswith("Blob")]) == 1
        assert "Uploaded" in terminal.rows[0]
    else:
        assert terminal.getvalue() == ""


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_blob_upload_stream_wiring_preserves_json(tmp_path: Path, chunked: bool, disabled: bool) -> None:
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
        result = CliRunner().invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["size"] == 6 and len(seen) == 2
    assert result.stderr == ""


@pytest.mark.parametrize("disabled", [False, True])
def test_blob_download_real_library_wiring(disabled: bool) -> None:
    """Exercise the real streaming operation behind the command without network access."""
    data = b"abcdef"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Length"] = "6"
    response.raw = io.BytesIO(data)
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
    ):
        result = CliRunner().invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["digest"] == digest
    assert result.stderr == "" and http.call_count == 1
    assert ("Downloaded" in terminal.getvalue()) is not disabled

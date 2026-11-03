"""Single-row transfer rendering, isolated from library events and structured output."""

import io
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import IO, Literal, final

import click

from regshape.libs.decorators import get_telemetry_config
from regshape.libs.progress import TransferDirection, TransferPhase, TransferProgress

logger = logging.getLogger(__name__)
_TERMINAL = {
    TransferPhase.COMPLETED, TransferPhase.SKIPPED,
    TransferPhase.FAILED, TransferPhase.CANCELLED,
}


def _bytes(value: float) -> str:
    """Format payload counts/rates using explicit binary units."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _safe_label(value: str) -> str:
    """Keep terminal labels printable and single-column ASCII, never control sequences."""
    return value.encode("unicode_escape").decode("ascii")


def _failure_phase(error: BaseException) -> TransferPhase:
    """Distinguish cancellation from ordinary errors without exposing exception contents."""
    return (
        TransferPhase.CANCELLED
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit))
        else TransferPhase.FAILED
    )


class _UnflushedStream(io.TextIOBase, IO[str]):
    """Borrow a write-only Click destination while deferring its automatic flush."""

    def __init__(self, stream: IO[str]) -> None:
        """Retain the destination; closing this adapter never closes the borrowed stream."""
        super().__init__()
        self.stream = stream

    def write(self, text: str) -> int:
        """Forward a write so callers can record cursor movement before flushing."""
        return self.stream.write(text)

    def writable(self) -> bool:
        """Expose the adapter's write-only capability."""
        return True

    def flush(self) -> None:
        """Leave flushing to the renderer after it records the completed control write."""

    def isatty(self) -> bool:
        """Preserve Click's terminal detection and platform ANSI handling."""
        return self.stream.isatty()

    def fileno(self) -> int:
        """Expose the borrowed descriptor for Click's native terminal handling."""
        return self.stream.fileno()


class _TelemetryStream(io.TextIOBase):
    """Route complete/partial telemetry writes around an active progress row."""

    def __init__(self, display: "ProgressDisplay") -> None:
        """Borrow the display stream without taking ownership of it."""
        super().__init__()
        self.display = display
        self._diagnostic_failed = False
        self._diagnostic_error_type: str | None = None

    def writable(self) -> bool:
        """Support print-based telemetry emission."""
        return True

    def write(self, text: str) -> int:
        """Coordinate diagnostics; after a callback failure preserve that primary exception."""
        if self.display._render_failed and self._diagnostic_failed:
            return len(text)
        try:
            self.display.write_diagnostic(text)
        except BaseException as exc:
            if not self.display._render_failed:
                raise
            self._defer_failure(exc)
        return len(text)

    def flush(self) -> None:
        """Flush the borrowed stream without closing it."""
        if self.display.stream.closed or (self.display._render_failed and self._diagnostic_failed):
            return
        try:
            self.display.stream.flush()
        except BaseException as exc:
            if not self.display._render_failed:
                raise
            self._defer_failure(exc)

    def _defer_failure(self, error: BaseException) -> None:
        """Retain a safe error type without moving the cursor before progress cleanup."""
        self._diagnostic_failed = True
        self._diagnostic_error_type = type(error).__name__

    def report_failure(self) -> None:
        """Report a suppressed diagnostic only after progress cursor cleanup is finished."""
        if self._diagnostic_error_type is None:
            return
        logger.error(
            "Telemetry output failed (%s); preserving original progress error",
            self._diagnostic_error_type,
        )
        self._diagnostic_error_type = None


@final
class ProgressDisplay:
    """Render one active row and retain one final row per unique blob.

    Rendering is automatic only on an interactive stderr stream, disabled for
    explicit structured output, structured telemetry, or ``disabled=True``.
    Observations still update invocation-local counts when rendering is off.
    ``clock`` and ``columns`` are deterministic test seams, not CLI options.
    """

    def __init__(
        self, *, disabled: bool = False, structured: bool = False,
        stream: IO[str] | None = None, total_blobs: int | None = 1,
        clock: Callable[[], float] = time.monotonic,
        columns: Callable[[], int] | None = None,
    ) -> None:
        """Borrow output and initialize per-invocation row/attempt state."""
        self.stream = sys.stderr if stream is None else stream
        self.config = get_telemetry_config()
        self.enabled = not disabled and not structured and self.config.output_format != "json" and self.stream.isatty()
        self.total_blobs = total_blobs
        self.clock = clock
        self.columns = columns or self._terminal_columns
        self._event: TransferProgress | None = None
        self._indices: dict[str, int] = {}
        self._finished: dict[str, TransferPhase] = {}
        self._started: float | None = None
        self._last_draw = float("-inf")
        self._visible = False
        self._row_advanced = False
        self._render_failed = False
        self._rendered_length = 0
        self._rendered_columns = 0
        self._partial_diagnostic = False
        self._preparation: str | None = None
        self._old_output = self.config.output
        self._proxy = _TelemetryStream(self)
        self._control_output = _UnflushedStream(self.stream)

    def __enter__(self) -> "ProgressDisplay":
        """Temporarily coordinate telemetry only when it targets this terminal."""
        if self.enabled and self.config.output is self.stream:
            self.config.output = self._proxy
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> Literal[False]:
        """Restore output and preserve a primary error if rendering cleanup also fails."""
        try:
            self._render_failed = False
            self._finish_diagnostic()
            if self._preparation is not None:
                self.finish_preparation(
                    success=exc is None,
                    cancelled=isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)),
                )
            if self._event is not None and exc is not None:
                self._finished.pop(self._event.digest, None)
                self.observe(replace(self._event, phase=_failure_phase(exc)))
            elif self._event is not None and self._event.phase in _TERMINAL:
                self._draw(force=True)
                self._finish_transfer_row()
            elif self._event is not None:
                self._draw(force=True)
                self._end_row()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            logger.error("Progress cleanup failed (%s); preserving original error", type(cleanup_error).__name__)
        finally:
            if self.config.output is self._proxy:
                self.config.output = self._old_output
            self._proxy.report_failure()
        return False

    @property
    def completed(self) -> int:
        """Count unique successfully transferred blobs, not skipped occurrences."""
        return sum(phase == TransferPhase.COMPLETED for phase in self._finished.values())

    @property
    def skipped(self) -> int:
        """Count unique already-present blobs without claiming transferred bytes."""
        return sum(phase == TransferPhase.SKIPPED for phase in self._finished.values())

    def prepare(self, label: str) -> None:
        """Show an indeterminate preparation row before any blob becomes active."""
        self._preparation = label
        if self.enabled:
            self._write_row(self._preparation + " ...")

    def finish_preparation(self, *, success: bool = True, cancelled: bool = False) -> None:
        """Overwrite preparation once with its final state before allocating blob rows."""
        if self._preparation is not None and self.enabled:
            state = "cancelled" if cancelled else ("done" if success else "failed")
            self._write_row(self._preparation + " ... " + state)
            self._end_row()
        self._preparation = None

    def observe(self, event: TransferProgress) -> None:
        """Replace one blob row; retries reset its rate clock and terminal rows are unique.

        Events must describe sequential transfers. A new digest before the
        preceding terminal event raises ``RuntimeError``; rendering errors
        propagate to the library callback boundary.
        """
        if event.digest in self._finished:
            return
        if self._preparation is not None:
            self.finish_preparation()
        previous = self._event
        if previous is not None and previous.digest != event.digest:
            if previous.phase not in _TERMINAL:
                raise RuntimeError("Concurrent blob progress is not supported")
            self._finish_diagnostic()
            self._draw(force=True)
            self._finish_transfer_row()
            previous = None
        if event.digest not in self._indices:
            self._indices[event.digest] = len(self._indices) + 1
            self._started = None
        if event.phase == TransferPhase.TRANSFERRING and self._started is None:
            self._started = self.clock()
        if previous is not None and previous.attempt != event.attempt:
            self._started = self.clock()
        force = previous is None or previous.phase != event.phase or previous.attempt != event.attempt
        self._event = event
        try:
            self._draw(force=force or event.phase in _TERMINAL)
            if event.phase in _TERMINAL:
                self._finished[event.digest] = event.phase
                if not self._partial_diagnostic:
                    self._finish_transfer_row()
        except BaseException as exc:
            # Nested scenario telemetry runs before the display context exits.
            self._event = replace(event, phase=_failure_phase(exc))
            self._finished.pop(event.digest, None)
            self._render_failed = True
            raise

    def _text(self, event: TransferProgress) -> str:
        """Describe logical bytes and state without treating a full byte count as success."""
        index = self._indices[event.digest]
        prefix = f"Blob {index}" + (f"/{self.total_blobs}" if self.total_blobs is not None else "")
        labels = {
            TransferPhase.PREPARING: "Preparing",
            TransferPhase.TRANSFERRING: "Downloading" if event.direction == TransferDirection.DOWNLOAD else "Uploading",
            TransferPhase.RETRYING: f"Retrying attempt {event.attempt}",
            TransferPhase.VERIFYING: "Verifying",
            TransferPhase.COMMITTING: "Committing",
            TransferPhase.COMPLETED: "Downloaded" if event.direction == TransferDirection.DOWNLOAD else "Uploaded",
            TransferPhase.SKIPPED: "Already present; skipped",
            TransferPhase.FAILED: "Failed",
            TransferPhase.CANCELLED: "Cancelled",
        }
        state = labels[event.phase]
        width = max(0, self.columns() - 1)
        count = "" if event.phase == TransferPhase.SKIPPED else _bytes(event.bytes_completed)
        if count and event.total_bytes is not None:
            count += "/" + _bytes(event.total_bytes)
        if len(f"{prefix} {state} {count}".rstrip()) > width:
            prefix = f"Blob {index}"
        if len(f"{prefix} {state} {count}".rstrip()) > width and count:
            count = _bytes(event.bytes_completed)
        if len(f"{prefix} {state} {count}".rstrip()) > width:
            prefix = f"#{index}"
            if event.phase == TransferPhase.SKIPPED:
                state = "Skipped"
        text = f"{prefix} {state} {count}".rstrip()
        extras: list[str] = []
        if event.phase == TransferPhase.TRANSFERRING and event.total_bytes:
            ratio = event.bytes_completed / event.total_bytes
            filled = int(ratio * 12)
            bar = f" [{'#' * filled}{'-' * (12 - filled)}] {ratio:.0%}"
            extras.append(bar if len(text + bar) <= width else f" {ratio:.0%}")
        if event.total_bytes is None and event.phase not in _TERMINAL:
            extras.append(" (total unknown)")
        for extra in extras:
            if len(text + extra) <= width:
                text += extra
        algorithm, digest = event.digest.split(":", 1)
        short_digest = f" {algorithm}:{digest[:4]}..."
        if len(text + short_digest) <= width:
            text = prefix + short_digest + text[len(prefix):]
        if event.phase == TransferPhase.TRANSFERRING:
            elapsed = self.clock() - self._started if self._started is not None else 0
            if elapsed > 0 and event.bytes_completed:
                rate = event.bytes_completed / elapsed
                rate_text = f" {_bytes(rate)}/s"
                if len(text + rate_text) <= width:
                    text += rate_text
                    if event.total_bytes and event.bytes_completed < event.total_bytes:
                        eta = f" ~{(event.total_bytes - event.bytes_completed) / rate:.0f}s remaining"
                        if len(text + eta) <= width:
                            text += eta
        return text

    def _draw(self, *, force: bool) -> None:
        """Coalesce routine updates to ten per second while flushing state transitions."""
        if not self.enabled or self._partial_diagnostic or self._render_failed:
            return
        if self._event is None and self._preparation is None:
            return
        now = self.clock()
        if not force and self.columns() == self._rendered_columns and now - self._last_draw < 0.1:
            return
        text = self._text(self._event) if self._event is not None else f"{self._preparation} ..."
        self._write_row(text)
        self._last_draw = now

    def _write_row(self, text: str) -> None:
        """Erase trailing text and avoid autowrap at the current terminal width."""
        columns = max(1, self.columns())
        text = _safe_label(text)[:columns - 1]
        self._clear_row(columns)
        self._visible = True
        self._rendered_length = len(text)
        self._rendered_columns = columns
        click.echo(text, file=self.stream, nl=False, color=True)

    def _terminal_columns(self) -> int:
        """Measure stderr, not possibly redirected stdout; use Click-compatible fallback width."""
        try:
            return os.get_terminal_size(self.stream.fileno()).columns
        except OSError:
            return shutil.get_terminal_size((80, 24)).columns

    def _clear_row(self, columns: int) -> None:
        """Erase the active row, including its reflowed footprint after a terminal shrink."""
        wrapped = max(0, (self._rendered_length - 1) // columns) if self._visible else 0
        controls = ("\x1b[1A" if self._row_advanced else "") + "\r\x1b[2K"
        controls += "\x1b[1A\r\x1b[2K" * wrapped
        click.echo(controls, file=self._control_output, nl=False, color=True)
        self._visible = False
        self._row_advanced = False
        self.stream.flush()

    def _finish_transfer_row(self) -> None:
        """Retain a terminal transfer row and clear only its active state."""
        self._end_row()
        self._event = None
        self._started = None

    def _end_row(self) -> None:
        """Track a written newline before flushing so failed finalization can repaint it."""
        if self.enabled and self._visible:
            self.stream.write("\n")
            self._row_advanced = True
            self.stream.flush()
            self._visible = False
            self._row_advanced = False

    def write_diagnostic(self, text: str) -> None:
        """Write telemetry without leaving an old progress row behind."""
        if self.enabled and self._visible:
            self._clear_row(max(1, self.columns()))
        self.stream.write(text)
        self._partial_diagnostic = not text.endswith("\n")
        if not self._partial_diagnostic:
            self._draw(force=True)
            if self._event is not None and self._event.phase in _TERMINAL and not self._render_failed:
                self._finished[self._event.digest] = self._event.phase
                self._finish_transfer_row()

    def _finish_diagnostic(self) -> None:
        """Terminate an interrupted diagnostic before rendering a final row or another blob."""
        if self._partial_diagnostic:
            self.stream.write("\n")
            self._partial_diagnostic = False

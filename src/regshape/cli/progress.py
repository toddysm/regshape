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


class _TelemetryStream(io.TextIOBase):
    """Route complete/partial telemetry writes around an active progress row."""

    def __init__(self, display: "ProgressDisplay") -> None:
        """Borrow the display stream without taking ownership of it."""
        super().__init__()
        self.display = display

    def writable(self) -> bool:
        """Support print-based telemetry emission."""
        return True

    def write(self, text: str) -> int:
        """Preserve text bytes while suspending/redrawing the active row at newlines."""
        self.display.write_diagnostic(text)
        return len(text)

    def flush(self) -> None:
        """Flush the borrowed stream without closing it."""
        if not self.display.stream.closed:
            self.display.stream.flush()


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
        self._rendered_length = 0
        self._rendered_columns = 0
        self._partial_diagnostic = False
        self._preparation: str | None = None
        self._old_output = self.config.output
        self._proxy = _TelemetryStream(self)

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
            self._finish_diagnostic()
            if self._preparation is not None:
                self.finish_preparation(
                    success=exc is None,
                    cancelled=isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)),
                )
            if self._event is not None and self._event.phase in _TERMINAL:
                self._draw(force=True)
                self._finish_transfer_row()
            elif self._event is not None and exc is not None:
                phase = (
                    TransferPhase.CANCELLED
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit))
                    else TransferPhase.FAILED
                )
                self.observe(replace(self._event, phase=phase))
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
        self._draw(force=force or event.phase in _TERMINAL)
        if event.phase in _TERMINAL:
            self._finished[event.digest] = event.phase
            if not self._partial_diagnostic:
                self._finish_transfer_row()

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
        if self.columns() < 40:
            return f"{prefix} {state}"
        text = f"{prefix} {event.digest[:19]}... {state}"
        if event.phase == TransferPhase.SKIPPED:
            return text
        text += " " + _bytes(event.bytes_completed)
        if event.total_bytes is not None:
            text += "/" + _bytes(event.total_bytes)
        elif event.phase not in _TERMINAL:
            text += " (total unknown)"
        if event.phase == TransferPhase.TRANSFERRING:
            if event.total_bytes:
                ratio = event.bytes_completed / event.total_bytes
                filled = int(ratio * 12)
                text += f" [{'#' * filled}{'-' * (12 - filled)}] {ratio:.0%}"
            elapsed = self.clock() - self._started if self._started is not None else 0
            if elapsed > 0 and event.bytes_completed:
                rate = event.bytes_completed / elapsed
                text += f" {_bytes(rate)}/s"
                if event.total_bytes and event.bytes_completed < event.total_bytes:
                    text += f" ~{(event.total_bytes - event.bytes_completed) / rate:.0f}s remaining"
        return text

    def _draw(self, *, force: bool) -> None:
        """Coalesce routine updates to ten per second while flushing state transitions."""
        if not self.enabled or self._partial_diagnostic:
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
        click.echo(text, file=self.stream, nl=False, color=True)
        self.stream.flush()
        self._visible = True
        self._rendered_length = len(text)
        self._rendered_columns = columns

    def _terminal_columns(self) -> int:
        """Measure stderr, not possibly redirected stdout; use Click-compatible fallback width."""
        try:
            return os.get_terminal_size(self.stream.fileno()).columns
        except OSError:
            return shutil.get_terminal_size((80, 24)).columns

    def _clear_row(self, columns: int) -> None:
        """Erase the active row, including its reflowed footprint after a terminal shrink."""
        wrapped = max(0, (self._rendered_length - 1) // columns) if self._visible else 0
        controls = "\r\x1b[2K" + "\x1b[1A\r\x1b[2K" * wrapped
        click.echo(controls, file=self.stream, nl=False, color=True)
        self._visible = False

    def _finish_transfer_row(self) -> None:
        """Retain a terminal transfer row and clear only its active state."""
        self._end_row()
        self._event = None
        self._started = None

    def _end_row(self) -> None:
        """Retain the final rendered row exactly once."""
        if self.enabled and self._visible:
            click.echo(file=self.stream)
            self._visible = False

    def write_diagnostic(self, text: str) -> None:
        """Write telemetry without leaving an old progress row behind."""
        if self.enabled and self._visible:
            self._clear_row(max(1, self.columns()))
        self.stream.write(text)
        self._partial_diagnostic = not text.endswith("\n")
        if not self._partial_diagnostic:
            self._draw(force=True)
            if self._event is not None and self._event.phase in _TERMINAL:
                self._finish_transfer_row()

    def _finish_diagnostic(self) -> None:
        """Terminate an interrupted diagnostic before rendering a final row or another blob."""
        if self._partial_diagnostic:
            self.stream.write("\n")
            self._partial_diagnostic = False

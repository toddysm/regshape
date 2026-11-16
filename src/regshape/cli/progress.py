"""Shared blob/manifest/artifact rows, diagnostics and payload-safe terminal handoff."""

import io
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from types import TracebackType
from typing import IO, Literal, final

import click

from regshape.libs.artifacts.models import ArtifactPhase, ArtifactProgress
from regshape.libs.decorators import get_telemetry_config
from regshape.libs.progress import (
    ManifestKind,
    ManifestPhase,
    ManifestProgress,
    TransferDirection,
    TransferPhase,
    TransferProgress,
)

logger = logging.getLogger(__name__)


class _ManifestStage(Enum):
    """CLI-only stages after receipt; library receipt alone never means command success."""

    RECEIVED = "received"
    PROCESSING = "processing"
    WRITING = "writing"
    COMPLETED = "completed"


class _ArtifactStage(Enum):
    """Per-object verified storage, not completion of the whole artifact publication."""

    STORED = "stored"


_RowKey = tuple[Literal["blob", "manifest", "artifact"], str]
_RowPhase = TransferPhase | _ManifestStage | _ArtifactStage

_TERMINAL: set[_RowPhase] = {
    TransferPhase.COMPLETED, TransferPhase.SKIPPED,
    TransferPhase.FAILED, TransferPhase.CANCELLED,
    _ManifestStage.COMPLETED,
    _ArtifactStage.STORED,
}


@dataclass(frozen=True)
class _Row:
    """Private rendering state; namespaced keys separate blob and opaque invocation identities.

    Artifact ``kind`` selects numbering without changing identity. ``role`` is
    descriptive only; ``source`` distinguishes stored local bytes from traffic.
    """

    key: _RowKey
    label: str
    phase: _RowPhase
    bytes_completed: int
    total_bytes: int | None
    attempt: int = 1
    direction: TransferDirection = TransferDirection.DOWNLOAD
    digest: str | None = None
    kind: str | None = None
    role: str | None = None
    source: str = "network"

    @property
    def is_blob(self) -> bool:
        """Distinguish verified blob rows from command-owned manifest completion."""
        return self.key[0] == "blob"


def _row(event: TransferProgress | ManifestProgress | ArtifactProgress) -> _Row:
    """Normalize observations without fabricating a digest or weakening public event types."""
    if isinstance(event, ArtifactProgress):
        artifact_phases: dict[str, _RowPhase] = {
            "preparing": TransferPhase.PREPARING,
            "transferring": TransferPhase.TRANSFERRING,
            "received": _ManifestStage.RECEIVED,
            "verifying": TransferPhase.VERIFYING,
            "stored": _ArtifactStage.STORED,
            "skipped": TransferPhase.SKIPPED,
            "failed": TransferPhase.FAILED,
            "cancelled": TransferPhase.CANCELLED,
        }
        return _Row(
            ("artifact", event.transfer_id),
            "Manifest" if event.kind == "unknown" else event.kind.title(),
            artifact_phases[event.phase], event.bytes_completed, event.total_bytes,
            attempt=event.attempt, digest=event.digest, kind=event.kind,
            role=event.role, source=event.source,
        )
    if isinstance(event, TransferProgress):
        return _Row(
            ("blob", event.digest), "Blob", event.phase,
            event.bytes_completed, event.total_bytes,
            event.attempt, event.direction, event.digest,
        )
    if not isinstance(event, ManifestProgress):
        raise TypeError("Progress display requires a blob, manifest or artifact observation")
    phases: dict[ManifestPhase, _RowPhase] = {
        ManifestPhase.PREPARING: TransferPhase.PREPARING,
        ManifestPhase.TRANSFERRING: TransferPhase.TRANSFERRING,
        ManifestPhase.RECEIVED: _ManifestStage.RECEIVED,
        ManifestPhase.FAILED: TransferPhase.FAILED,
        ManifestPhase.CANCELLED: TransferPhase.CANCELLED,
    }
    return _Row(
        ("manifest", event.transfer_id),
        "Index" if event.kind == ManifestKind.INDEX else "Manifest",
        phases[event.phase], event.bytes_completed, event.total_bytes,
    )


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


class _TelemetryStream(io.TextIOBase, IO[str]):
    """Route complete/partial telemetry writes around an active progress row."""

    def __init__(self, display: "ProgressDisplay") -> None:
        """Borrow the display stream without taking ownership of it."""
        super().__init__()
        self.display = display
        self._diagnostic_failed = False
        self._diagnostic_error: BaseException | None = None

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
        """Retain a deferred diagnostic without moving the cursor; report only its type later."""
        self._diagnostic_failed = True
        self._diagnostic_error = error

    def report_failure(self, primary: BaseException | None) -> None:
        """Report a deferred diagnostic after cleanup without masking the original failure."""
        if self._diagnostic_error is None:
            return
        error = self._diagnostic_error
        self._diagnostic_error = None
        try:
            logger.error(
                "Telemetry output failed (%s); preserving original progress error",
                type(error).__name__,
            )
        except BaseException:
            if primary is None:
                raise
            raise primary


class _ManifestOutput:
    """Suspend a manifest row while newline-terminated result helpers write stdout.

    A class-based context preserves immutable caller exceptions. On partial
    failure the display, not stdout, supplies a safe new line before repainting.
    """

    def __init__(self, display: "ProgressDisplay") -> None:
        """Borrow the display; never replace or close the caller's stdout."""
        self.display = display

    def __enter__(self) -> None:
        """Clear the row through stderr before any payload characters are written."""
        self.display._begin_output()

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> Literal[False]:
        """Rebase on success or retain a safe-boundary requirement on partial failure."""
        self.display._output_suspended = False
        if exc is not None:
            self.display._record_failure(exc)
        else:
            self.display._output_needs_linebreak = False
            try:
                self.display._draw(force=True)
            except BaseException as error:
                self.display._record_failure(error)
                raise
        return False


@final
class ProgressDisplay:
    """Render sequential transfers with one row per blob digest or stable document/object identity.

    Rendering is automatic only on an interactive stderr stream, disabled for
    explicit structured output, structured telemetry, or ``disabled=True``.
    Observations still update invocation-local counts when rendering is off.
    ``clock`` and ``columns`` are deterministic test seams, not CLI options.
    The synchronous context temporarily coordinates existing logging handlers
    bound to this terminal; other output destinations remain untouched.
    ``show_counts`` adds unique successful-transfer (``done``) and already-present
    counts when space permits; skipped blobs never contribute transferred bytes.
    """

    def __init__(
        self, *, disabled: bool = False, structured: bool = False,
        stream: IO[str] | None = None, total_blobs: int | None = 1,
        show_counts: bool = False,
        clock: Callable[[], float] = time.monotonic,
        columns: Callable[[], int] | None = None,
    ) -> None:
        """Borrow output and initialize per-invocation row/attempt state."""
        self.stream = sys.stderr if stream is None else stream
        self.config = get_telemetry_config()
        self.enabled = not disabled and not structured and self.config.output_format != "json" and self.stream.isatty()
        self.total_blobs = total_blobs
        self.show_counts = show_counts
        self.clock = clock
        self.columns = columns or self._terminal_columns
        self._event: _Row | None = None
        self._indices: dict[_RowKey, int] = {}
        self._finished: dict[_RowKey, _RowPhase] = {}
        self._row_counts: dict[str, int] = {}
        self._started: float | None = None
        self._last_draw = float("-inf")
        self._visible = False
        self._row_advanced = False
        self._render_failed = False
        self._rendered_length = 0
        self._rendered_columns = 0
        self._partial_diagnostic = False
        self._output_suspended = False
        self._output_needs_linebreak = False
        self._preparation: str | None = None
        self._old_output = self.config.output
        self._proxy = _TelemetryStream(self)
        self._control_output = _UnflushedStream(self.stream)
        self._log_streams: list[tuple[logging.StreamHandler, IO[str]]] = []
        self._old_last_resort: logging.Handler | None = None
        self._last_resort_proxy: logging.StreamHandler | None = None

    def __enter__(self) -> "ProgressDisplay":
        """Coordinate telemetry and existing stderr logging without replacing global stderr."""
        if self.enabled:
            try:
                if self.config.output is self.stream:
                    self.config.output = self._proxy
                self._redirect_logging()
            except BaseException:
                self._restore_outputs()
                raise
        return self

    def _redirect_logging(self) -> None:
        """Route handlers bound to this exact terminal through the shared diagnostic writer."""
        loggers = [logging.getLogger()] + [
            item for item in list(logging.Logger.manager.loggerDict.values())
            if isinstance(item, logging.Logger)
        ]
        for item in loggers:
            for handler in list(item.handlers):
                if isinstance(handler, logging.StreamHandler) and handler.stream is self.stream:
                    handler.acquire()
                    try:
                        self._log_streams.append((handler, handler.stream))
                        handler.stream = self._proxy
                    finally:
                        handler.release()
        fallback = logging.lastResort
        if isinstance(fallback, logging.StreamHandler) and fallback.stream is self.stream:
            self._old_last_resort = fallback
            replacement = logging.StreamHandler(self._proxy)
            replacement.setLevel(fallback.level)
            replacement.setFormatter(fallback.formatter)
            replacement.terminator = fallback.terminator
            for log_filter in fallback.filters:
                replacement.addFilter(log_filter)
            self._last_resort_proxy = replacement
            logging.lastResort = replacement

    def _restore_outputs(self) -> None:
        """Restore only destinations still owned by this display, without fallible I/O."""
        if self.config.output is self._proxy:
            self.config.output = self._old_output
        for handler, stream in reversed(self._log_streams):
            handler.acquire()
            try:
                if handler.stream is self._proxy:
                    handler.stream = stream
            finally:
                handler.release()
        self._log_streams.clear()
        if self._last_resort_proxy is not None:
            if logging.lastResort is self._last_resort_proxy:
                logging.lastResort = self._old_last_resort
            self._last_resort_proxy.close()
            self._last_resort_proxy = None

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> Literal[False]:
        """Restore output and preserve a primary error if rendering cleanup also fails."""
        cleanup_failure: BaseException | None = None
        try:
            self._render_failed = False
            self._finish_output_boundary()
            self._finish_diagnostic()
            if self._preparation is not None:
                self.finish_preparation(
                    success=exc is None,
                    cancelled=isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)),
                )
            if self._event is not None and exc is not None:
                self._finished.pop(self._event.key, None)
                self._observe_row(replace(self._event, phase=_failure_phase(exc)))
            elif self._event is not None and self._event.phase in _TERMINAL:
                self._draw(force=True)
                self._finish_transfer_row()
            elif self._event is not None:
                self._draw(force=True)
                self._end_row()
        except BaseException as cleanup_error:
            cleanup_failure = cleanup_error
            if exc is None:
                raise
        finally:
            self._restore_outputs()
            if cleanup_failure is not None and exc is not None:
                try:
                    logger.error(
                        "Progress cleanup failed (%s); preserving original error",
                        type(cleanup_failure).__name__,
                    )
                except BaseException:  # noqa: BLE001 - preserve the original cleanup owner
                    raise exc
            self._proxy.report_failure(exc if exc is not None else cleanup_failure)
        return False

    @property
    def completed(self) -> int:
        """Count unique successfully transferred blobs, not skipped occurrences."""
        return sum(key[0] == "blob" and phase == TransferPhase.COMPLETED for key, phase in self._finished.items())

    @property
    def skipped(self) -> int:
        """Count unique already-present blobs without claiming transferred bytes."""
        return sum(key[0] == "blob" and phase == TransferPhase.SKIPPED for key, phase in self._finished.items())

    def prepare(self, label: str) -> None:
        """Show an indeterminate preparation row before any transfer becomes active."""
        self._preparation = label
        if self.enabled:
            try:
                self._write_row(self._preparation + " ...")
            except BaseException:
                self._render_failed = True
                raise

    def finish_preparation(self, *, success: bool = True, cancelled: bool = False) -> None:
        """Overwrite preparation once with its final state before allocating transfer rows."""
        if self._preparation is not None and self.enabled:
            state = "cancelled" if cancelled else ("done" if success else "failed")
            try:
                self._write_row(self._preparation + " ... " + state)
                self._end_row()
            except BaseException:
                self._render_failed = True
                raise
        self._preparation = None

    def observe(self, event: TransferProgress | ManifestProgress | ArtifactProgress) -> None:
        """Adapt all transfer kinds into the same renderer and diagnostic lifecycle.

        Blob deduplication remains digest-based. Manifest identity is opaque and
        independent of advertised kind; callers explicitly process/write/finish
        the active standalone manifest. Artifact identities survive kind/digest
        discovery and finalize only on verified storage, including tiny configs.
        Embedded/local observations never acquire network rate or percentage.
        Concurrent transfers raise RuntimeError.
        """
        self._observe_row(_row(event))

    def artifact_phase(self, phase: ArtifactPhase) -> None:
        """Announce local packaging/publication separately after all object rows settle.

        A new phase completes the previous local phase; the caller completes the
        final one only after publication and required diagnostics return.
        """
        if self._event is not None:
            self._finish_diagnostic()
            if self._event.phase not in _TERMINAL:
                raise RuntimeError("Artifact phase requires all object rows to be stored")
            self._draw(force=True)
            self._finish_transfer_row()
        self.finish_preparation()
        self.prepare({
            ArtifactPhase.PACKAGING: "Packaging OCI archive",
            ArtifactPhase.PUBLISHING: "Publishing artifact",
        }[phase])

    def _observe_row(self, event: _Row) -> None:
        """Reuse shared redraw/finalization, assigning artifact kind numbers on discovery.

        Unknown-root numbering never consumes a real manifest/index ordinal.
        The existing row key and cursor survive that reclassification.
        """
        if event.key in self._finished:
            return
        if self._preparation is not None:
            self.finish_preparation()
        previous = self._event
        if previous is not None and previous.key != event.key:
            if previous.phase not in _TERMINAL:
                raise RuntimeError("Concurrent transfer progress is not supported")
            self._finish_output_boundary()
            self._finish_diagnostic()
            self._draw(force=True)
            self._finish_transfer_row()
            previous = None
        if event.key not in self._indices or (
            previous is not None and event.key[0] == "artifact" and previous.kind != event.kind
        ):
            group = f"artifact:{event.kind}" if event.key[0] == "artifact" else event.key[0]
            self._row_counts[group] = self._row_counts.get(group, 0) + 1
            self._indices[event.key] = self._row_counts[group]
            self._started = None
        if event.phase == TransferPhase.TRANSFERRING and self._started is None:
            self._started = self.clock()
        if previous is not None and previous.attempt != event.attempt:
            self._started = self.clock()
        force = (
            previous is None or previous.phase != event.phase
            or previous.attempt != event.attempt or previous.label != event.label
            or previous.total_bytes != event.total_bytes or previous.digest != event.digest
        )
        self._event = event
        try:
            self._draw(force=force or event.phase in _TERMINAL)
            if event.phase in _TERMINAL:
                self._finished[event.key] = event.phase
                if not self._partial_diagnostic:
                    self._finish_transfer_row()
        except BaseException as exc:
            # Nested scenario telemetry runs before the display context exits.
            self._record_failure(exc)
            raise

    def _record_failure(self, error: BaseException) -> None:
        """Keep failed state pending until outer cleanup, including nested telemetry failures."""
        if self._event is not None:
            self._finished.pop(self._event.key, None)
            self._event = replace(self._event, phase=_failure_phase(error))
        self._render_failed = True

    def _manifest_row(self, *allowed: _ManifestStage) -> _Row:
        """Require the active manifest stage rather than silently accepting missing observations."""
        event = self._event
        if event is None or event.is_blob or event.phase not in allowed:
            raise RuntimeError("Manifest progress is not at the required stage")
        if self._output_suspended:
            raise RuntimeError("Manifest output is already in progress")
        return event

    def processing_manifest(self) -> None:
        """Begin optional model parsing/extraction only after library receipt."""
        event = self._manifest_row(_ManifestStage.RECEIVED)
        self._observe_row(replace(event, phase=_ManifestStage.PROCESSING))

    def writing_manifest(self) -> None:
        """Begin output after receipt or processing without claiming successful completion."""
        event = self._manifest_row(_ManifestStage.RECEIVED, _ManifestStage.PROCESSING)
        self._observe_row(replace(event, phase=_ManifestStage.WRITING))

    def finish_manifest(self) -> None:
        """Finalize after output and scenario work return, flushing diagnostics before success."""
        event = self._manifest_row(_ManifestStage.WRITING)
        self._finish_output_boundary()
        self._finish_diagnostic()
        self._observe_row(replace(event, phase=_ManifestStage.COMPLETED))

    def manifest_output(self) -> _ManifestOutput:
        """Borrow a suspension scope for the existing newline-terminated stdout helpers."""
        return _ManifestOutput(self)

    def _begin_output(self) -> None:
        """Erase only the active row, then forget its footprint before stdout can move the cursor."""
        self._manifest_row(_ManifestStage.WRITING)
        try:
            self._finish_diagnostic()
            if self.enabled and self._visible:
                self._clear_row(max(1, self.columns()))
        except BaseException as error:
            self._record_failure(error)
            raise
        self._visible = self._row_advanced = False
        self._rendered_length = self._rendered_columns = 0
        self._output_suspended = True
        self._output_needs_linebreak = True

    def _finish_output_boundary(self) -> None:
        """Protect partial stdout from cursor-up/erase by starting a fresh stderr line."""
        if self._output_needs_linebreak and not self._output_suspended:
            if self.enabled:
                self.stream.write("\n")
                self._output_needs_linebreak = False
                self.stream.flush()
            else:
                self._output_needs_linebreak = False

    def _text(self, event: _Row) -> str:
        """Describe logical bytes and state without treating a full byte count as success."""
        index = self._indices[event.key]
        prefix = f"{event.label} {index}"
        if event.role is not None:
            prefix += f" ({event.role})"
        if event.is_blob and self.total_blobs is not None:
            prefix += f"/{self.total_blobs}"
        if self.show_counts and event.is_blob:
            pending = event.key not in self._finished
            completed = self.completed + (pending and event.phase == TransferPhase.COMPLETED)
            skipped = self.skipped + (pending and event.phase == TransferPhase.SKIPPED)
            prefix += f" [{completed} done/{skipped} skipped]"
        labels: dict[_RowPhase, str] = {
            TransferPhase.PREPARING: "Preparing",
            TransferPhase.TRANSFERRING: (
                f"Storing ({event.source})" if event.source != "network"
                else ("Downloading" if event.direction == TransferDirection.DOWNLOAD else "Uploading")
            ),
            TransferPhase.RETRYING: f"Retrying attempt {event.attempt}",
            TransferPhase.VERIFYING: "Verifying",
            TransferPhase.COMMITTING: "Committing",
            TransferPhase.COMPLETED: "Downloaded" if event.direction == TransferDirection.DOWNLOAD else "Uploaded",
            TransferPhase.SKIPPED: "Already present; skipped",
            TransferPhase.FAILED: "Failed",
            TransferPhase.CANCELLED: "Cancelled",
            _ManifestStage.RECEIVED: "Received",
            _ManifestStage.PROCESSING: "Processing",
            _ManifestStage.WRITING: "Writing",
            _ManifestStage.COMPLETED: "Downloaded",
            _ArtifactStage.STORED: (
                "Downloaded" if event.source == "network" else f"Stored ({event.source})"
            ),
        }
        state = labels[event.phase]
        width = max(0, self.columns() - 1)
        count = "" if event.phase == TransferPhase.SKIPPED else _bytes(event.bytes_completed)
        if count and event.total_bytes is not None:
            count += "/" + _bytes(event.total_bytes)
        if len(f"{prefix} {state} {count}".rstrip()) > width:
            prefix = f"{event.label} {index}"
        if len(f"{prefix} {state} {count}".rstrip()) > width and count:
            count = _bytes(event.bytes_completed)
        if len(f"{prefix} {state} {count}".rstrip()) > width:
            prefix = f"#{index}"
            if event.phase == TransferPhase.SKIPPED:
                state = "Skipped"
        if event.key[0] == "artifact" and len(f"{prefix} {state} {count}".rstrip()) > width:
            state = {
                "Downloading": "Pulling", "Downloaded": "Stored",
                "Verifying": "Verify", "Preparing": "Prepare",
            }.get(state, state)
        text = f"{prefix} {state} {count}".rstrip()
        extras: list[str] = []
        if event.phase == TransferPhase.TRANSFERRING and event.total_bytes and event.source == "network":
            ratio = event.bytes_completed / event.total_bytes
            filled = int(ratio * 12)
            bar = f" [{'#' * filled}{'-' * (12 - filled)}] {ratio:.0%}"
            extras.append(bar if len(text + bar) <= width else f" {ratio:.0%}")
        if event.total_bytes is None and event.phase not in _TERMINAL:
            extras.append(" (total unknown)")
        for extra in extras:
            if len(text + extra) <= width:
                text += extra
        if event.digest is not None:
            algorithm, digest = event.digest.split(":", 1)
            short_digest = f" {algorithm}:{digest[:4]}..."
            if len(text + short_digest) <= width:
                text = prefix + short_digest + text[len(prefix):]
        if event.phase == TransferPhase.TRANSFERRING and event.source == "network":
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
        if not self.enabled or self._partial_diagnostic or self._render_failed or self._output_suspended:
            return
        if self._event is None and self._preparation is None:
            return
        self._finish_output_boundary()
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
            columns = os.get_terminal_size(self.stream.fileno()).columns
        except OSError:
            return shutil.get_terminal_size((80, 24)).columns
        return columns if columns > 0 else shutil.get_terminal_size((80, 24)).columns

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
        self._finish_output_boundary()
        if self.enabled and self._visible:
            self._clear_row(max(1, self.columns()))
        self.stream.write(text)
        self._partial_diagnostic = not text.endswith("\n")
        if not self._partial_diagnostic:
            self._draw(force=True)
            if self._event is not None and self._event.phase in _TERMINAL and not self._render_failed:
                self._finished[self._event.key] = self._event.phase
                self._finish_transfer_row()

    def _finish_diagnostic(self) -> None:
        """Terminate an interrupted diagnostic before rendering a final row or another blob."""
        if self._partial_diagnostic:
            self.stream.write("\n")
            self._partial_diagnostic = False

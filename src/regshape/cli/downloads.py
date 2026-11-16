"""Collect safe download status and emit one warning after command output/cleanup.

The collector never prints from a status callback. Commands place this scope
outside telemetry, progress and result output; it owns neither workers nor sinks.
"""

import io
import logging
import sys
from types import TracebackType
from typing import IO, Literal, final

import click

from regshape.cli.progress import (
    ProgressDisplay,
    _redirect_stderr_handlers,
    _restore_stderr_handlers,
)
from regshape.libs.blobs.download import DownloadStatus
from regshape.libs.decorators import get_telemetry_config


class _DownloadOutput(io.TextIOBase, IO[str]):
    """Track diagnostic failures throughout one command without owning its stderr.

    Existing display cleanup keeps its repaint behavior; the outer warning
    collector uses the persistent flag to avoid any additional diagnostics.
    """

    def __init__(self, stream: IO[str]) -> None:
        """Borrow a text sink before telemetry setup or progress suppression is decided."""
        super().__init__()
        self.stream = stream
        self.failed = False

    def write(self, text: str) -> int:
        """Record a real text-write failure, excluding Click's empty binary probes."""
        if not isinstance(text, str):
            raise TypeError("Download diagnostics require text")
        try:
            return self.stream.write(text)
        except BaseException:
            self.failed = True
            raise

    def flush(self) -> None:
        """Record flush failures without changing their exception identity."""
        try:
            self.stream.flush()
        except BaseException:
            self.failed = True
            raise

    def writable(self) -> bool:
        """Expose the borrowed destination's text-output capability."""
        return True

    def isatty(self) -> bool:
        """Preserve progress suppression and Click's platform terminal handling."""
        return self.stream.isatty()

    def fileno(self) -> int:
        """Measure the borrowed terminal rather than redirected stdout."""
        return self.stream.fileno()

    def close(self) -> None:
        """Never close or flush the borrowed sink, including during adapter destruction."""


@final
class DownloadWarnings:
    """Coalesce changed-ETag observations by content address, independently of redraws.

    Only validated digests are retained, not part events, ETags, URLs or diagnostic
    messages. The set is bounded by the invocation's distinct blob inputs/graph
    budget. Invocation-scoped stderr tracking outlives the borrowed display and
    covers telemetry cleanup/error reporting even when progress is suppressed.
    All borrowed streams and logging handlers are restored without I/O.
    """

    def __init__(self, *, artifact: bool = False) -> None:
        """Select count-based artifact wording or the standalone verified digest."""
        self.artifact = artifact
        self.display: ProgressDisplay | None = None
        self._affected: set[str] = set()
        self._stderr: IO[str] | None = None
        self._output: _DownloadOutput | None = None
        self._log_streams: list[tuple[logging.StreamHandler, IO[str]]] = []

    def __enter__(self) -> "DownloadWarnings":
        """Track stderr before telemetry setup, retaining renderer/logging stream identity."""
        self._stderr = sys.stderr
        self._output = _DownloadOutput(self._stderr)
        sys.stderr = self._output
        try:
            _redirect_stderr_handlers(
                self._stderr, self._output, self._log_streams, include_last_resort=True,
            )
        except BaseException:
            self._restore_output()
            raise
        return self

    def observe(self, event: DownloadStatus) -> None:
        """Remember a fixed reason once per blob, without output or completion claims."""
        if event.reason == "etag_changed" and event.digest is not None:
            self._affected.add(event.digest)

    def result_failed(self) -> None:
        """Do not diagnose through stderr when it aliases a failed final-result destination."""
        if self._output is not None and (sys.stdout is self._stderr or sys.stdout is sys.stderr):
            self._output.failed = True

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Warn after cleanup/result or failure reporting, preserving any primary error.

        Click normally reports cancellation/ClickException outside the callback.
        For an affected invocation, report them here once before the warning and
        retain their exit codes. Unexpected errors propagate unchanged; only their
        type is reported here, never a potentially sensitive exception message.
        Secondary sink errors are chained without logging back to the failed sink.
        """
        try:
            return self._finish(exc)
        finally:
            self._restore_output()

    def _restore_output(self) -> None:
        """Restore only invocation-owned bindings, never flushing a possibly failed sink."""
        if self._output is not None:
            _restore_stderr_handlers(self._output, self._log_streams)
            config = get_telemetry_config()
            if config.output is self._output and self._stderr is not None:
                config.output = self._stderr
            if sys.stderr is self._output and self._stderr is not None:
                sys.stderr = self._stderr

    def _finish(self, exc: BaseException | None) -> Literal[False]:
        """Emit only after successful stderr use throughout the command's complete lifetime."""
        if (
            not self._affected
            or (self._output is not None and self._output.failed)
            or (self.display is not None and self.display.sink_failed)
        ):
            return False
        primary = exc
        if primary is None:
            try:
                sys.stdout.flush()
            except BaseException as flush_error:
                if sys.stdout is sys.stderr or sys.stdout is self._stderr:
                    raise
                primary = flush_error
        reported: click.exceptions.Exit | None = None
        try:
            if isinstance(primary, (KeyboardInterrupt, EOFError, click.Abort)):
                reported = click.exceptions.Exit(1)
                click.echo("\nAborted!", err=True)
            elif isinstance(primary, click.ClickException):
                reported = click.exceptions.Exit(primary.exit_code)
                primary.show()
            elif primary is not None and not isinstance(primary, (SystemExit, click.exceptions.Exit)):
                click.echo(f"Error: Download did not complete ({type(primary).__name__}).", err=True)
            click.echo(self._message(success=primary is None), err=True, color=False)
        except BaseException as warning_error:
            if primary is None:
                raise
            if reported is not None:
                raise reported from warning_error
            raise primary from warning_error
        if reported is not None:
            raise reported from exc
        if exc is None and primary is not None:
            raise primary
        return False

    def _message(self, *, success: bool) -> str:
        """Return fixed safe text; only complete command success asserts verification."""
        if self.artifact:
            count = len(self._affected)
            if success:
                return (
                    f"Warning: ETag changed for {count} blobs; restarted them as "
                    "full-stream downloads and verified their digests."
                )
            return (
                f"Warning: ETag changed for {count} blobs; full-stream fallbacks were "
                "attempted, but the operation did not complete."
            )
        if success:
            digest = next(iter(self._affected))
            return (
                f"Warning: ETag changed during ranged download of {digest}; restarted "
                "as a full-stream download and verified the digest."
            )
        return (
            "Warning: ETag changed during ranged download; a full-stream fallback was "
            "attempted, but the operation did not complete."
        )

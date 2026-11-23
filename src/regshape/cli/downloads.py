"""Validate shared download controls and defer safe warnings until command cleanup.

The collector never prints from a status callback. Commands place this scope
outside telemetry, progress and result output; it owns neither workers nor sinks.
"""

import io
import logging
import os
import sys
from collections.abc import Callable
from types import TracebackType
from typing import IO, Any, Literal, final

import click
from click.core import ParameterSource

from regshape.cli.progress import (
    ProgressDisplay,
    _redirect_stderr_handlers,
    _restore_stderr_handlers,
)
from regshape.libs.blobs.download import DownloadOptions, DownloadStatus
from regshape.libs.decorators import get_telemetry_config

if sys.platform == "win32":
    import ctypes
    import msvcrt


def download_options(function: Callable[..., Any]) -> Callable[..., Any]:
    """Attach shared pooled/range/legacy controls without resolving credentials or opening files."""
    for option in reversed((
        click.option("--concurrency", type=click.IntRange(1, 32), default=4, show_default=True,
                     help="Maximum concurrent download requests."),
        click.option("--ranges-per-blob", type=click.IntRange(1, 32), default=1, show_default=True,
                     help="Concurrent ranges per blob; 1 uses a full GET."),
        click.option("--range-size", type=click.IntRange(65_536, 67_108_864),
                     default=8_388_608, show_default=True, metavar="BYTES",
                     help="HTTP range bytes, separate from the streaming I/O buffer."),
        click.option("--sequential", is_flag=True,
                     help="Use one pooled request at a time (not legacy transport)."),
        click.option("--legacy-transport", is_flag=True,
                     help="Use the original serial transport without pooled status/retry controls."),
        click.option("--download-retries", type=click.IntRange(0, 10), default=None,
                     metavar="N", help="Pooled retry budget, including 429 (default: 3; 0 disables)."),
        click.option("--scratch-dir", type=click.Path(exists=True, file_okay=False), default=None,
                     metavar="PATH", help="Existing trusted root for bounded private range spools."),
    )):
        function = option(function)
    return function


def download_policy(
    ctx: click.Context, *, concurrency: int, ranges_per_blob: int, range_size: int,
    sequential: bool, legacy_transport: bool, download_retries: int | None,
    scratch_dir: str | None,
) -> DownloadOptions:
    """Map explicit Click controls to SDK policy, rejecting conflicts before credential lookup."""
    explicit = {
        name for name in (
            "concurrency", "ranges_per_blob", "sequential", "download_retries", "scratch_dir",
        )
        if ctx.get_parameter_source(name) not in (None, ParameterSource.DEFAULT)
    }
    if legacy_transport and explicit:
        raise click.UsageError("--legacy-transport conflicts with explicit pooled download controls")
    if sequential and explicit.intersection(("concurrency", "ranges_per_blob")):
        raise click.UsageError("--sequential conflicts with explicit --concurrency/--ranges-per-blob")
    try:
        return DownloadOptions(
            concurrency=1 if sequential or legacy_transport else concurrency,
            ranges_per_blob=1 if sequential or legacy_transport else ranges_per_blob,
            range_size=range_size, max_retries=download_retries, scratch_dir=scratch_dir,
            transport="legacy" if legacy_transport else "pooled",
        )
    except ValueError as error:
        raise click.BadParameter(str(error)) from error


class _DownloadOutput(io.TextIOBase, IO[str]):
    """Track diagnostic failures throughout one command without owning its stderr.

    Existing display cleanup keeps its repaint behavior; the outer warning
    collector uses the persistent flag to avoid any additional diagnostics.
    ``failure`` retains the first exact sink exception for operation boundaries
    that must distinguish diagnostic I/O from acquisition I/O.
    """

    def __init__(self, stream: IO[str]) -> None:
        """Borrow a text sink before telemetry setup or progress suppression is decided."""
        super().__init__()
        self.stream = stream
        self.failed = False
        self.failure: BaseException | None = None

    def write(self, text: str) -> int:
        """Record a real text-write failure, excluding Click's empty binary probes."""
        if not isinstance(text, str):
            raise TypeError("Download diagnostics require text")
        try:
            return self.stream.write(text)
        except BaseException as error:
            self.failed = True
            if self.failure is None:
                self.failure = error
            raise

    def flush(self) -> None:
        """Record flush failures without changing their exception identity."""
        try:
            self.stream.flush()
        except BaseException as error:
            self.failed = True
            if self.failure is None:
                self.failure = error
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

    @property
    def encoding(self) -> str | None:  # type: ignore[override]  # TextIOBase's runtime codec is read-only/nullable.
        """Preserve the borrowed sink's codec for encoding-safe progress glyph selection."""
        return getattr(self.stream, "encoding", None)

    @property
    def closed(self) -> bool:
        """Expose borrowed-sink closure so deferred telemetry never flushes a closed destination."""
        return self.stream.closed

    def close(self) -> None:
        """Never close or flush the borrowed sink, including during adapter destruction."""


def _raise_silent_exit(exc: BaseException | None) -> None:
    """Convert Click-reportable errors to silent exits, leaving other primary errors alone."""
    if isinstance(exc, (KeyboardInterrupt, EOFError, click.Abort)):
        raise click.exceptions.Exit(1) from exc
    if isinstance(exc, click.ClickException):
        raise click.exceptions.Exit(exc.exit_code) from exc


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
        if self._output is not None and self._shares_result_sink():
            self._output.failed = True

    def _shares_result_sink(self) -> bool:
        """Recognize shared objects or OS destinations without writing to either stream.

        If descriptors or destination identities are unavailable, only object
        identity can establish sharing; unrelated healthy stderr remains usable.
        """
        if sys.stdout is self._stderr or sys.stdout is sys.stderr:
            return True
        if self._stderr is None:
            return False
        try:
            output_fd, error_fd = sys.stdout.fileno(), self._stderr.fileno()
            if output_fd == error_fd:
                return True
            if sys.platform == "win32":
                # Windows pipes have zero stat identities; compare kernel handles instead.
                compare = getattr(ctypes.WinDLL("kernelbase"), "CompareObjectHandles", None)
                if compare is not None:
                    compare.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                    compare.restype = ctypes.c_int
                    return bool(compare(
                        msvcrt.get_osfhandle(output_fd), msvcrt.get_osfhandle(error_fd),
                    ))
            output_stat, error_stat = os.fstat(output_fd), os.fstat(error_fd)
            return output_stat.st_ino != 0 and os.path.samestat(output_stat, error_stat)
        except (AttributeError, OSError, ValueError):
            return False

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
            (self._output is not None and self._output.failed)
            or (self.display is not None and self.display.sink_failed)
        ):
            _raise_silent_exit(exc)
            return False
        if not self._affected:
            return False
        primary = exc
        if primary is None:
            try:
                sys.stdout.flush()
            except BaseException as flush_error:
                if self._shares_result_sink():
                    _raise_silent_exit(flush_error)
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
                if self._output is not None and self._output.failed:
                    _raise_silent_exit(warning_error)
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

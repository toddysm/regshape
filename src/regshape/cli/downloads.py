"""Collect safe download status and emit one warning after command output/cleanup.

The collector never prints from a status callback. Commands place this scope
outside telemetry, progress and result output; it owns neither workers nor sinks.
"""

import sys
from types import TracebackType
from typing import Literal, final

import click

from regshape.cli.progress import ProgressDisplay
from regshape.libs.blobs.download import DownloadStatus


@final
class DownloadWarnings:
    """Coalesce changed-ETag observations by content address, independently of redraws.

    Only validated digests are retained, not part events, ETags, URLs or diagnostic
    messages. The set is bounded by the invocation's distinct blob inputs/graph
    budget. A borrowed display identifies a failed terminal sink, which must not
    be invoked again to deliver a warning.
    """

    def __init__(self, *, artifact: bool = False) -> None:
        """Select count-based artifact wording or the standalone verified digest."""
        self.artifact = artifact
        self.display: ProgressDisplay | None = None
        self._affected: set[str] = set()

    def __enter__(self) -> "DownloadWarnings":
        """Begin collection before the command creates telemetry or transfer resources."""
        return self

    def observe(self, event: DownloadStatus) -> None:
        """Remember a fixed reason once per blob, without output or completion claims."""
        if event.reason == "etag_changed" and event.digest is not None:
            self._affected.add(event.digest)

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
        if not self._affected or (self.display is not None and self.display.sink_failed):
            return False
        primary = exc
        if primary is None:
            try:
                sys.stdout.flush()
            except BaseException as flush_error:
                if sys.stdout is sys.stderr:
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
        if exc is None and primary is not None:
            raise primary
        if reported is not None:
            raise reported from exc
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

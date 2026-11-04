"""Shared response sizing, ownership and secondary-failure diagnostics.

These internal helpers do not consume response bodies or render terminal output.
Keeping them outside the transport package avoids decorator/client import cycles.
"""

import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Literal

import requests

logger = logging.getLogger(__name__)


def download_total(headers: Mapping[str, str]) -> int | None:
    """Return a length comparable with consumed bytes, or None for unusable metadata."""
    if headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        return None
    if headers.get("Transfer-Encoding"):
        return None
    raw = headers.get("Content-Length", "").strip()
    if not raw.isascii() or not raw.isdecimal():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def report_secondary_failure(
    primary: BaseException,
    secondary: BaseException,
    context: str,
) -> None:
    """Log a secondary failure without replacing the active primary exception.

    Only exception types are logged. If logging itself fails, re-raise the
    primary and let Python retain secondary context without replacing its cause.
    """
    try:
        logger.error(
            "%s failed (%s); preserving the original error",
            context,
            type(secondary).__name__,
        )
    except BaseException:  # noqa: BLE001 - preserve the active primary error and its cause
        raise primary


def close_response(
    response: requests.Response,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Close an owned response; propagate a sole close failure, preserve an active error."""
    try:
        response.close()
    except BaseException as error:
        if primary_error is None:
            raise
        report_secondary_failure(primary_error, error, "Response cleanup")


class ClosingResponse:
    """Own a response without assigning attributes on caller exceptions.

    A class-based exit also supports immutable exceptions: generator context
    managers restore their traceback with a Python attribute assignment.
    """

    def __init__(self, response: requests.Response) -> None:
        """Take ownership of an acquired response; do not read its body."""
        self.response = response

    def __enter__(self) -> requests.Response:
        """Expose the owned response for bounded-scope consumption."""
        return self.response

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Close on every exit and preserve an active operation/cancellation error."""
        close_response(self.response, primary_error=exc)
        return False

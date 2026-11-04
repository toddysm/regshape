"""Shared response sizing, ownership and secondary-failure diagnostics.

These internal helpers do not consume response bodies or render terminal output.
Keeping them outside the transport package avoids decorator/client import cycles.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

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

    Only exception types are logged. If logging itself fails, retain those
    failures as exception context without altering the primary identity/cause.
    """
    try:
        logger.error(
            "%s failed (%s); preserving the original error",
            context,
            type(secondary).__name__,
        )
    except BaseException as diagnostic_error:  # noqa: BLE001 - preserve the primary failure
        if secondary is not primary:
            secondary.__context__ = None
        if diagnostic_error is primary:
            if secondary is not primary:
                primary.__context__ = secondary
        else:
            diagnostic_error.__context__ = (
                secondary
                if secondary is not primary and secondary is not diagnostic_error
                else None
            )
            primary.__context__ = diagnostic_error


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


@contextmanager
def closing_response(response: requests.Response) -> Iterator[requests.Response]:
    """Own a response until scope exit, preserving an operation error during cleanup."""
    try:
        yield response
    except BaseException as error:
        close_response(response, primary_error=error)
        raise
    else:
        close_response(response)

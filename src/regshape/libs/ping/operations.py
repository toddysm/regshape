#!/usr/bin/env python3

"""
:mod:`regshape.libs.ping.operations` - OCI registry ping operations
=====================================================================

.. module:: regshape.libs.ping.operations
   :platform: Unix, Windows
   :synopsis: Library-level function for pinging an OCI Distribution-compliant
              registry via ``GET /v2/``.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

The function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  It is intentionally free of Click/CLI concerns —
authentication outcomes can be returned as data; rendering is the caller's
responsibility.
"""

import dataclasses
import time

import requests

from regshape.libs.errors import AuthError, PingError
from regshape.libs.transport import RegistryClient


# ===========================================================================
# Data models
# ===========================================================================


@dataclasses.dataclass
class PingResult:
    """Result of a ``GET /v2/`` ping against an OCI registry.

    :param reachable: ``True`` for HTTP 200 or an authentication-required outcome.
        This does not guarantee that credentials grant access.
    :param status_code: HTTP status code, or ``None`` when authentication
        negotiation raised without returning a response.
    :param api_version: Value of the ``Docker-Distribution-API-Version``
        response header, or ``None`` if absent.
    :param latency_ms: Round-trip time in milliseconds, or ``None`` when no
        response was returned.
    :param auth_required: Whether authentication is required or failed.
    :param error: Authentication diagnostic, or ``None`` for ordinary responses.
    """

    reachable: bool
    status_code: int | None
    api_version: str | None
    latency_ms: float | None
    auth_required: bool = False
    error: str | None = None

    @classmethod
    def from_auth_error(
        cls,
        error: AuthError,
        *,
        status_code: int | None = None,
        api_version: str | None = None,
        latency_ms: float | None = None,
    ) -> "PingResult":
        """Represent an authentication failure without inferring HTTP metadata.

        :param error: Original diagnostic, preserved as text.
        :param status_code: Known response status; do not infer it from error text.
        :param api_version: Known API-version header, if a response was returned.
        :param latency_ms: Measured round-trip milliseconds, if available.
        :returns: A reachable, authentication-required result. Omitted metadata
            remains ``None`` for exceptions from credential/token negotiation.
        """
        return cls(
            reachable=True,
            status_code=status_code,
            api_version=api_version,
            latency_ms=latency_ms,
            auth_required=True,
            error=str(error),
        )

    def to_dict(self) -> dict:
        """Serialise all SDK fields, including authentication state and nulls."""
        return {
            "reachable": self.reachable,
            "status_code": self.status_code,
            "api_version": self.api_version,
            "latency_ms": self.latency_ms,
            "auth_required": self.auth_required,
            "error": self.error,
        }


# ===========================================================================
# Public domain operations
# ===========================================================================


def ping(client: RegistryClient, *, raise_on_auth: bool = True) -> PingResult:
    """Ping the registry by issuing ``GET /v2/``.

    A successful 200 response confirms that the registry is reachable and
    speaks the OCI Distribution API. By default, preserve the original SDK
    behavior: HTTP 401 and transport authentication errors raise ``AuthError``;
    other non-200 responses (including raw HTTP 403) are not reachable.

    With ``raise_on_auth=False``, HTTP 401/403 and transport ``AuthError``
    outcomes instead return a reachable, authentication-required result.
    A transport authentication error has no response metadata; its original
    diagnostic is preserved without guessing a status from its text.

    :param client: Authenticated transport client for the target registry.
    :param raise_on_auth: Keep legacy authentication behavior when ``True``.
    :returns: A :class:`PingResult` describing the outcome.
    :raises AuthError: On HTTP 401 or transport authentication failure when
        ``raise_on_auth`` is ``True``.
    :raises PingError: On connection, DNS, timeout, or other request errors.
    """
    try:
        start = time.monotonic()
        response = client.get("/v2/")
        elapsed_ms = (time.monotonic() - start) * 1000
    except AuthError as exc:
        if raise_on_auth:
            raise
        return PingResult.from_auth_error(exc)
    except requests.exceptions.ConnectionError as exc:
        raise PingError(
            f"Registry {client.config.registry} is not reachable",
            str(exc),
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise PingError(
            f"Registry {client.config.registry} is not reachable",
            "Connection timed out",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PingError(
            f"Registry {client.config.registry} is not reachable",
            str(exc),
        ) from exc

    api_version = response.headers.get("Docker-Distribution-API-Version")
    latency_ms = round(elapsed_ms, 1)

    if response.status_code in (401, 403):
        error = AuthError(
            f"Registry {client.config.registry} requires authentication",
            f"HTTP {response.status_code}",
        )
        if not raise_on_auth:
            return PingResult.from_auth_error(
                error,
                status_code=response.status_code,
                api_version=api_version,
                latency_ms=latency_ms,
            )
        if response.status_code == 401:
            raise error

    return PingResult(
        reachable=response.status_code == 200,
        status_code=response.status_code,
        api_version=api_version,
        latency_ms=latency_ms,
    )

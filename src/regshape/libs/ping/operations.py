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
error reporting is the caller's responsibility.
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

    :param reachable: ``True`` if the registry returned HTTP 200.
    :param status_code: HTTP status code returned by the registry.
    :param api_version: Value of the ``Docker-Distribution-API-Version``
        response header, or ``None`` if absent.
    :param latency_ms: Round-trip time in milliseconds.
    """

    reachable: bool
    status_code: int
    api_version: str | None
    latency_ms: float

    def to_dict(self) -> dict:
        """Serialise the result to a plain dictionary."""
        return {
            "reachable": self.reachable,
            "status_code": self.status_code,
            "api_version": self.api_version,
            "latency_ms": self.latency_ms,
        }


# ===========================================================================
# Public domain operations
# ===========================================================================


def ping(client: RegistryClient) -> PingResult:
    """Ping the registry by issuing ``GET /v2/``.

    A successful 200 response confirms that the registry is reachable and
    speaks the OCI Distribution API.

    :param client: Authenticated transport client for the target registry.
    :returns: A :class:`PingResult` describing the outcome.
    :raises AuthError: On HTTP 401 (authentication required or failed).
    :raises PingError: On connection, DNS, or timeout errors.
    """
    try:
        start = time.monotonic()
        response = client.get("/v2/")
        elapsed_ms = (time.monotonic() - start) * 1000
    except AuthError:
        raise
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

    if response.status_code == 401:
        raise AuthError(
            f"Registry {client.config.registry} requires authentication",
            f"HTTP {response.status_code}",
        )

    api_version = response.headers.get("Docker-Distribution-API-Version")

    return PingResult(
        reachable=response.status_code == 200,
        status_code=response.status_code,
        api_version=api_version,
        latency_ms=round(elapsed_ms, 1),
    )

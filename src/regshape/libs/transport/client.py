#!/usr/bin/env python3

"""
:mod:`regshape.libs.transport.client` - RegistryClient and TransportConfig
===========================================================================

.. module:: regshape.libs.transport.client
   :platform: Unix, Windows
   :synopsis: ``RegistryClient`` is the single HTTP entry point for all OCI
              registry communication. It resolves credentials once on
              construction, handles the ``WWW-Authenticate`` challenge /
              401 → authenticate → retry cycle, and delegates every actual
              HTTP call to :func:`~regshape.libs.decorators.call_details.http_request`
              so that ``--debug-calls`` telemetry is available for free.

              ``TransportConfig`` is a plain dataclass that carries the
              per-registry connection settings.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from dataclasses import dataclass, field
from typing import Optional

import requests

from regshape.libs.auth import registryauth
from regshape.libs.auth.credentials import resolve_credentials
from regshape.libs.decorators.call_details import http_request
from regshape.libs.errors import AuthError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TransportConfig:
    """Per-registry connection settings for :class:`RegistryClient`.

    :param registry: Registry hostname, optionally with port
        (e.g. ``"acr.io"``, ``"localhost:5000"``). Must not include a scheme.
    :param insecure: When ``True``, use ``http://`` instead of ``https://``.
        Defaults to ``False``.
    :param username: Explicit username for authentication. When ``None``,
        credentials are resolved from the credential store at construction
        time.  Supply both *username* and *password* to bypass credential
        store lookup.
    :param password: Explicit password. See *username*.
    :param timeout: Per-request timeout in seconds. Defaults to ``30``.
    """

    registry: str
    insecure: bool = False
    username: Optional[str] = None
    password: Optional[str] = None
    timeout: int = 30

    def __post_init__(self) -> None:
        if not self.registry:
            raise ValueError("TransportConfig.registry must not be empty")
        if "://" in self.registry:
            raise ValueError(
                "TransportConfig.registry must be a hostname, not a URL "
                f"(got {self.registry!r}). Remove the scheme prefix."
            )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class RegistryClient:
    """HTTP client for OCI registry communication.

    All domain operation modules and CLI commands construct one
    ``RegistryClient`` per invocation and pass it down rather than making
    raw ``requests`` calls themselves.  This ensures:

    - Credentials are resolved exactly once (via the credential store chain
      if not supplied explicitly).
    - The 401 → ``WWW-Authenticate`` → authenticate → retry cycle is
      implemented in one place.
    - Every HTTP call goes through
      :func:`~regshape.libs.decorators.call_details.http_request`, so
      ``--debug-calls`` telemetry works across the entire CLI without any
      per-command instrumentation.

    :param config: Connection settings for the target registry.
    """

    def __init__(self, config: TransportConfig) -> None:
        self.config = config
        # Resolve credentials once — domain modules and CLI commands never
        # call resolve_credentials() directly.
        self._username, self._password = resolve_credentials(
            config.registry, config.username, config.password
        )
        # Store the last response for access to headers (e.g., pagination)
        self.last_response: Optional[requests.Response] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Fully-qualified base URL for this registry.

        :returns: ``"https://registry"`` or ``"http://registry"`` depending on
            :attr:`TransportConfig.insecure`.
        """
        scheme = "http" if self.config.insecure else "https"
        return f"{scheme}://{self.config.registry}"

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        headers: Optional[dict] = None,
        **kwargs,
    ) -> requests.Response:
        """Issue an authenticated HTTP request to the registry.

        Builds the full URL from :attr:`base_url` + *path*, then:

        1. Sends the request without an ``Authorization`` header.
        2. If the response is **401** and the registry sends a
           ``WWW-Authenticate`` header, parses the challenge, obtains a
           token or Basic credential, and retries with the
           ``Authorization`` header attached.
        3. Returns the final :class:`requests.Response` (caller decides
           whether the status code is acceptable).

        ``--debug-calls`` output is emitted automatically for every call
        because :func:`~regshape.libs.decorators.call_details.http_request`
        is decorated with ``@debug_call``.

        :param method: HTTP method (``"GET"``, ``"HEAD"``, ``"PUT"``, etc.).
        :param path: Path component of the URL, starting with ``/``
            (e.g. ``"/v2/myrepo/manifests/latest"``).
        :param headers: Optional request headers. A shallow copy is taken so
            that the caller's dict is never mutated.
        :param kwargs: Additional keyword arguments forwarded to
            :func:`~regshape.libs.decorators.call_details.http_request`
            (e.g. ``data``, ``params``, ``stream``).
        :returns: The HTTP response from the registry.
        :raises AuthError: If authentication fails — either no
            ``WWW-Authenticate`` header was returned with the 401, the scheme
            is Basic but no credentials are available, or token exchange fails.
        :raises requests.exceptions.RequestException: On transport errors
            (connection refused, timeout, TLS, etc.).
        """
        url = f"{self.base_url}{path}"
        req_headers = dict(headers) if headers else {}
        timeout = kwargs.pop("timeout", self.config.timeout)

        response = http_request(url, method, headers=req_headers, timeout=timeout, **kwargs)
        self.last_response = response

        if response.status_code != 401:
            return response

        # ── 401 handling ────────────────────────────────────────────────
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {self.config.registry!r} returned 401 without "
                "a WWW-Authenticate header",
            )

        auth_scheme = www_auth.split(" ", 1)[0]
        if auth_scheme.lower() == "basic" and (
            self._username is None or self._password is None
        ):
            raise AuthError(
                "Authentication failed",
                "Registry requested Basic authentication but no credentials "
                "are available for "
                f"{self.config.registry!r}. Run 'regshape auth login' first.",
            )

        normalized_www_auth, normalized_scheme = _normalize_www_authenticate(www_auth)
        auth_value = registryauth.authenticate(
            normalized_www_auth, self._username, self._password
        )
        req_headers["Authorization"] = f"{normalized_scheme} {auth_value}"

        response = http_request(url, method, headers=req_headers, timeout=timeout, **kwargs)
        self.last_response = response
        return response

    # ------------------------------------------------------------------
    # HTTP method convenience wrappers
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated GET request.

        :param path: URL path (e.g. ``"/v2/repo/manifests/tag"``).
        """
        return self.request("GET", path, **kwargs)

    def head(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated HEAD request.

        :param path: URL path.
        """
        return self.request("HEAD", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated PUT request.

        :param path: URL path.
        """
        return self.request("PUT", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated POST request.

        :param path: URL path.
        """
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated PATCH request.

        :param path: URL path.
        """
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated DELETE request.

        :param path: URL path.
        """
        return self.request("DELETE", path, **kwargs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_www_authenticate(www_auth: str) -> tuple[str, str]:
    """Normalize a ``WWW-Authenticate`` header value.

    - Capitalises ``basic`` / ``bearer`` scheme names so that
      :func:`~regshape.libs.auth.registryauth.authenticate` receives the
      expected casing.
    - Strips whitespace from each comma-separated parameter to prevent parse
      failures when a registry emits
      ``Bearer realm=\"...\", service=\"...\"`` (space after the comma).

    :param www_auth: Raw ``WWW-Authenticate`` header value.
    :returns: ``(normalized_www_auth, normalized_scheme)`` tuple where
        *normalized_www_auth* is the normalised full value and
        *normalized_scheme* is the scheme token used to build the
        ``Authorization`` header (e.g. ``"Bearer"``).
    """
    scheme, sep, params = www_auth.partition(" ")
    normalized_scheme = (
        scheme.capitalize() if scheme.lower() in ("basic", "bearer") else scheme
    )
    if sep and params:
        cleaned_params = ",".join(part.strip() for part in params.split(","))
        normalized_www_auth = f"{normalized_scheme} {cleaned_params}"
    else:
        normalized_www_auth = normalized_scheme
    return normalized_www_auth, normalized_scheme

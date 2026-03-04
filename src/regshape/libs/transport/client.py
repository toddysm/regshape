#!/usr/bin/env python3

"""
:mod:`regshape.libs.transport.client` - RegistryClient and TransportConfig
===========================================================================

.. module:: regshape.libs.transport.client
   :platform: Unix, Windows
   :synopsis: RegistryClient is the single HTTP entry point for all OCI
              registry communication. It resolves credentials once on
              construction, handles the WWW-Authenticate challenge /
              401 -> authenticate -> retry cycle, and delegates every actual
              HTTP call to http_request so that --debug-calls telemetry
              is available for free.

              TransportConfig is a plain dataclass that carries the
              per-registry connection settings.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from dataclasses import dataclass, field
from typing import Optional, List

import requests

from regshape.libs.auth import registryauth
from regshape.libs.auth.credentials import resolve_credentials
from regshape.libs.decorators.call_details import http_request
from regshape.libs.errors import AuthError
from regshape.libs.transport.middleware import (
    MiddlewarePipeline, Middleware, AuthMiddleware, LoggingMiddleware,
    RetryMiddleware, CachingMiddleware, RetryConfig
)
from regshape.libs.transport.models import RegistryRequest, RegistryResponse


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TransportConfig:
    """Per-registry connection settings for RegistryClient.

    :param registry: Registry hostname, optionally with port
        (e.g. "acr.io", "localhost:5000"). Must not include a scheme.
    :param insecure: When True, use http:// instead of https://.
        Defaults to False.
    :param username: Explicit username for authentication. When None,
        credentials are resolved from the credential store at construction
        time.  Supply both username and password to bypass credential
        store lookup.
    :param password: Explicit password. See username.
    :param timeout: Per-request timeout in seconds. Defaults to 30.
    :param enable_middleware: When True, enables the middleware pipeline
        for request processing. Defaults to True.
    :param enable_logging: When True, adds logging middleware to the
        pipeline. Defaults to False.
    :param enable_retries: When True, adds retry middleware with exponential
        backoff. Defaults to False.
    :param enable_caching: When True, adds caching middleware for GET
        requests. Defaults to False.
    :param retry_config: Configuration for retry middleware. Only used when
        enable_retries is True.
    :param cache_size: Maximum number of cached responses. Only used when
        enable_caching is True. Defaults to 100.
    :param cache_ttl: Time-to-live in seconds for cached responses.
        ``None`` means entries never expire, which is appropriate for
        content-addressed registry objects (manifests, blobs). Only used
        when enable_caching is True. Defaults to ``None``.
    :param middlewares: Additional custom middleware to add to the pipeline.
        These are added after the built-in middleware.
    """

    registry: str
    insecure: bool = False
    username: Optional[str] = None
    password: Optional[str] = None
    timeout: int = 30
    enable_middleware: bool = True
    enable_logging: bool = False
    enable_retries: bool = False
    enable_caching: bool = False
    retry_config: Optional[RetryConfig] = None
    cache_size: int = 100
    cache_ttl: Optional[float] = None
    middlewares: List[Middleware] = field(default_factory=list)

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
    RegistryClient per invocation and pass it down rather than making
    raw requests calls themselves.  This ensures:

    - Credentials are resolved exactly once (via the credential store chain
      if not supplied explicitly).
    - The 401 -> WWW-Authenticate -> authenticate -> retry cycle is
      implemented in one place.
    - Every HTTP call goes through http_request, so --debug-calls telemetry 
      works across the entire CLI without any per-command instrumentation.

    :param config: Connection settings for the target registry.
    """

    def __init__(self, config: TransportConfig) -> None:
        self.config = config
        # Resolve credentials once - domain modules and CLI commands never
        # call resolve_credentials() directly.
        self._username, self._password = resolve_credentials(
            config.registry, config.username, config.password
        )
        # Store the last response for access to headers (e.g., pagination)
        self.last_response: Optional[requests.Response] = None
        
        # Initialize middleware pipeline if enabled
        self._pipeline: Optional[MiddlewarePipeline] = None
        if config.enable_middleware:
            self._pipeline = self._setup_middleware_pipeline()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Fully-qualified base URL for this registry.

        :returns: "https://registry" or "http://registry" depending on
            TransportConfig.insecure.
        """
        scheme = "http" if self.config.insecure else "https"
        return f"{scheme}://{self.config.registry}"

    # ------------------------------------------------------------------
    # Middleware setup
    # ------------------------------------------------------------------

    def _setup_middleware_pipeline(self) -> MiddlewarePipeline:
        """Set up the middleware pipeline based on configuration."""
        pipeline = MiddlewarePipeline()
        
        # Add authentication middleware if credentials are available
        if self._username and self._password:
            # Create credentials object
            credentials_obj = type('BasicCredentials', (), {
                'username': self._username,
                'password': self._password
            })()
            pipeline.add_middleware(AuthMiddleware(credentials_obj))
        
        # Add logging middleware if enabled
        if self.config.enable_logging:
            pipeline.add_middleware(LoggingMiddleware("regshape.transport"))
        
        # Add retry middleware if enabled
        if self.config.enable_retries:
            retry_config = self.config.retry_config or RetryConfig()
            pipeline.add_middleware(RetryMiddleware(retry_config))
        
        # Add caching middleware if enabled
        if self.config.enable_caching:
            pipeline.add_middleware(
                CachingMiddleware(self.config.cache_size, ttl=self.config.cache_ttl)
            )
        
        # Add custom middleware
        for middleware in self.config.middlewares:
            pipeline.add_middleware(middleware)
        
        return pipeline

    def _terminal_handler(self, request: RegistryRequest) -> RegistryResponse:
        """Terminal handler that performs the actual HTTP request.
        
        This is the final handler in the middleware pipeline that converts
        RegistryRequest to a requests call and wraps the response.
        """
        # Convert RegistryRequest to requests parameters
        url = f"{self.base_url}{request.url}" if request.url.startswith('/') else request.url
        
        # Use http_request for telemetry (--debug-calls)
        response = http_request(
            url=url,
            method=request.method,
            headers=request.headers,
            data=request.body,
            stream=request.stream,
            params=request.params,
            timeout=request.timeout or self.config.timeout
        )
        
        # Store for backward compatibility
        self.last_response = response
        
        # Convert to RegistryResponse.
        # For streaming requests, from_requests_response(stream=True) avoids
        # reading response.content so that the streaming iterator is preserved
        # for callers (e.g. blob downloads).
        return RegistryResponse.from_requests_response(response, stream=request.stream)

    def _legacy_authenticate_and_retry(
        self, 
        method: str, 
        url: str, 
        req_headers: dict, 
        timeout: int, 
        **kwargs
    ) -> requests.Response:
        """Legacy authentication handling for when middleware is disabled."""
        response = http_request(url, method, headers=req_headers, timeout=timeout, **kwargs)
        self.last_response = response

        if response.status_code != 401:
            return response

        # -- 401 handling --------------------------------------------------------
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

        When middleware is enabled (default), processes the request through 
        the middleware pipeline which can provide authentication, logging,
        retries, caching, and custom middleware.

        When middleware is disabled, falls back to the legacy implementation
        that handles 401/WWW-Authenticate challenges manually.

        --debug-calls output is emitted automatically for every call
        because the terminal handler uses http_request from decorators.

        :param method: HTTP method (GET, HEAD, PUT, etc.).
        :param path: Path component of the URL, starting with /
            (e.g. /v2/myrepo/manifests/latest).
        :param headers: Optional request headers. A shallow copy is taken so
            that the caller dict is never mutated.
        :param kwargs: Additional keyword arguments forwarded to the HTTP
            request (e.g. data, params, stream).
        :returns: The HTTP response from the registry.
        :raises AuthError: If authentication fails.
        :raises requests.exceptions.RequestException: On transport errors.
        """
        req_headers = dict(headers) if headers else {}
        timeout = kwargs.pop("timeout", self.config.timeout)
        
        if self._pipeline is not None:
            # Use middleware pipeline
            registry_request = RegistryRequest(
                method=method,
                url=path,
                headers=req_headers,
                body=kwargs.get('data'),
                stream=kwargs.get('stream', False),
                params=kwargs.get('params'),
                timeout=timeout
            )
            
            try:
                registry_response = self._pipeline.execute(registry_request, self._terminal_handler)
                # Ensure last_response is available for backward compatibility
                # even after middleware processing
                self.last_response = registry_response.raw_response
                return registry_response.raw_response
            except Exception as exc:
                # Handle authentication errors that might come from middleware
                if isinstance(exc, AuthError):
                    # Re-raise AuthError with its original traceback
                    raise
                # Re-raise other middleware errors while preserving their tracebacks
                raise
        else:
            # Legacy path - direct implementation without middleware
            url = f"{self.base_url}{path}"
            return self._legacy_authenticate_and_retry(
                method, url, req_headers, timeout, **kwargs
            )

    # ------------------------------------------------------------------
    # HTTP method convenience wrappers
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> requests.Response:
        """Issue an authenticated GET request.

        :param path: URL path (e.g. "/v2/repo/manifests/tag").
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
    """Normalize a WWW-Authenticate header value.

    - Capitalises basic / bearer scheme names so that
      registryauth.authenticate receives the expected casing.
    - Strips whitespace from each comma-separated parameter to prevent parse
      failures when a registry emits
      Bearer realm="...", service="..." (space after the comma).

    :param www_auth: Raw WWW-Authenticate header value.
    :returns: (normalized_www_auth, normalized_scheme) tuple where
        normalized_www_auth is the normalised full value and
        normalized_scheme is the scheme token used to build the
        Authorization header (e.g. "Bearer").
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

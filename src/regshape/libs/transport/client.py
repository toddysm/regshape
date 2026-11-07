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
              is available for free. Only responses from the configured registry
              origin may initiate credential negotiation, including auth probes.

              TransportConfig is a plain dataclass that carries the
              per-registry connection settings.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import copy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, List, Optional

import requests

from regshape.libs._http import ClosingResponse
from regshape.libs.auth import registryauth
from regshape.libs.auth.credentials import resolve_credentials
from regshape.libs.decorators.call_details import http_request
from regshape.libs.errors import AuthError
from regshape.libs.transport.body import _UploadBody
from regshape.libs.transport.middleware import (
    AuthMiddleware,
    CachingMiddleware,
    LoggingMiddleware,
    Middleware,
    MiddlewarePipeline,
    NextHandler,
    RetryConfig,
    RetryMiddleware,
    _normalize_www_authenticate,
)
from regshape.libs.transport.models import RegistryRequest, RegistryResponse
from regshape.libs.transport.urls import require_authentication_origin

if TYPE_CHECKING:
    from regshape.libs.transport.download import DownloadTransport


def _send_http_request(*args: Any, **kwargs: Any) -> requests.Response:
    """Prepare counted bodies and optional pre-redirect hooks without changing ordinary calls."""
    response_hook = kwargs.pop("response_hook", None)
    if response_hook is not None:
        kwargs["hooks"] = {"response": response_hook}
    body = kwargs.get("data")
    if isinstance(body, _UploadBody):
        body.prepare_attempt()
        try:
            return http_request(*args, **kwargs)
        finally:
            body.raise_if_failed()
    return http_request(*args, **kwargs)


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
    :param enable_caching: When True, caches buffered GET responses. Streamed
        requests bypass the cache to avoid exhausted-response reuse. Defaults to False.
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
        """Resolve credentials once and construct private request/middleware state."""
        self.config = config
        # Resolve credentials once - domain modules and CLI commands never
        # call resolve_credentials() directly.
        self._username, self._password = resolve_credentials(
            config.registry, config.username, config.password
        )
        # Store the last response for access to headers (e.g., pagination)
        self.last_response: Optional[requests.Response] = None
        self._session: requests.Session | None = None
        self._download_context: DownloadTransport | None = None
        
        # Initialize middleware pipeline if enabled
        self._pipeline: Optional[MiddlewarePipeline] = None
        if config.enable_middleware:
            self._pipeline = self._setup_middleware_pipeline()

    def supports_download_fork(self) -> bool:
        """Check base-client configuration or an explicit custom safe-fork override.

        The base hook only supports its configured built-ins. Subclasses and
        runtime pipeline changes must opt in rather than silently lose behavior.
        """
        if type(self).fork_for_download is not RegistryClient.fork_for_download:
            return True
        if type(self) is not RegistryClient or self.config.middlewares:
            return False
        if set(vars(self)) - {
            "config", "_username", "_password", "last_response",
            "_pipeline", "_session", "_download_context",
        }:
            return False
        if self._download_context is not None:
            return True
        if not self.config.enable_middleware:
            return self._pipeline is None
        if type(self._pipeline) is not MiddlewarePipeline:
            return False
        expected: list[type] = [AuthMiddleware]
        if self.config.enable_logging:
            expected.append(LoggingMiddleware)
        if self.config.enable_retries:
            expected.append(RetryMiddleware)
        if self.config.enable_caching:
            expected.append(CachingMiddleware)
        if [type(item) for item in self._pipeline._middleware] != expected:
            return False
        allowed = {
            AuthMiddleware: {"_username", "_password", "_registry", "_base_url"},
            LoggingMiddleware: {"logger", "level"},
            RetryMiddleware: {"config", "honor_retry_after"},
            CachingMiddleware: {"cache", "max_size", "ttl"},
        }
        if any(set(vars(item)) != allowed[type(item)] for item in self._pipeline._middleware):
            return False
        auth = self._pipeline._middleware[0]
        if not isinstance(auth, AuthMiddleware):
            return False
        return (
            auth._username == self._username and auth._password == self._password
            and auth._base_url == self.base_url and auth._registry == self.config.registry
        )

    def fork_for_download(self, *, context: "DownloadTransport") -> "RegistryClient":
        """Create an isolated worker from the operation's resolved configuration snapshot.

        No credential-store lookup occurs. The worker borrows the context's auth
        provider and connection pool, but owns its headers, cookies, middleware,
        cache and last_response. Custom extensions must explicitly override this
        hook and preserve their semantics with equivalent isolation. Call only
        from the context before worker I/O; the original client is never closed.
        Only built-in settings are copied; the override reconstructs custom
        middleware without copying the original middleware's mutable state.

        :param context: Open operation-owned pooled transport.
        :returns: Fresh base client with worker-local mutable request state.
        :raises ValueError: If customization has no explicit safe override.
        """
        if not self.supports_download_fork():
            raise ValueError("Custom client requires an explicit safe download fork")
        if context.borrowed is not self and self._download_context is not context:
            raise ValueError("Download fork requires its client's operation context")
        worker = object.__new__(RegistryClient)
        worker.config = replace(
            context.config, retry_config=copy.deepcopy(context.config.retry_config), middlewares=[],
        )
        worker._username, worker._password = context.username, context.password
        worker.last_response = None
        worker._download_context = context
        session = context.new_session()
        worker._session = session
        worker._pipeline = MiddlewarePipeline()

        def authenticate(request: RegistryRequest, next_handler: NextHandler) -> RegistryResponse:
            """Bind the shared provider to this worker's private session and request chain."""
            return context.auth.execute(request, next_handler, session)

        worker._pipeline.add_middleware(authenticate)
        if context.logging_config is not None:
            worker._pipeline.add_middleware(LoggingMiddleware(*context.logging_config))
        worker._pipeline.add_middleware(
            RetryMiddleware(copy.deepcopy(context.retry_config), honor_retry_after=True)
        )
        if context.cache_config is not None:
            worker._pipeline.add_middleware(
                CachingMiddleware(*context.cache_config)
            )
        return worker

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
        """Set up the middleware pipeline based on configuration.

        AuthMiddleware is **always** added so that the 401 ->
        WWW-Authenticate -> authenticate -> retry cycle works for
        Basic, Bearer (authenticated), *and* Bearer (anonymous) flows.
        """
        pipeline = MiddlewarePipeline()
        
        # Always add authentication middleware — the 401 challenge /
        # retry cycle must be available even when no credentials are
        # configured (anonymous Bearer-token flow).
        pipeline.add_middleware(
            AuthMiddleware(
                username=self._username,
                password=self._password,
                registry=self.config.registry,
                base_url=self.base_url,
            )
        )
        
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
        RegistryRequest to a requests call and wraps the response. Preserve
        an explicit redirect opt-out before authentication sees the response.
        """
        # Convert RegistryRequest to requests parameters
        url = f"{self.base_url}{request.url}" if request.url.startswith('/') else request.url
        
        # Use http_request for telemetry (--debug-calls)
        redirect_options: dict[str, Any] = {} if request.allow_redirects else {"allow_redirects": False}
        if request.response_hook is not None:
            redirect_options["response_hook"] = request.response_hook
        if self._session is not None:
            redirect_options["session"] = self._session
        response = _send_http_request(
            url=url,
            method=request.method,
            headers=request.headers,
            data=request.body,
            stream=request.stream,
            params=request.params,
            timeout=request.timeout or self.config.timeout,
            **redirect_options,
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
        """Validate 401 issuers before legacy negotiation, disposing owned challenges and probes.

        A foreign/missing-origin 401 fails before header interpretation or the
        no-challenge fallback. Valid external token realms remain registry policy.
        Successful off-origin responses are unchanged; rejected retry responses
        are closed while valid final responses remain caller-owned.
        """
        response = _send_http_request(url, method, headers=req_headers, timeout=timeout, **kwargs)
        self.last_response = response

        if response.status_code != 401:
            return response

        # -- 401 handling --------------------------------------------------------
        require_authentication_origin(response, self.base_url)
        with ClosingResponse(response):
            www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            # Some registries (e.g. ACR) only return WWW-Authenticate on
            # the /v2/ endpoint.  Fall back to a /v2/ probe.
            if kwargs.get("allow_redirects", True) and kwargs.get("response_hook") is None:
                v2_url = f"{self.base_url}/v2/"
                v2_resp = http_request(v2_url, "GET", headers={}, timeout=timeout)
                if v2_resp.status_code == 401:
                    require_authentication_origin(v2_resp, self.base_url)
                with ClosingResponse(v2_resp):
                    if v2_resp.status_code == 401:
                        www_auth = v2_resp.headers.get("WWW-Authenticate", "")
            else:
                original_request = RegistryRequest(
                    method=method, url=url, headers=req_headers,
                    timeout=timeout, allow_redirects=kwargs.get("allow_redirects", True),
                    stream=kwargs.get("stream", False),
                    response_hook=kwargs.get("response_hook"),
                )
                www_auth = AuthMiddleware._probe_v2_challenge(
                    self._terminal_handler, original_request, self.base_url,
                )
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

        response = _send_http_request(url, method, headers=req_headers, timeout=timeout, **kwargs)
        self.last_response = response
        if response.status_code == 401:
            require_authentication_origin(response, self.base_url)
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
            request (e.g. data, params, stream). ``allow_redirects=False``
            disables automatic redirects, including authentication probes
            and retries, in both transport modes.
            ``response_hook`` runs before Requests redirect processing and is
            retained through auth retries/probes; callers own any hook-failure cleanup.
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
                timeout=timeout,
                allow_redirects=kwargs.get("allow_redirects", True),
                response_hook=kwargs.get("response_hook"),
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

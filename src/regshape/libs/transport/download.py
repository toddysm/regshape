"""Operation-owned pooled sessions and single-flight download authentication.

Clients and sessions are worker-local; only the bounded adapter, slot accounting
and scoped auth provider are shared. Callers must close all responses and join
workers before closing this context. No process-global pool or token cache exists.
"""

import copy
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import requests
from urllib3 import ProxyManager

from regshape.libs._http import ClosingResponse, report_secondary_failure
from regshape.libs.auth.download import DownloadToken, negotiate_download_token
from regshape.libs.auth.registryauth import _parse_auth_header
from regshape.libs.errors import AuthError
from regshape.libs.transport.client import RegistryClient, TransportConfig
from regshape.libs.transport.middleware import (
    AuthMiddleware,
    CachingMiddleware,
    LoggingMiddleware,
    RetryConfig,
    RetryMiddleware,
)
from regshape.libs.transport.models import RegistryRequest, RegistryResponse
from regshape.libs.transport.urls import _url_origin, require_authentication_origin

if TYPE_CHECKING:
    from regshape.libs.blobs.download import DownloadOptions

NextHandler = Callable[[RegistryRequest], RegistryResponse]
request_observer: ContextVar[Callable[[int, float], None] | None] = ContextVar(
    "regshape_download_request_observer", default=None,
)
request_lifecycle: ContextVar[Callable[[bool], None] | None] = ContextVar(
    "regshape_download_request_lifecycle", default=None,
)


@dataclass
class _AuthState:
    """Serialize one repository's discovery/refresh while allowing authenticated peers."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    initialized: bool = False
    scope: str = ""
    token: DownloadToken | None = None
    generation: int = 0
    challenge: str = field(default="", repr=False)


class _DownloadAuth:
    """Single-flight provider bound to one immutable origin/credential snapshot."""

    def __init__(
        self, config: TransportConfig, username: str | None, password: str | None,
        retry_status_codes: tuple[int, ...],
    ) -> None:
        """Snapshot credentials and retryable probe statuses without store/network effects."""
        scheme = "http" if config.insecure else "https"
        self.base_url = f"{scheme}://{config.registry}"
        self.origin = _url_origin(self.base_url)
        self.username = username
        self.password = password
        self.timeout = config.timeout
        self.retry_status_codes = tuple(retry_status_codes)
        self.states: dict[str, _AuthState] = {}
        self.lock = threading.Lock()

    def _state(self, request: RegistryRequest) -> _AuthState:
        """Find a bounded repository-pull state; reject non-download request scopes."""
        scope = self._scope(request)
        with self.lock:
            if scope not in self.states:
                if len(self.states) >= 32:
                    raise ValueError("Download authentication scope limit exceeded")
                self.states[scope] = _AuthState(scope=scope)
            return self.states[scope]

    def _refresh(self, state: _AuthState, request: RegistryRequest, session: requests.Session) -> None:
        """Exchange under the scope lock; failures leave later peers free to retry.

        Failed exchanges never cache exception objects or advance the token
        generation. Process-control exceptions propagate only to their caller.
        """
        state.token = negotiate_download_token(
            state.challenge, self.username, self.password,
            base_url=self.base_url, scope=state.scope, session=session,
            timeout=request.timeout or self.timeout,
        )
        state.generation += 1
        state.initialized = True

    @staticmethod
    def _scope(request: RegistryRequest) -> str:
        """Derive scope from the terminal resource, allowing resource names inside repositories."""
        full_path = urlsplit(request.url).path
        if full_path == "/v2/":
            return ""
        if not full_path.startswith("/v2/"):
            raise ValueError("Download forks only support registry blob/manifest reads")
        path = full_path.removeprefix("/v2/")
        parent, _, reference = path.rpartition("/")
        repo, _, resource = parent.rpartition("/")
        if repo and reference and resource in ("manifests", "blobs"):
            return f"repository:{repo}:pull"
        raise ValueError("Download forks only support registry blob/manifest reads")

    @staticmethod
    def _authorized(request: RegistryRequest, token: DownloadToken | None) -> RegistryRequest:
        """Copy request headers, preserving body, timeout, redirect and response hooks."""
        headers = requests.structures.CaseInsensitiveDict(request.headers)
        if token is not None:
            headers["Authorization"] = token.authorization
        return replace(request, headers=dict(headers))

    def _challenge(
        self, state: _AuthState, request: RegistryRequest, challenge: str,
        next_handler: NextHandler, session: requests.Session,
    ) -> RegistryResponse:
        """Negotiate a challenge or return a retryable probe to the shared outer retry owner."""
        if not challenge:
            probe = AuthMiddleware._probe_v2_response(
                next_handler, request, self.base_url, stream=True,
            )
            if probe.status_code != 401 and probe.status_code in self.retry_status_codes:
                return probe
            with ClosingResponse(probe.raw_response):
                challenge = (
                    probe.raw_response.headers.get("WWW-Authenticate", "")
                    if probe.status_code == 401 else ""
                )
        if not challenge:
            raise AuthError("Registry returned no download authentication challenge")
        state.challenge = challenge
        self._refresh(state, request, session)
        retried = next_handler(self._authorized(request, state.token))
        if retried.status_code == 401:
            require_authentication_origin(retried.raw_response, self.base_url)
        return retried

    def _capture_challenge(self, response: RegistryResponse) -> str:
        """Validate and close a rejected response before waiting for the scope lock."""
        require_authentication_origin(response.raw_response, self.base_url)
        with ClosingResponse(response.raw_response):
            return response.raw_response.headers.get("WWW-Authenticate", "")

    def execute(
        self, request: RegistryRequest, next_handler: NextHandler, session: requests.Session,
    ) -> RegistryResponse:
        """Share discovery, expiry and one refresh per rejected token generation.

        Only same-origin GET/HEAD requests can use scoped credentials. Foreign
        successes pass through without provider headers; foreign 401s fail closed.
        Followers do not dispatch while initial discovery or expiry refresh runs.
        Unsuccessful initial responses leave discovery serialized for later peers.
        A refreshed generation is reused for matching or absent challenges;
        explicit realm/service changes require their own validated negotiation.
        """
        if request.method not in ("GET", "HEAD"):
            raise ValueError("Download forks only support GET and HEAD")
        url = self.base_url + request.url if request.url.startswith("/") else request.url
        request = replace(request, url=url)
        if _url_origin(url) != self.origin:
            response = next_handler(request)
            if response.status_code == 401:
                require_authentication_origin(response.raw_response, self.base_url)
            return response
        state = self._state(request)
        with state.lock:
            if not state.initialized:
                response = next_handler(request)
                if response.status_code == 401:
                    return self._challenge(
                        state, request, self._capture_challenge(response), next_handler, session,
                    )
                state.initialized = 200 <= response.status_code < 300
                return response
            if state.token is not None and (
                state.token.expires_at is not None and state.token.expires_at <= time.monotonic()
            ):
                self._refresh(state, request, session)
            token, generation = state.token, state.generation
        response = next_handler(self._authorized(request, token))
        if response.status_code != 401:
            return response
        challenge = self._capture_challenge(response)
        with state.lock:
            same_challenge = not challenge or (
                _parse_auth_header(challenge) == _parse_auth_header(state.challenge)
            )
            if state.generation == generation or not same_challenge:
                return self._challenge(state, request, challenge, next_handler, session)
            retried = next_handler(self._authorized(request, state.token))
            if retried.status_code == 401:
                require_authentication_origin(retried.raw_response, self.base_url)
            return retried


class _ResponseLease:
    """Hold a global slot until a streamed response is explicitly closed."""

    def __init__(self, response: requests.Response, context: "DownloadTransport") -> None:
        """Capture the original close method without reading any response bytes."""
        self.original_close = response.close
        self.context = context
        self.lock = threading.Lock()
        self.closed = False

    def close(self) -> None:
        """Close once and release accounting even when underlying cleanup fails."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.original_close()
            finally:
                self.context._release()


class _DownloadAdapter(requests.adapters.HTTPAdapter):
    """Share thread-safe origin pools and serialize bounded proxy-manager creation."""

    def __init__(self, concurrency: int) -> None:
        """Bound each origin cache/pool to C and allow at least the two environment proxies."""
        self._proxy_lock = threading.Lock()
        self._proxy_limit = max(2, concurrency)
        super().__init__(
            pool_connections=concurrency, pool_maxsize=concurrency,
            pool_block=True, max_retries=0,
        )

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> ProxyManager:
        """Initialize one manager per proxy under a lock; reject unbounded proxy churn."""
        with self._proxy_lock:
            if proxy not in self.proxy_manager and len(self.proxy_manager) >= self._proxy_limit:
                raise ValueError("Download proxy-pool limit exceeded")
            return super().proxy_manager_for(proxy, **proxy_kwargs)


class _DownloadSession(requests.Session):
    """Worker-local Requests state borrowing an adapter it must never close."""

    def __init__(self, context: "DownloadTransport") -> None:
        """Replace and close fresh default adapters; preserve environment/proxy/CA behavior."""
        super().__init__()
        for adapter in self.adapters.values():
            adapter.close()
        self.adapters.clear()
        self.context = context
        self.closed = False
        self._send_depth = 0
        self.mount("http://", context.adapter)
        self.mount("https://", context.adapter)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        """Lease one slot around Requests' prepared request and complete redirect chain.

        Buffered responses release the slot once Requests has consumed the body.
        Recursive redirect sends borrow the outer slot. Sessions are worker-local,
        not concurrently shared. Exceptions retain their exact identity.
        """
        if self.closed:
            raise RuntimeError("Download session is closed")
        if self._send_depth:
            self.context._record_dispatch()
            return super().send(request, **kwargs)
        self.context._acquire()
        self.context._record_dispatch()
        self._send_depth += 1
        started = time.perf_counter()
        try:
            response = super().send(request, **kwargs)
        except BaseException:
            self.context._release()
            observer = request_observer.get()
            if observer is not None:
                observer(0, time.perf_counter() - started)
            raise
        finally:
            self._send_depth -= 1
        if kwargs.get("stream"):
            lease = _ResponseLease(response, self.context)
            response.close = lease.close  # type: ignore[method-assign]  # Requests has no close hook.
        else:
            self.context._release()
        observer = request_observer.get()
        if observer is not None:
            observer(
                0,
                time.perf_counter() - started,
            )
        return response

    def close(self) -> None:
        """Detach borrowed adapters before closing only local session resources."""
        if not self.closed:
            self.closed = True
            self.adapters.clear()
            super().close()


class DownloadTransport:
    """Own a bounded pooled download context; borrow the original RegistryClient.

    :param client: Supported borrowed client or explicit safe-fork extension.
    :param options: Validated pooled DownloadOptions.
    :raises ValueError: For legacy policy or unsupported customizations before pool creation.
    """

    def __init__(self, client: RegistryClient, options: "DownloadOptions") -> None:
        """Snapshot built-ins and validate the effective retry budget before allocation.

        Custom middleware state is reconstructed only by the safe-fork hook,
        never traversed while copying connection/retry settings.
        """
        if options.transport != "pooled" or not client.supports_download_fork():
            raise ValueError("DownloadTransport requires supported pooled download policy")
        options.validate()
        self.options = options
        self.borrowed = client
        self.config = replace(
            client.config, retry_config=copy.deepcopy(client.config.retry_config), middlewares=[],
        )
        self.username, self.password = client._username, client._password
        explicit_retry = self.config.retry_config
        self.logging_config: tuple[str, int] | None = None
        self.cache_config: tuple[int, float | None] | None = None
        if client._pipeline is not None:
            for middleware in client._pipeline._middleware:
                if type(middleware) is RetryMiddleware:
                    if self.config.retry_config is not None or middleware.config != RetryConfig():
                        explicit_retry = middleware.config
                elif type(middleware) is LoggingMiddleware:
                    self.logging_config = (middleware.logger.name, middleware.level)
                elif type(middleware) is CachingMiddleware:
                    self.cache_config = (middleware.max_size, middleware.ttl)
        self.retry_config = copy.deepcopy(explicit_retry) if explicit_retry else RetryConfig(
            status_codes=(429, 500, 502, 503, 504),
        )
        if explicit_retry is not None and not self.config.enable_retries:
            self.retry_config.max_retries = 0
        if options.max_retries is not None:
            self.retry_config.max_retries = options.max_retries
        if type(self.retry_config.max_retries) is not int or not 0 <= self.retry_config.max_retries <= 10:
            raise ValueError("Resolved download max_retries must be an integer in 0..10")
        self.auth = _DownloadAuth(
            self.config, self.username, self.password, self.retry_config.status_codes,
        )
        self.adapter = _DownloadAdapter(options.concurrency)
        self.slots = threading.BoundedSemaphore(options.concurrency)
        self.lock = threading.Lock()
        self.active = 0
        self.active_peak = 0
        self.request_count = 0
        self._coordinator_created = False
        self.closed = False
        self.sessions: list[_DownloadSession] = []

    def _acquire(self) -> None:
        """Reserve a global request/response slot, failing if context closure won the race."""
        self.slots.acquire()
        with self.lock:
            if self.closed:
                self.slots.release()
                raise RuntimeError("Download transport is closed")
            self.active += 1
            self.active_peak = max(self.active_peak, self.active)
        lifecycle = request_lifecycle.get()
        if lifecycle is not None:
            lifecycle(True)

    def _release(self) -> None:
        """Release one acquired global slot after request failure or response disposal."""
        with self.lock:
            self.active -= 1
        self.slots.release()
        lifecycle = request_lifecycle.get()
        if lifecycle is not None:
            lifecycle(False)

    def _record_dispatch(self) -> None:
        """Count each prepared HTTP hop, including root/auth and failed redirect chains."""
        with self.lock:
            self.request_count += 1
        observer = request_observer.get()
        if observer is not None:
            observer(1, 0.0)

    def new_session(self) -> requests.Session:
        """Create worker-local mutable state on the shared pool without credential lookup."""
        with self.lock:
            if self.closed:
                raise RuntimeError("Download transport is closed")
            self.sessions = [session for session in self.sessions if not session.closed]
            if len(self.sessions) >= self.options.concurrency + 1:
                raise ValueError("Download context allows one root and at most C worker sessions")
            session = _DownloadSession(self)
            self.sessions.append(session)
            return session

    def fork(self) -> RegistryClient:
        """Invoke the explicit safe-fork hook and reject unsafe returned identity."""
        worker = self.borrowed.fork_for_download(context=self)
        if (
            not isinstance(worker, RegistryClient) or worker is self.borrowed
            or worker.config is self.borrowed.config
            or worker._download_context is not self
            or worker._session is None
        ):
            raise ValueError("fork_for_download must return an isolated client in this context")
        return worker

    def __enter__(self) -> RegistryClient:
        """Return a fresh root/manifest/blob client; clean up if extension construction fails."""
        try:
            return self.fork()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as secondary:  # noqa: BLE001 - preserve the primary fork failure
                report_secondary_failure(primary, secondary, "Download transport cleanup")
            raise

    def close(self) -> None:
        """Close child-local resources and the pool once, even on lifecycle misuse.

        Callers must still close responses and join workers. A live response
        can release its accounting after closure, but no new requests may start.

        :raises RuntimeError: After cleanup, if callers still own a request/stream.
        """
        with self.lock:
            if self.closed:
                return
            active = self.active
            self.closed = True
        try:
            for session in self.sessions:
                session.close()
        finally:
            self.adapter.close()
        if active:
            raise RuntimeError("Close download responses and join workers before closing the pool")

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Close after acquisition; preserve an active domain/observer failure exactly."""
        try:
            self.close()
        except BaseException as secondary:
            if exc is None:
                raise
            report_secondary_failure(exc, secondary, "Download transport cleanup")
        return False

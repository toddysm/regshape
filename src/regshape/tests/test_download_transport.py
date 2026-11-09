"""Real local HTTP/1.1 coverage of pooled isolation, scoped auth and connection lifetime.

Fixtures use only ephemeral loopback sockets and synthetic credentials. Concurrent
jobs are driven by tests, not by the later production scheduler.
"""

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from regshape.libs.artifacts import pull_artifact
from regshape.libs.auth.download import negotiate_download_token
from regshape.libs.blobs import DownloadOptions, get_blob
from regshape.libs.errors import AuthError
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.download import DownloadTransport
from regshape.libs.transport.middleware import (
    CachingMiddleware,
    RetryConfig,
    RetryMiddleware,
)
from regshape.tests.artifact_fixtures import StreamResponse, address, document, edge


class _ServerState:
    """Thread-safe counters and synthetic registry policies for the loopback fixture."""

    def __init__(self) -> None:
        """Initialize per-scope credentials/tokens and bounded opaque object payloads."""
        self.lock = threading.Lock()
        self.mode = "bearer"
        self.token_calls: list[tuple[str, str]] = []
        self.unauthorized = 0
        self.requests = 0
        self.ports: set[int] = set()
        self.headers: list[str | None] = []
        self.valid_tokens: dict[str, str] = {}
        self.payload = b"download bytes"
        self.root = document(config=edge(self.payload))
        self.failures = 0
        self.pre_auth_failures = 0
        self.failure_status = 429
        self.token_lifetime = 3600
        self.challenge_scope: str | None = None
        self.token_status = 200
        self.token_posts = 0
        self.token_post_status = 200
        self.token_post_data: list[dict[str, list[str]]] = []
        self.retry_after: str | None = None


@pytest.fixture
def server():
    """Run and stop an ephemeral HTTP/1.1 server, yielding its authority and mutable policy."""
    state = _ServerState()

    class Handler(BaseHTTPRequestHandler):
        """Serve deterministic token, root and blob responses with persistent connections."""

        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            """Suppress HTTP-server diagnostics; assertions own all synthetic request evidence."""

        def handle(self) -> None:
            """Accept expected connection resets when a streamed challenge is discarded."""
            try:
                super().handle()
            except ConnectionResetError:
                return

        def _reply(self, status: int, payload: bytes, headers: dict | None = None) -> None:
            """Write a framed response so client connection reuse is measurable."""
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

        def do_GET(self) -> None:
            """Issue scoped tokens or require the negotiated token for registry reads."""
            path = urlsplit(self.path)
            with state.lock:
                state.ports.add(self.client_address[1])
                if path.path == "/token":
                    params = parse_qs(path.query)
                    scope = params.get("scope", [""])[0]
                    credential = self.headers.get("Authorization", "")
                    state.token_calls.append((scope, credential))
                    token = f"token-{len(state.token_calls)}"
                    state.valid_tokens[scope] = token
                    self._reply(state.token_status, json.dumps({
                        "token": token, "expires_in": state.token_lifetime,
                    }).encode())
                    return
                state.requests += 1
                authorization = self.headers.get("Authorization")
                state.headers.append(authorization)
                if path.path == "/v2/":
                    repo, resource = "", ""
                else:
                    repo, resource, _ = path.path.removeprefix("/v2/").rsplit("/", 2)
                scope = f"repository:{repo}:pull" if repo else ""
                if state.pre_auth_failures:
                    state.pre_auth_failures -= 1
                    self._reply(state.failure_status, b"")
                    return
                basic = "Basic " + base64.b64encode(b"test-user:test-password").decode()
                expected = (
                    basic if state.mode == "basic"
                    else "Bearer " + state.valid_tokens.get(scope, "not-issued")
                )
                if state.mode != "anonymous" and authorization != expected:
                    state.unauthorized += 1
                    authority = f"127.0.0.1:{self.server.server_port}"
                    challenged_scope = state.challenge_scope if state.challenge_scope is not None else scope
                    challenge = (
                        'Basic realm="registry"' if state.mode == "basic" else
                        f'Bearer realm="http://{authority}/token",service="registry",scope="{challenged_scope}"'
                    )
                    self._reply(401, b"", {"WWW-Authenticate": challenge})
                    return
                if state.failures:
                    state.failures -= 1
                    headers = {"Retry-After": state.retry_after} if state.retry_after is not None else {}
                    self._reply(state.failure_status, b"", headers)
                    return
                payload = state.root if resource == "manifests" else state.payload
                self._reply(200, payload)

        def do_POST(self) -> None:
            """Consume an ACR-style refresh-token request and return a scoped access token."""
            data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            with state.lock:
                state.token_posts += 1
                state.token_post_data.append(data)
                scope = data.get("scope", [""])[0]
                token = f"refresh-{state.token_posts}"
                state.valid_tokens[scope] = token
                self._reply(state.token_post_status, json.dumps({"access_token": token}).encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"127.0.0.1:{httpd.server_port}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _client(
    authority: str, *, username: str | None = "test-user",
    password: str | None = "test-password", **kwargs: object,
) -> RegistryClient:
    """Resolve a synthetic credential snapshot once without touching Docker configuration."""
    with patch(
        "regshape.libs.transport.client.resolve_credentials",
        return_value=(username, password),
    ):
        return RegistryClient(TransportConfig(authority, insecure=True, **kwargs))


def _receive(worker: RegistryClient, repo: str = "repo") -> bytes:
    """Consume and close one response to release its connection and global slot."""
    with worker.get(f"/v2/{repo}/blobs/{address(b'download bytes')}", stream=True) as response:
        assert response.status_code == 200
        return response.content


@pytest.mark.parametrize("middleware", [True, False])
def test_single_flight_and_connection_reuse(server, middleware: bool) -> None:
    """Root, manifest and concurrent blob clients share one same-scope negotiation."""
    authority, state = server
    client = _client(authority, enable_middleware=middleware)
    original_last = client.last_response
    context = DownloadTransport(client, DownloadOptions())
    with context as root:
        with root.get("/v2/repo/manifests/latest", stream=True) as response:
            assert response.content == state.root
        workers = [context.fork() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert list(executor.map(_receive, workers)) == [state.payload] * 4
        for _ in range(4):
            assert _receive(root) == state.payload
        assert len(state.token_calls) == 1 and state.unauthorized == 1
        pool = context.adapter.poolmanager.connection_from_url(root.base_url)
        assert pool.num_connections <= 4
        assert len(state.ports) < state.requests
        assert context.active == 0
    assert context.closed and all(session.closed for session in context.sessions)
    assert client.last_response is original_last


def test_concurrent_unknown_scope_single_flight(server) -> None:
    """Only one peer sends the initial unauthenticated request for an unknown repository."""
    authority, state = server
    with DownloadTransport(_client(authority), DownloadOptions()) as root:
        context = root._download_context
        workers = [context.fork() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert list(executor.map(_receive, workers)) == [state.payload] * 4
    assert state.unauthorized == 1 and len(state.token_calls) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_unsuccessful_first_response_keeps_concurrent_discovery_single_flight(server, status: int) -> None:
    """Waiting peers share one discovery while the first throttled request backs off."""
    authority, state = server
    state.pre_auth_failures = 1
    state.failure_status = status
    backoff_entered = threading.Event()
    release_backoff = threading.Event()
    peers_ready = threading.Barrier(4)

    def pause_backoff(attempt: int) -> None:
        """Hold the original request outside its released slot until peers finish discovery."""
        backoff_entered.set()
        assert release_backoff.wait(timeout=5)

    def receive_peer(worker: RegistryClient) -> bytes:
        """Dispatch four peers together against the still-undiscovered authentication scope."""
        peers_ready.wait(timeout=5)
        return _receive(worker)

    with (
        patch.object(RetryMiddleware, "_wait_backoff", side_effect=pause_backoff),
        DownloadTransport(_client(authority), DownloadOptions(max_retries=1)) as root,
    ):
        context = root._download_context
        workers = [context.fork() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=5) as executor:
            throttled = executor.submit(_receive, root)
            try:
                assert backoff_entered.wait(timeout=5)
                assert not context.auth.states["repository:repo:pull"].initialized
                assert context.active == 0
                peers = [executor.submit(receive_peer, worker) for worker in workers]
                assert [peer.result(timeout=5) for peer in peers] == [state.payload] * 4
            finally:
                release_backoff.set()
            assert throttled.result(timeout=5) == state.payload
    assert len(state.token_calls) == 1 and state.unauthorized == 1


@pytest.mark.parametrize("rejected", [False, True])
def test_expiry_and_rejected_generation_refresh_once(server, rejected: bool) -> None:
    """Expired or rejected shared generations cause one exchange rather than one per worker."""
    authority, state = server
    with DownloadTransport(_client(authority), DownloadOptions()) as root:
        assert _receive(root) == state.payload
        context = root._download_context
        auth = context.auth.states["repository:repo:pull"]
        if rejected:
            state.valid_tokens.clear()
        else:
            auth.token = replace(auth.token, expires_at=0)
        workers = [context.fork() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert list(executor.map(_receive, workers)) == [state.payload] * 4
    assert len(state.token_calls) == 2


def test_expiry_refresh_and_inflight_rejection_release_slot_before_scope_lock(server) -> None:
    """An expired peer holding the scope lock cannot deadlock an in-flight 401 at C=1."""
    authority, state = server
    with DownloadTransport(_client(authority), DownloadOptions.sequential()) as root:
        assert _receive(root) == state.payload
        context = root._download_context
        peer = context.fork()
        auth = context.auth.states["repository:repo:pull"]
        state.valid_tokens.clear()
        rejected_ready = threading.Event()
        refresh_entered = threading.Event()
        rejected = []
        send = root._session.send
        refresh = context.auth._refresh

        def hold_rejection(request, **kwargs):
            """Hold the response lease until the peer has entered refresh under its lock."""
            response = send(request, **kwargs)
            if response.status_code == 401:
                rejected.append(response)
                rejected_ready.set()
                assert refresh_entered.wait(timeout=5)
            return response

        def notify_refresh(*args, **kwargs):
            """Expose scope-lock ownership before the token exchange waits for the C=1 slot."""
            refresh_entered.set()
            return refresh(*args, **kwargs)

        with (
            patch.object(root._session, "send", side_effect=hold_rejection),
            patch.object(context.auth, "_refresh", side_effect=notify_refresh),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            inflight = executor.submit(_receive, root)
            try:
                assert rejected_ready.wait(timeout=5)
                auth.token = replace(auth.token, expires_at=0)
                expired = executor.submit(_receive, peer)
                assert inflight.result(timeout=5) == state.payload
                assert expired.result(timeout=5) == state.payload
            finally:
                # Release the blocked peer even if a regression times out.
                for response in rejected:
                    response.close()
        assert context.active == 0 and auth.generation == 2
    assert len(state.token_calls) == 2


def test_scope_and_credential_isolation(server) -> None:
    """Different repository scopes and operation credential identities never share cached tokens."""
    authority, state = server
    with DownloadTransport(_client(authority), DownloadOptions()) as worker:
        _receive(worker, "first")
        _receive(worker, "second")
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=("other", "other-password"),
    ):
        client = RegistryClient(TransportConfig(authority, insecure=True))
    with DownloadTransport(client, DownloadOptions()) as worker:
        _receive(worker, "first")
    assert [scope for scope, _ in state.token_calls] == [
        "repository:first:pull", "repository:second:pull", "repository:first:pull",
    ]
    assert state.token_calls[0][1] != state.token_calls[2][1]


@pytest.mark.parametrize("repo", ["team/manifests/image", "team/blobs/image", "team/manifests/blobs/image"])
@pytest.mark.parametrize("resource", ["manifests", "blobs"])
def test_nested_repository_scope_uses_terminal_resource(server, repo: str, resource: str) -> None:
    """Repository components named manifests/blobs remain in the validated authentication scope."""
    authority, state = server
    with DownloadTransport(_client(authority), DownloadOptions.sequential()) as worker:
        reference = "latest" if resource == "manifests" else address(state.payload)
        with worker.get(f"/v2/{repo}/{resource}/{reference}", stream=True) as response:
            assert response.status_code == 200
            assert response.content == (state.root if resource == "manifests" else state.payload)
        result = get_blob(worker, repo, address(state.payload))
        assert result.digest == address(state.payload)
    assert [scope for scope, _ in state.token_calls] == [f"repository:{repo}:pull"]
    assert state.unauthorized == 1


@pytest.mark.parametrize("mode", ["anonymous", "basic"])
@pytest.mark.parametrize("middleware", [True, False])
def test_anonymous_and_basic(server, mode: str, middleware: bool) -> None:
    """Built-in modes retain origin-bound anonymous/Basic flows without Bearer token calls."""
    authority, state = server
    state.mode = mode
    with DownloadTransport(_client(authority, enable_middleware=middleware), DownloadOptions()) as worker:
        _receive(worker)
        _receive(worker)
    assert state.token_calls == []
    assert state.unauthorized == int(mode == "basic")


def test_worker_isolation_and_single_pool_close(server) -> None:
    """Credentials resolve once; child close cannot close a peer's pool or mutate the caller."""
    authority, _state = server
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=(None, None),
    ) as resolve:
        client = RegistryClient(TransportConfig(authority, insecure=True, enable_caching=True))
        context = DownloadTransport(client, DownloadOptions())
        with patch.object(context.adapter, "close", wraps=context.adapter.close) as close:
            with context as first:
                second = context.fork()
                assert first.config is not second.config and first.config is not client.config
                assert first._pipeline is not second._pipeline
                assert first._session is not second._session
                first._session.headers["X-Test"] = "first"
                first._session.cookies.set("example", "first")
                assert "X-Test" not in second._session.headers
                assert second._session.cookies.get("example") is None
                caches = [
                    next(item for item in worker._pipeline._middleware if isinstance(item, CachingMiddleware))
                    for worker in (first, second)
                ]
                assert caches[0].cache is not caches[1].cache
                first._session.close()
                assert close.call_count == 0
                _receive(second)
                _receive(second)
                assert caches[1].cache == {}
                assert client.last_response is None
            assert close.call_count == 1
            context.close()
            assert close.call_count == 1
        resolve.assert_called_once()


def test_owned_stream_holds_slot_and_closes_pool_on_misuse(server) -> None:
    """Premature cleanup closes the pool once, rejects new work and retains response accounting."""
    authority, _state = server
    context = DownloadTransport(_client(authority), DownloadOptions.sequential())
    worker = context.fork()
    response = worker.get("/v2/repo/blobs/test", stream=True)
    assert context.active == 1
    try:
        with patch.object(context.adapter, "close", wraps=context.adapter.close) as close:
            with pytest.raises(RuntimeError, match="Close download responses"):
                context.close()
            assert context.closed and close.call_count == 1
            with pytest.raises(RuntimeError, match="closed"):
                context.new_session()
            response.close()
            response.close()
            assert context.active == 0
            context.close()
            close.assert_called_once()
    finally:
        response.close()
        context.close()


def test_pool_closes_with_live_response_without_masking_primary_error(
    server, caplog: pytest.LogCaptureFixture,
) -> None:
    """Context exit reports lifecycle misuse but retains a primary failure and closes its pool."""
    authority, _state = server
    context = DownloadTransport(_client(authority), DownloadOptions.sequential())
    primary = ValueError("synthetic primary failure")
    response = None
    try:
        with patch.object(context.adapter, "close", wraps=context.adapter.close) as close:
            with pytest.raises(ValueError) as caught, context as worker:
                response = worker.get("/v2/repo/blobs/test", stream=True)
                raise primary
            assert caught.value is primary
            close.assert_called_once()
        assert context.closed
        assert "Download transport cleanup" in caplog.text
    finally:
        if response is not None:
            response.close()
        context.close()
    assert context.active == 0


@pytest.mark.parametrize("budget", [None, 0, 2])
def test_modern_retry_default_and_explicit_zero(server, budget: int | None) -> None:
    """Pooled defaults recover 429; explicit zero and nonzero overrides control actual attempts."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 2
    with (
        patch.object(RetryMiddleware, "_wait_backoff"),
        DownloadTransport(_client(authority), DownloadOptions(max_retries=budget)) as worker,
    ):
        with worker.get("/v2/repo/blobs/test", stream=True) as response:
            assert response.status_code == (429 if budget == 0 else 200)
        assert worker._download_context.retry_config.max_retries == (3 if budget is None else budget)
    assert state.requests == (1 if budget == 0 else 3)


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_retry_budget_shared_across_authentication_resends(server, budget: int) -> None:
    """Mixed 429/401/429 responses cannot restart the retry budget after token negotiation."""
    authority, state = server
    state.pre_auth_failures = 1
    state.failures = 1
    with (
        patch.object(RetryMiddleware, "_wait_backoff") as wait,
        DownloadTransport(_client(authority), DownloadOptions(max_retries=budget)) as worker,
    ):
        with worker.get("/v2/repo/blobs/test", stream=True) as response:
            assert response.status_code == (200 if budget == 2 else 429)
        assert wait.call_count == budget
    assert state.requests == {0: 1, 1: 3, 2: 4}[budget]
    assert len(state.token_calls) == (0 if budget == 0 else 1)


def test_explicit_client_retry_policy_retained(server) -> None:
    """An enabled explicit client policy takes precedence over the pooled default, not an override."""
    authority, state = server
    state.mode = "anonymous"
    policy = RetryConfig(max_retries=1, backoff_factor=0, status_codes=(429,))
    client = _client(authority, enable_retries=True, retry_config=policy)
    with DownloadTransport(client, DownloadOptions()) as worker:
        assert worker._download_context.retry_config == policy
        assert worker._download_context.retry_config is not policy
    with DownloadTransport(client, DownloadOptions(max_retries=0)) as worker:
        assert worker._download_context.retry_config.max_retries == 0
    assert policy.max_retries == 1


def test_implicit_enabled_retry_policy_uses_modern_defaults(server) -> None:
    """Enabling retries without an explicit policy still includes the modern 429 budget."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 1
    with (
        patch.object(RetryMiddleware, "_wait_backoff"),
        DownloadTransport(_client(authority, enable_retries=True), DownloadOptions()) as worker,
    ):
        _receive(worker)
    assert state.requests == 2


@pytest.mark.parametrize("budget", [None, 0, 2])
def test_disabled_explicit_client_retry_policy_preserves_zero_unless_overridden(server, budget) -> None:
    """An explicitly disabled policy keeps statuses/backoff and performs only authorized retries."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 2
    policy = RetryConfig(max_retries=5, backoff_factor=0, status_codes=(429,), exceptions=())
    client = _client(authority, enable_retries=False, retry_config=policy)
    with (
        patch.object(RetryMiddleware, "_wait_backoff"),
        DownloadTransport(client, DownloadOptions(max_retries=budget)) as worker,
    ):
        resolved = worker._download_context.retry_config
        assert resolved.max_retries == (0 if budget is None else budget)
        assert resolved.status_codes == (429,) and resolved.backoff_factor == 0
        assert resolved.exceptions == ()
        with worker.get("/v2/repo/blobs/test", stream=True) as response:
            assert response.status_code == (200 if budget == 2 else 429)
    assert state.requests == (3 if budget == 2 else 1)
    assert policy.max_retries == 5


@pytest.mark.parametrize("header,expected", [
    ("5", 5.0),
    ("300", 300.0),
    ("000000000000000000000005", 5.0),
    ("0", 1.0),
    ("Fri, 06 Nov 2026 16:00:05 GMT", 5.0),
    ("Fri, 06 Nov 2026 15:59:00 GMT", 1.0),
    ("not-a-date", 1.0),
    ("", 1.0),
])
@pytest.mark.parametrize("status", [429, 503])
def test_pooled_retry_after_actual_wait(
    server, header: str, expected: float, status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    """Pooled throttling waits honor seconds/dates, release slots and diagnose malformed values."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 1
    state.failure_status = status
    state.retry_after = header

    def assert_released(delay: float) -> None:
        """Prove the discarded response releases its global slot before backoff starts."""
        assert worker._download_context.active == 0

    with (
        patch(
            "regshape.libs.transport.middleware.time.time",
            return_value=datetime(2026, 11, 6, 16, tzinfo=timezone.utc).timestamp(),
        ),
        patch("regshape.libs.transport.middleware.time.sleep", side_effect=assert_released) as sleep,
        DownloadTransport(_client(authority), DownloadOptions()) as worker,
    ):
        _receive(worker)
        assert worker._download_context.active == 0
    sleep.assert_called_once_with(expected)
    assert ("Malformed Retry-After" in caplog.text) == (header in ("not-a-date", ""))


def test_legacy_retry_after_timing_is_unchanged(server) -> None:
    """The original borrowed-client path retains configured backoff despite a Retry-After header."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 1
    state.retry_after = "5"
    policy = RetryConfig(max_retries=1, status_codes=(429,), backoff_factor=0)
    with patch("regshape.libs.transport.middleware.time.sleep") as sleep:
        get_blob(
            _client(authority, enable_retries=True, retry_config=policy),
            "repo", address(state.payload), download_options=DownloadOptions.legacy(),
        )
    sleep.assert_called_once_with(0)


@pytest.mark.parametrize("header", [
    "301", "31536000", "Fri, 01 Jan 2100 00:00:00 GMT", "9" * 20,
])
@pytest.mark.parametrize("status", [429, 503])
def test_excessive_retry_after_fails_after_release(server, header: str, status: int) -> None:
    """Server delays above the 300-second ceiling fail without sleeping, retrying or leaking slots."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 1
    state.retry_after = header
    state.failure_status = status
    with (
        patch("regshape.libs.transport.middleware.time.sleep") as sleep,
        DownloadTransport(_client(authority), DownloadOptions()) as worker,
    ):
        with pytest.raises(ValueError, match="supported download wait"):
            _receive(worker)
        assert worker._download_context.active == 0
    assert state.requests == 1
    sleep.assert_not_called()


def test_worker_session_bound_and_closed_record_reclamation(server) -> None:
    """One root plus C peers bounds mutable session records; closed peers can be replaced."""
    authority, _state = server
    with DownloadTransport(_client(authority), DownloadOptions.sequential()) as root:
        context = root._download_context
        context.fork()
        with pytest.raises(ValueError, match="at most C"):
            context.fork()
        root._session.close()
        replacement = context.fork()
        assert replacement._session is not root._session
        assert len(context.sessions) == 2


def test_proxy_pool_creation_is_single_flight_and_bounded(server) -> None:
    """Concurrent first use shares one proxy manager and unexpected proxy churn fails explicitly."""
    authority, _state = server
    context = DownloadTransport(_client(authority), DownloadOptions.sequential())
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            managers = list(executor.map(
                context.adapter.proxy_manager_for, ["http://127.0.0.1:1234"] * 4,
            ))
        assert all(manager is managers[0] for manager in managers)
        context.adapter.proxy_manager_for("http://127.0.0.1:1235")
        with pytest.raises(ValueError, match="proxy-pool"):
            context.adapter.proxy_manager_for("http://127.0.0.1:1236")
        assert len(context.adapter.proxy_manager) == 2
    finally:
        context.close()


@pytest.mark.parametrize("changes", [
    {"expires_in": True}, {"expires_in": -1}, {"expires_in": 10**100},
    {"expires_in": "3600"}, {"expires_in": 1, "issued_at": 42},
    {"expires_in": 1, "issued_at": "2026-11-07T00:00:00"},
    {"token": "bad token"}, {"token": "\x7f"}, {"token": 42},
])
def test_invalid_token_metadata_rejects_and_closes(changes: dict) -> None:
    """Malformed expiry/token metadata retains AuthError and releases the bounded response."""
    import requests

    response = StreamResponse(json.dumps({"token": "synthetic", **changes}).encode())
    with (
        requests.Session() as session,
        patch("regshape.libs.auth.download.http_request", return_value=response) as send,
        pytest.raises(AuthError),
    ):
        negotiate_download_token(
            'Bearer realm="https://tokens.example/token",service="registry"',
            None, None, base_url="https://registry.example",
            scope="repository:repo:pull", session=session, timeout=12,
        )
    assert response.close_calls == 1
    assert send.call_args.kwargs["session"] is session
    assert send.call_args.kwargs["timeout"] == 12
    assert send.call_args.kwargs["allow_redirects"] is False


def test_token_payload_limit_and_expiry_metadata() -> None:
    """Token JSON is bounded to 1 MiB and valid issued_at metadata expires in monotonic time."""
    import requests

    with requests.Session() as session:
        for payload in (
            {"token": "x" * 1_048_576},
            {"token": "synthetic", "expires_in": 3600, "issued_at": "2026-11-07T00:00:00Z"},
        ):
            response = StreamResponse(json.dumps(payload).encode())
            with patch("regshape.libs.auth.download.http_request", return_value=response):
                if len(payload["token"]) > 1_000_000:
                    with pytest.raises(AuthError, match="byte limit"):
                        negotiate_download_token(
                            'Bearer realm="https://tokens.example/token",service="registry"',
                            None, None, base_url="https://registry.example", scope="",
                            session=session, timeout=12,
                        )
                else:
                    token = negotiate_download_token(
                        'Bearer realm="https://tokens.example/token",service="registry"',
                        None, None, base_url="https://registry.example", scope="",
                        session=session, timeout=12,
                    )
                    assert token.expires_at is not None
            assert response.close_calls == 1


def test_scope_mismatch_rejected_before_exchange(server) -> None:
    """A registry challenge cannot expand repository/action credentials into another scope."""
    authority, state = server
    state.challenge_scope = "repository:other:pull,push"
    with DownloadTransport(_client(authority), DownloadOptions()) as worker:
        with pytest.raises(AuthError, match="scope"):
            _receive(worker)
        assert worker._download_context.active == 0
    assert state.token_calls == []


def test_refresh_token_fallback(server) -> None:
    """ACR sentinel credentials enable a scoped, identified POST after releasing the GET."""
    authority, state = server
    state.token_status = 401
    client = _client(authority, username="00000000-0000-0000-0000-000000000000")
    with DownloadTransport(client, DownloadOptions()) as worker:
        assert _receive(worker) == state.payload
    assert len(state.token_calls) == 1 and state.token_posts == 1
    assert state.token_post_data == [{
        "grant_type": ["refresh_token"], "refresh_token": ["test-password"],
        "client_id": ["regshape"], "service": ["registry"],
        "scope": ["repository:repo:pull"],
    }]


@pytest.mark.parametrize("username,password", [
    ("test-user", "wrong-password"), (None, None),
    ("00000000-0000-0000-0000-000000000000", ""),
    ("00000000-0000-0000-0000-000000000000", None),
])
def test_non_acr_credentials_do_not_trigger_refresh_post(
    server, username: str | None, password: str | None,
) -> None:
    """Ordinary or empty credentials retain the GET 401 without attempting a refresh grant."""
    authority, state = server
    state.token_status = 401
    client = _client(authority, username=username, password=password)
    with DownloadTransport(client, DownloadOptions()) as worker:
        with pytest.raises(AuthError, match="Status code: 401"):
            _receive(worker)
        assert worker._download_context.active == 0
    assert len(state.token_calls) == 1 and state.token_posts == 0


@pytest.mark.parametrize("status", [401, 403, 503])
def test_failed_refresh_post_retains_original_rejection(
    server, status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected refresh grant closes both exchanges and reports the original GET 401."""
    authority, state = server
    state.token_status = 401
    state.token_post_status = status
    client = _client(authority, username="00000000-0000-0000-0000-000000000000")
    with DownloadTransport(client, DownloadOptions()) as worker:
        with pytest.raises(AuthError, match="Status code: 401"):
            _receive(worker)
        assert worker._download_context.active == 0
    assert state.token_posts == 1
    assert "retaining original token rejection" in caplog.text


def test_refresh_post_transport_failure_retains_original_rejection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Refresh transport failures cannot obscure the closed GET 401 or expose exception details."""
    rejected = StreamResponse(b"", status=401)
    with (
        requests.Session() as session,
        patch(
            "regshape.libs.auth.download.http_request",
            side_effect=[rejected, requests.exceptions.Timeout("synthetic private endpoint")],
        ) as send,
        pytest.raises(AuthError, match="Status code: 401"),
    ):
        negotiate_download_token(
            'Bearer realm="https://tokens.example/token",service="registry"',
            "00000000-0000-0000-0000-000000000000", "synthetic-refresh",
            base_url="https://registry.example", scope="repository:repo:pull",
            session=session, timeout=12,
        )
    assert rejected.close_calls == 1 and send.call_count == 2
    assert "retaining original token rejection" in caplog.text
    assert "synthetic private endpoint" not in caplog.text


@pytest.mark.parametrize("phase", ["discovery", "expiry", "rejection"])
@pytest.mark.parametrize("error_type", [AuthError, KeyboardInterrupt, SystemExit])
def test_failed_exchange_does_not_poison_later_peer(
    server, phase: str, error_type: type[BaseException],
) -> None:
    """Failed discovery/refresh never replays an exception into a later worker or advances state."""
    authority, state = server
    with DownloadTransport(_client(authority), DownloadOptions()) as root:
        context = root._download_context
        if phase != "discovery":
            _receive(root)
            auth = context.auth.states["repository:repo:pull"]
            if phase == "expiry":
                auth.token = replace(auth.token, expires_at=0)
            else:
                state.valid_tokens.clear()
        failure = error_type("synthetic exchange failure")
        failed = False

        def fail_once(*args, **kwargs):
            """Raise only for the first caller, then perform a real loopback token exchange."""
            nonlocal failed
            if not failed:
                failed = True
                raise failure
            return negotiate_download_token(*args, **kwargs)

        with patch("regshape.libs.transport.download.negotiate_download_token", side_effect=fail_once):
            with pytest.raises(error_type) as caught:
                _receive(root)
            assert caught.value is failure
            auth = context.auth.states["repository:repo:pull"]
            assert auth.generation == (0 if phase == "discovery" else 1)
            assert context.active == 0
            assert _receive(context.fork()) == state.payload
            assert auth.generation == (1 if phase == "discovery" else 2)
    assert len(state.token_calls) == (1 if phase == "discovery" else 2)


@pytest.mark.parametrize("fraction", ["", ".8", ".858", ".8583", ".858301", ".858301624"])
@pytest.mark.parametrize("zone", ["Z", "+00:00", "+02:00"])
def test_fractional_issuance_time_on_supported_python_versions(fraction: str, zone: str) -> None:
    """RFC 3339 fractions, including Docker Hub nanoseconds, retain expiry on Python 3.10+."""
    issued = f"2026-11-09T18:16:37{fraction}{zone}"
    whole_seconds = datetime.fromisoformat(
        "2026-11-09T18:16:37" + zone.replace("Z", "+00:00"),
    ).timestamp()
    fractional_seconds = float("0" + fraction) if fraction else 0
    response = StreamResponse(json.dumps({
        "token": "synthetic", "expires_in": 3600, "issued_at": issued,
    }).encode())
    with (
        requests.Session() as session,
        patch("regshape.libs.auth.download.http_request", return_value=response),
        patch("regshape.libs.auth.download.time.time", return_value=whole_seconds + 10),
        patch("regshape.libs.auth.download.time.monotonic", return_value=1000),
    ):
        token = negotiate_download_token(
            'Bearer realm="https://tokens.example/token",service="registry"',
            None, None, base_url="https://registry.example", scope="repository:repo:pull",
            session=session, timeout=12,
        )
    assert token.expires_at == pytest.approx(4590 + fractional_seconds, abs=0.000002)
    assert response.close_calls == 1


def test_waiting_peers_recover_after_single_flight_exchange_failure(server) -> None:
    """A failed leader releases waiting workers to share one successful replacement exchange."""
    authority, state = server
    entered = threading.Event()
    release = threading.Event()
    attempts = 0

    def exchange(*args, **kwargs):
        """Hold and fail only the initial leader while peers wait on the repository lock."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(timeout=5)
            raise AuthError("synthetic transient exchange failure")
        return negotiate_download_token(*args, **kwargs)

    with (
        DownloadTransport(_client(authority), DownloadOptions()) as root,
        patch("regshape.libs.transport.download.negotiate_download_token", side_effect=exchange),
        ThreadPoolExecutor(max_workers=4) as executor,
    ):
        context = root._download_context
        workers = [context.fork() for _ in range(3)]
        leader = executor.submit(_receive, root)
        try:
            assert entered.wait(timeout=5)
            peers = [executor.submit(_receive, worker) for worker in workers]
        finally:
            release.set()
        with pytest.raises(AuthError, match="synthetic transient"):
            leader.result(timeout=5)
        assert [peer.result(timeout=5) for peer in peers] == [state.payload] * 3
        assert attempts == 2 and context.active == 0
    assert len(state.token_calls) == 1


@pytest.mark.parametrize("service", [None, "", "registry"])
@pytest.mark.parametrize("scope", [None, "repository:repo:pull", "repository:repo:pull,push", "repository:repo:push,pull"])
def test_optional_service_and_covering_scope_request_only_pull(
    service: str | None, scope: str | None,
) -> None:
    """Broader same-repository challenges never broaden requested or cached token permissions."""
    challenge = 'Bearer realm="https://tokens.example/token"'
    if service is not None:
        challenge += f',service="{service}"'
    if scope is not None:
        challenge += f',scope="{scope}"'
    response = StreamResponse(b'{"token":"synthetic"}')
    with (
        requests.Session() as session,
        patch("regshape.libs.auth.download.http_request", return_value=response) as send,
    ):
        token = negotiate_download_token(
            challenge, None, None, base_url="https://registry.example",
            scope="repository:repo:pull", session=session, timeout=12,
        )
    expected = {"scope": "repository:repo:pull"}
    if service:
        expected["service"] = service
    assert send.call_args.kwargs["params"] == expected
    assert token.scope == "repository:repo:pull" and token.service == (service or "")
    assert response.close_calls == 1


@pytest.mark.parametrize("scope", [
    "repository:other:pull,push", "repository:repo:push",
    "registry:repo:pull", "repository:repo:*", "",
    "repository:repo:pull repository:other:pull",
])
def test_non_covering_scope_rejected_before_token_request(scope: str) -> None:
    """Cross-repository, multi-scope and non-pull challenges fail before credential exchange."""
    with (
        requests.Session() as session,
        patch("regshape.libs.auth.download.http_request") as send,
        pytest.raises(AuthError, match="scope"),
    ):
        negotiate_download_token(
            f'Bearer realm="https://tokens.example/token",scope="{scope}"',
            None, None, base_url="https://registry.example", scope="repository:repo:pull",
            session=session, timeout=12,
        )
    send.assert_not_called()


def test_artifact_root_and_blobs_share_context(server, tmp_path: Path) -> None:
    """Policy plumbing establishes the pooled context before root discovery, without changing results."""
    authority, state = server
    client = _client(authority)
    result = pull_artifact(client, "repo", "latest", tmp_path / "layout")
    assert result.root.digest == address(state.root)
    assert result.blobs == 1
    assert len(state.token_calls) == 1 and state.unauthorized == 1
    assert client.last_response is None
    assert (tmp_path / "layout" / "blobs" / "sha256" / address(state.payload).split(":")[1]).read_bytes() == state.payload


@pytest.mark.parametrize("explicit", [False, True])
def test_custom_worker_nested_downloads_inherit_context_without_reforking(
    server, tmp_path: Path, caplog: pytest.LogCaptureFixture, explicit: bool,
) -> None:
    """Nested blob/artifact calls accept custom worker state and keep one borrowed context."""
    class Custom(RegistryClient):
        """Safe extension whose worker-only state intentionally fails base support detection."""

        def fork_for_download(self, *, context: DownloadTransport) -> RegistryClient:
            """Add isolated custom state after the built-in worker has been constructed."""
            worker = super().fork_for_download(context=context)
            worker.custom_state = []
            return worker

    authority, state = server
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=("test-user", "test-password"),
    ):
        client = Custom(TransportConfig(authority, insecure=True))
    policy = DownloadOptions(concurrency=2, max_retries=0)
    with DownloadTransport(client, policy) as worker:
        context = worker._download_context
        assert not worker.supports_download_fork()
        with patch("regshape.libs.transport.download._DownloadAdapter", side_effect=AssertionError("nested pool")):
            result = get_blob(
                worker, "repo", address(state.payload), download_options=policy if explicit else None,
            )
            assert result.digest == address(state.payload)
            artifact = pull_artifact(
                worker, "repo", "latest", tmp_path / "layout", download_options=policy if explicit else None,
            )
            assert artifact.root.digest == address(state.root)
            for conflicting in (DownloadOptions(), DownloadOptions.legacy()):
                with pytest.raises(ValueError, match="Nested downloads"):
                    get_blob(worker, "repo", address(state.payload), download_options=conflicting)
                with pytest.raises(ValueError, match="Nested downloads"):
                    pull_artifact(worker, "repo", "latest", tmp_path / "rejected", download_options=conflicting)
        assert len(context.sessions) == 1 and not context.closed
        assert context.options == policy
        assert not (tmp_path / "rejected").exists()
    assert len(state.token_calls) == 1 and state.unauthorized == 1
    assert "original serial transport" not in caplog.text


@pytest.mark.parametrize("options", [None, DownloadOptions.sequential(), DownloadOptions.legacy()])
def test_singular_output_and_progress_preserved(server, options: DownloadOptions | None) -> None:
    """Default, pooled sequential and genuine legacy preserve verification and caller-thread callbacks."""
    authority, state = server
    state.mode = "anonymous"
    client = _client(authority)
    caller = threading.get_ident()
    threads = []

    def observe(event: object) -> None:
        """Record the callback thread without modifying event or transport state."""
        threads.append(threading.get_ident())

    result = get_blob(client, "repo", address(state.payload), progress_callback=observe, download_options=options)
    assert result.digest == address(state.payload)
    assert threads and set(threads) == {caller}
    assert state.requests == 1
    assert (client.last_response is not None) == (options is not None and options.transport == "legacy")

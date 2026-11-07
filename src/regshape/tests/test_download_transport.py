"""Real local HTTP/1.1 coverage of pooled isolation, scoped auth and connection lifetime.

Fixtures use only ephemeral loopback sockets and synthetic credentials. Concurrent
jobs are driven by tests, not by the later production scheduler.
"""

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

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
        self.token_lifetime = 3600
        self.challenge_scope: str | None = None
        self.token_status = 200
        self.token_posts = 0


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
                repo = path.path.removeprefix("/v2/").split("/blobs/")[0].split("/manifests/")[0]
                scope = f"repository:{repo}:pull"
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
                    self._reply(429, b"")
                    return
                payload = state.root if "/manifests/" in path.path else state.payload
                self._reply(200, payload)

        def do_POST(self) -> None:
            """Consume an ACR-style refresh-token request and return a scoped access token."""
            data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            with state.lock:
                state.token_posts += 1
                scope = data.get("scope", [""])[0]
                token = f"refresh-{state.token_posts}"
                state.valid_tokens[scope] = token
                self._reply(200, json.dumps({"access_token": token}).encode())

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


def _client(authority: str, **kwargs: object) -> RegistryClient:
    """Resolve a synthetic credential snapshot once without touching Docker configuration."""
    with patch(
        "regshape.libs.transport.client.resolve_credentials",
        return_value=("test-user", "test-password"),
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


def test_owned_stream_holds_slot_and_blocks_pool_close(server) -> None:
    """Pool cleanup rejects live streams and response.close releases accounting exactly once."""
    authority, _state = server
    context = DownloadTransport(_client(authority), DownloadOptions.sequential())
    worker = context.fork()
    response = worker.get("/v2/repo/blobs/test", stream=True)
    assert context.active == 1
    with pytest.raises(RuntimeError, match="Close download responses"):
        context.close()
    response.close()
    response.close()
    assert context.active == 0
    context.close()


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
    """ACR-style GET rejection closes the old response before instrumented POST exchange."""
    authority, state = server
    state.token_status = 401
    with DownloadTransport(_client(authority), DownloadOptions()) as worker:
        assert _receive(worker) == state.payload
    assert len(state.token_calls) == 1 and state.token_posts == 1


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

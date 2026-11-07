"""Network-free credential-boundary regressions using real Requests redirect machinery.

An in-memory adapter receives PreparedRequests, including synthetic Basic auth
at token endpoints. No credential lookup, socket, proxy or netrc access is used.
The tests distinguish the challenge issuer from a registry-approved token realm.
"""

import base64
import hashlib
import io
from collections.abc import Callable
from functools import partial
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from urllib3.response import HTTPResponse

from regshape.libs.blobs import DownloadOptions, get_blob
from regshape.libs.errors import AuthError
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.urls import require_authentication_origin

BASE = "https://registry.example"
CDN = "https://cdn.example"
TOKEN_ORIGIN = "https://tokens.example"
ATTACKER = "https://attacker.example"
CREDENTIALS = ("synthetic-user", "synthetic-password")
PAYLOAD = b"verified blob from the content delivery network"
DIGEST = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()
BLOB = "/v2/repo/blobs/" + DIGEST


def challenge(realm: str) -> str:
    """Return a synthetic Bearer challenge without conflating issuer and token-service origin."""
    return f'Bearer realm="{realm}/token",service="registry.example"'


class WireResponse(requests.Response):
    """Real response/raw-stream pair with visible closure and prepared-request origin."""

    def __init__(
        self, request: requests.PreparedRequest, status: int, body: bytes = b"",
        headers: dict | None = None,
    ) -> None:
        """Associate exact response origin with the request sent by Requests' adapter."""
        super().__init__()
        self.request = request
        self.url = request.url
        self.status_code = status
        self.headers.update(headers or {})
        self.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False)
        self.close_calls = 0

    def close(self) -> None:
        """Count release attempts without bypassing the actual Requests/urllib3 close behavior."""
        self.close_calls += 1
        super().close()


class WireAdapter(BaseAdapter):
    """Route prepared requests locally while retaining Sessions, hooks, redirects and auth headers."""

    def __init__(self) -> None:
        """Keep route callbacks and request/response evidence, with no network resources."""
        self.routes: dict[str, Callable[[requests.PreparedRequest], WireResponse]] = {}
        self.sent: list[requests.PreparedRequest] = []
        self.responses: list[WireResponse] = []

    def send(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Dispatch a prepared request to a registered synthetic endpoint; unknown endpoints fail."""
        self.sent.append(request)
        parts = urlsplit(request.url)
        key = f"{parts.scheme}://{parts.netloc}{parts.path}"
        assert key in self.routes, f"Unexpected synthetic endpoint: {key}"
        response = self.routes[key](request)
        self.responses.append(response)
        return response

    def close(self) -> None:
        """Release no sockets: the adapter owns only reusable in-memory route/evidence records."""

    def reply(
        self, url: str, status: int, body: bytes = b"", headers: dict | None = None,
    ) -> None:
        """Install a fixed response route, leaving actual redirect handling to Requests."""
        self.routes[url] = lambda request: WireResponse(request, status, body, headers)


@pytest.fixture(params=["pooled", "legacy"])
def wire(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> WireAdapter:
    """Exercise both backends on the local adapter, with proxies/netrc and sockets disabled."""
    adapter = WireAdapter()
    original = requests.Session.__init__

    def isolated(session: requests.Session) -> None:
        """Keep real session behavior but prohibit proxies, netrc and real network adapters."""
        original(session)
        session.trust_env = False
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    monkeypatch.setattr(requests.Session, "__init__", isolated)
    monkeypatch.setattr("regshape.libs.transport.download._DownloadAdapter", lambda concurrency: adapter)
    options = DownloadOptions() if request.param == "pooled" else DownloadOptions.legacy()
    monkeypatch.setattr(__name__ + ".get_blob", partial(get_blob, download_options=options))
    return adapter


def client(middleware: bool) -> RegistryClient:
    """Use explicit synthetic credentials, bypassing credential-store discovery."""
    return RegistryClient(TransportConfig(
        registry="registry.example", username=CREDENTIALS[0], password=CREDENTIALS[1],
        enable_middleware=middleware,
    ))


def download(middleware: bool, path: Path, bounded: bool = True):
    """Exercise the actual blob receive/authentication path, not a mocked authenticate function."""
    return get_blob(
        client(middleware), "repo", DIGEST, str(path),
        **({"max_bytes": len(PAYLOAD), "total_size": len(PAYLOAD)} if bounded else {}),
    )


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("auth", [challenge(ATTACKER), 'Basic realm="foreign"', None])
def test_redirected_401_never_negotiates_registry_credentials(
    tmp_path: Path, wire: WireAdapter, middleware: bool, bounded: bool, auth: str | None,
) -> None:
    """Foreign 401s fail before token exchange or no-challenge /v2/ fallback, and release owned output."""
    wire.reply(BASE + BLOB, 307, headers={"Location": CDN + "/payload"})
    wire.reply(CDN + "/payload", 401, headers={"WWW-Authenticate": auth} if auth else {})
    wire.reply(ATTACKER + "/token", 200, b'{"token":"synthetic-token"}')
    wire.reply(BASE + "/v2/", 401, headers={"WWW-Authenticate": challenge(TOKEN_ORIGIN)})
    with pytest.raises(AuthError, match="response origin") as caught:
        download(middleware, tmp_path / "blob", bounded)
    assert [request.url for request in wire.sent] == [BASE + BLOB, CDN + "/payload"]
    assert all("Authorization" not in request.headers for request in wire.sent)
    assert wire.responses[-1].close_calls == 1
    assert not list(tmp_path.iterdir())
    assert all(value not in str(caught.value) for value in (*CREDENTIALS, CDN))


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("auth", [challenge(ATTACKER), None])
def test_redirected_probe_401_cannot_select_a_foreign_realm(
    tmp_path: Path, wire: WireAdapter, middleware: bool, bounded: bool, auth: str | None,
) -> None:
    """Both the legacy plain probe and hook-preserving probes validate their actual final issuer."""
    wire.reply(BASE + BLOB, 401)
    wire.reply(BASE + "/v2/", 302, headers={"Location": CDN + "/challenge"})
    wire.reply(CDN + "/challenge", 401, headers={"WWW-Authenticate": auth} if auth else {})
    wire.reply(ATTACKER + "/token", 200, b'{"token":"synthetic-token"}')
    with pytest.raises(AuthError, match="response origin"):
        download(middleware, tmp_path / "blob", bounded)
    assert len(wire.sent) == 3
    assert wire.responses[0].close_calls == 1
    assert wire.responses[-1].close_calls == 1
    assert not any(request.url.startswith(ATTACKER) for request in wire.sent)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("retry_unauthorized", [False, True])
def test_registry_issued_external_realm_and_signed_cdn_success(
    tmp_path: Path, wire: WireAdapter, middleware: bool, probe: bool, retry_unauthorized: bool,
) -> None:
    """A trusted issuer may delegate tokens externally; signed-CDN success remains credential-free."""
    def registry(request: requests.PreparedRequest) -> WireResponse:
        """Challenge unauthenticated registry access and redirect the negotiated retry."""
        if "Authorization" not in request.headers:
            return WireResponse(
                request, 401,
                headers={} if probe else {"WWW-Authenticate": challenge(TOKEN_ORIGIN)},
            )
        return WireResponse(request, 307, headers={"Location": CDN + "/payload"})

    wire.routes[BASE + BLOB] = registry
    wire.reply(BASE + "/v2/", 302, headers={"Location": BASE + "/challenge"})
    wire.reply(BASE + "/challenge", 401, headers={"WWW-Authenticate": challenge(TOKEN_ORIGIN)})
    wire.reply(TOKEN_ORIGIN + "/token", 200, b'{"token":"synthetic-token"}')
    wire.reply(
        CDN + "/payload", 401 if retry_unauthorized else 200,
        b"" if retry_unauthorized else PAYLOAD,
        {"WWW-Authenticate": challenge(ATTACKER)} if retry_unauthorized else {},
    )
    wire.reply(ATTACKER + "/token", 200, b'{"token":"attacker-token"}')
    if retry_unauthorized:
        with pytest.raises(AuthError, match="response origin"):
            download(middleware, tmp_path / "blob")
        assert not list(tmp_path.iterdir())
    else:
        result = download(middleware, tmp_path / "blob")
        assert result.digest == DIGEST
        assert (tmp_path / "blob").read_bytes() == PAYLOAD
    token_requests = [request for request in wire.sent if request.url.startswith(TOKEN_ORIGIN)]
    assert len(token_requests) == 1
    expected = "Basic " + base64.b64encode(":".join(CREDENTIALS).encode()).decode()
    assert token_requests[0].headers["Authorization"] == expected
    assert all("Authorization" not in request.headers for request in wire.sent if request.url.startswith(CDN))
    assert not any(request.url.startswith(ATTACKER) for request in wire.sent)
    assert wire.responses[0].close_calls == 1
    assert wire.responses[-1].close_calls == 1


@pytest.mark.parametrize("middleware", [False, True])
def test_anonymous_signed_cdn_success_still_works(
    tmp_path: Path, wire: WireAdapter, middleware: bool,
) -> None:
    """An anonymously readable redirected blob does not require origin equality for a successful body."""
    wire.reply(BASE + BLOB, 307, headers={"Location": CDN + "/payload"})
    wire.reply(CDN + "/payload", 200, PAYLOAD)
    assert download(middleware, tmp_path / "blob").digest == DIGEST
    assert len(wire.sent) == 2
    assert all("Authorization" not in request.headers for request in wire.sent)


@pytest.mark.parametrize("middleware", [False, True])
def test_adapter_control_reproduces_disclosure_when_guard_is_removed(
    tmp_path: Path, wire: WireAdapter, middleware: bool,
) -> None:
    """Negative control: only disabling the guard reproduces synthetic credential disclosure plus digest success."""
    wire.reply(BASE + BLOB, 307, headers={"Location": CDN + "/payload"})

    def foreign(request: requests.PreparedRequest) -> WireResponse:
        """Challenge once, then serve valid digest-matching bytes after the unsafe exchange."""
        prior = sum(item.url == CDN + "/payload" for item in wire.sent)
        return WireResponse(
            request, 401 if prior == 1 else 200,
            b"" if prior == 1 else PAYLOAD,
            {"WWW-Authenticate": challenge(ATTACKER)} if prior == 1 else {},
        )

    wire.routes[CDN + "/payload"] = foreign
    wire.reply(ATTACKER + "/token", 200, b'{"token":"synthetic-token"}')
    with patch("regshape.libs.transport.client.require_authentication_origin"), patch(
        "regshape.libs.transport.middleware.require_authentication_origin",
    ), patch(
        "regshape.libs.transport.download.require_authentication_origin",
    ):
        assert download(middleware, tmp_path / "blob").digest == DIGEST
    exposed = [item for item in wire.sent if item.url.startswith(ATTACKER)]
    assert len(exposed) == 1 and exposed[0].headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize("actual", [
    None, "", "/v2/", "https://cdn.example/v2/", "http://registry.example/v2/",
    "https://registry.example:444/v2/", "https://user@registry.example/v2/",
    "https://registry.example:bad/v2/", " https://registry.example/v2/",
    "https://registry.example\\other/v2/",
])
def test_auth_origin_rejects_missing_malformed_or_changed_origin(actual: str | None) -> None:
    """Unknown response identity is never replaced with the original requested URL."""
    prepared = requests.Request("GET", BASE + "/v2/").prepare()
    response = WireResponse(prepared, 401)
    response.url = actual
    with pytest.raises(AuthError, match="response origin"):
        require_authentication_origin(response, BASE)
    assert response.close_calls == 1


def test_auth_origin_normalizes_host_case_and_default_port() -> None:
    """Equivalent HTTPS origins pass without closing a response still owned by the caller."""
    response = WireResponse(requests.Request("GET", BASE + "/v2/").prepare(), 401)
    response.url = "https://REGISTRY.example:443/v2/"
    require_authentication_origin(response, BASE)
    assert response.close_calls == 0
    response.close()


@pytest.mark.parametrize("middleware", [False, True])
def test_rejected_response_close_error_does_not_mask_origin_error(
    tmp_path: Path, wire: WireAdapter, middleware: bool,
) -> None:
    """A failing close preserves the rejection and prevents all credential negotiation."""
    wire.reply(BASE + BLOB, 307, headers={"Location": CDN + "/payload"})
    original_close = WireResponse.close

    def foreign(request: requests.PreparedRequest) -> WireResponse:
        """Return a cross-origin challenge whose owned close fails after releasing its stream."""
        response = WireResponse(request, 401, headers={"WWW-Authenticate": challenge(ATTACKER)})

        def fail_close() -> None:
            """Release the body and raise a secondary cleanup fault."""
            original_close(response)
            raise OSError("synthetic close failure")

        response.close = fail_close
        return response

    wire.routes[CDN + "/payload"] = foreign
    with pytest.raises(AuthError, match="response origin"):
        download(middleware, tmp_path / "blob")
    assert len(wire.sent) == 2 and not list(tmp_path.iterdir())

#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.transport.client`."""

import pytest
import requests
from unittest.mock import MagicMock, patch

from regshape.libs.errors import AuthError
from regshape.libs.transport.client import (
    RegistryClient,
    TransportConfig,
    _normalize_www_authenticate,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
PATH = "/v2/myrepo/myimage/manifests/latest"
BEARER_WWW_AUTH = 'Bearer realm="https://auth.example.io/token",service="acr.example.io"'
BASIC_WWW_AUTH = 'Basic realm="registry"'
TOKEN = "mytoken"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(
    status_code: int,
    body: str = "{}",
    www_auth: str = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    headers = {}
    if www_auth:
        headers["WWW-Authenticate"] = www_auth
    resp.headers = headers
    return resp


def _client(insecure: bool = False, username: str = None, password: str = None):
    """Build a RegistryClient with mocked credential resolution."""
    config = TransportConfig(
        registry=REGISTRY,
        insecure=insecure,
        username=username,
        password=password,
        enable_middleware=False,  # Disable middleware for legacy tests
    )
    with patch(
        "regshape.libs.transport.client.resolve_credentials",
        return_value=(username, password),
    ):
        return RegistryClient(config)


# ===========================================================================
# TestTransportConfig
# ===========================================================================

class TestTransportConfig:

    def test_minimal_config(self):
        c = TransportConfig(registry="acr.io")
        assert c.registry == "acr.io"
        assert c.insecure is False
        assert c.username is None
        assert c.password is None
        assert c.timeout == 30

    def test_full_config(self):
        c = TransportConfig(
            registry="localhost:5000",
            insecure=True,
            username="alice",
            password="secret",
            timeout=60,
        )
        assert c.insecure is True
        assert c.timeout == 60

    def test_empty_registry_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            TransportConfig(registry="")

    def test_registry_with_scheme_raises(self):
        with pytest.raises(ValueError, match="hostname, not a URL"):
            TransportConfig(registry="https://acr.io")


# ===========================================================================
# TestRegistryClientConstruction
# ===========================================================================

class TestRegistryClientConstruction:

    def test_base_url_https_by_default(self):
        c = _client()
        assert c.base_url == f"https://{REGISTRY}"

    def test_base_url_http_when_insecure(self):
        c = _client(insecure=True)
        assert c.base_url == f"http://{REGISTRY}"

    def test_credentials_resolved_from_store_when_not_provided(self):
        config = TransportConfig(registry=REGISTRY, enable_middleware=False)
        with patch(
            "regshape.libs.transport.client.resolve_credentials",
            return_value=("alice", "secret"),
        ) as mock_resolve:
            client = RegistryClient(config)
        mock_resolve.assert_called_once_with(REGISTRY, None, None)
        assert client._username == "alice"
        assert client._password == "secret"

    def test_explicit_credentials_bypass_store(self):
        config = TransportConfig(registry=REGISTRY, username="bob", password="pass", enable_middleware=False)
        with patch(
            "regshape.libs.transport.client.resolve_credentials",
            return_value=("bob", "pass"),
        ) as mock_resolve:
            client = RegistryClient(config)
        mock_resolve.assert_called_once_with(REGISTRY, "bob", "pass")
        assert client._username == "bob"


# ===========================================================================
# TestRegistryClientRequest — successful requests (no auth)
# ===========================================================================

class TestRegistryClientRequestNoAuth:

    def test_get_returns_response(self):
        ok = _make_response(200, '{"schemaVersion": 2}')
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            resp = _client().get(PATH)
        assert resp.status_code == 200
        mock.assert_called_once()

    def test_head_uses_head_method(self):
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().head(PATH)
        assert mock.call_args[0][1] == "HEAD"

    def test_put_uses_put_method(self):
        ok = _make_response(201)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().put(PATH, data=b'{}')
        assert mock.call_args[0][1] == "PUT"

    def test_delete_uses_delete_method(self):
        ok = _make_response(202)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().delete(PATH)
        assert mock.call_args[0][1] == "DELETE"

    def test_post_uses_post_method(self):
        ok = _make_response(202)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().post(PATH)
        assert mock.call_args[0][1] == "POST"

    def test_patch_uses_patch_method(self):
        ok = _make_response(204)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().patch(PATH)
        assert mock.call_args[0][1] == "PATCH"

    def test_full_url_built_from_base_url_and_path(self):
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().get(PATH)
        url_arg = mock.call_args[0][0]
        assert url_arg == f"https://{REGISTRY}{PATH}"

    def test_insecure_uses_http_scheme_in_url(self):
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client(insecure=True).get(PATH)
        url_arg = mock.call_args[0][0]
        assert url_arg.startswith("http://")

    def test_extra_kwargs_forwarded(self):
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().get(PATH, params={"n": 10})
        assert mock.call_args[1]["params"] == {"n": 10}

    def test_custom_headers_forwarded(self):
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().get(PATH, headers={"Accept": "application/json"})
        hdrs = mock.call_args[1].get("headers") or mock.call_args[0][2]
        assert hdrs.get("Accept") == "application/json"

    def test_default_timeout_used(self):
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            _client().get(PATH)
        assert mock.call_args[1]["timeout"] == 30

    def test_custom_timeout_in_config(self):
        ok = _make_response(200)
        config = TransportConfig(registry=REGISTRY, timeout=60, enable_middleware=False)
        with patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)):
            client = RegistryClient(config)
        with patch("regshape.libs.transport.client.http_request", return_value=ok) as mock:
            client.get(PATH)
        assert mock.call_args[1]["timeout"] == 60

    def test_caller_headers_not_mutated(self):
        """The client must not modify the dict passed as headers by the caller."""
        ok = _make_response(200)
        caller_headers = {"Accept": "application/json"}
        with patch("regshape.libs.transport.client.http_request", return_value=ok):
            _client().get(PATH, headers=caller_headers)
        assert "Authorization" not in caller_headers

    def test_non_401_errors_returned_without_retry(self):
        """5xx responses are returned as-is without a retry attempt."""
        err = _make_response(500)
        with patch("regshape.libs.transport.client.http_request", return_value=err) as mock:
            resp = _client().get(PATH)
        assert resp.status_code == 500
        assert mock.call_count == 1


# ===========================================================================
# TestRegistryClientRequest — Bearer auth challenge
# ===========================================================================

class TestRegistryClientRequestBearer:

    def test_bearer_challenge_triggers_token_exchange_and_retry(self):
        challenge = _make_response(401, www_auth=BEARER_WWW_AUTH)
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", side_effect=[challenge, ok]), \
             patch("regshape.libs.transport.client.registryauth.authenticate", return_value=TOKEN):
            resp = _client(username="alice", password="secret").get(PATH)
        assert resp.status_code == 200

    def test_authorization_header_set_on_retry(self):
        challenge = _make_response(401, www_auth=BEARER_WWW_AUTH)
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", side_effect=[challenge, ok]) as mock, \
             patch("regshape.libs.transport.client.registryauth.authenticate", return_value=TOKEN):
            _client(username="alice", password="secret").get(PATH)
        retry_headers = mock.call_args_list[1][1].get("headers") or mock.call_args_list[1][0][2]
        assert retry_headers.get("Authorization") == f"Bearer {TOKEN}"

    def test_bearer_www_auth_passed_to_registryauth(self):
        challenge = _make_response(401, www_auth=BEARER_WWW_AUTH)
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", side_effect=[challenge, ok]), \
             patch("regshape.libs.transport.client.registryauth.authenticate", return_value=TOKEN) as mock_auth:
            _client(username="alice", password="secret").get(PATH)
        # First arg to authenticate is the normalised WWW-Authenticate value
        assert "Bearer" in mock_auth.call_args[0][0]

    def test_two_http_calls_made_on_bearer_challenge(self):
        challenge = _make_response(401, www_auth=BEARER_WWW_AUTH)
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", side_effect=[challenge, ok]) as mock, \
             patch("regshape.libs.transport.client.registryauth.authenticate", return_value=TOKEN):
            _client(username="alice", password="secret").get(PATH)
        assert mock.call_count == 2


# ===========================================================================
# TestRegistryClientRequest — Basic auth challenge
# ===========================================================================

class TestRegistryClientRequestBasic:

    def test_basic_challenge_with_credentials_retries(self):
        challenge = _make_response(401, www_auth=BASIC_WWW_AUTH)
        ok = _make_response(200)
        with patch("regshape.libs.transport.client.http_request", side_effect=[challenge, ok]), \
             patch("regshape.libs.transport.client.registryauth.authenticate", return_value="dXNlcjpwYXNz"):
            resp = _client(username="alice", password="secret").get(PATH)
        assert resp.status_code == 200

    def test_basic_challenge_no_username_raises(self):
        challenge = _make_response(401, www_auth=BASIC_WWW_AUTH)
        with patch("regshape.libs.transport.client.http_request", return_value=challenge):
            with pytest.raises(AuthError, match="Basic authentication"):
                _client(username=None, password=None).get(PATH)

    def test_basic_challenge_no_password_raises(self):
        challenge = _make_response(401, www_auth=BASIC_WWW_AUTH)
        with patch("regshape.libs.transport.client.http_request", return_value=challenge):
            with pytest.raises(AuthError, match="Basic authentication"):
                _client(username="alice", password=None).get(PATH)


# ===========================================================================
# TestRegistryClientRequest — 401 without WWW-Authenticate
# ===========================================================================

class TestRegistryClientRequest401NoChallenge:

    def test_401_without_www_authenticate_raises_auth_error(self):
        challenge = _make_response(401)
        with patch("regshape.libs.transport.client.http_request", return_value=challenge):
            with pytest.raises(AuthError, match="without a WWW-Authenticate header"):
                _client().get(PATH)

    def test_401_without_www_authenticate_only_one_call_made(self):
        challenge = _make_response(401)
        with patch("regshape.libs.transport.client.http_request", return_value=challenge) as mock:
            with pytest.raises(AuthError):
                _client().get(PATH)
        assert mock.call_count == 1


# ===========================================================================
# TestNormalizeWwwAuthenticate
# ===========================================================================

class TestNormalizeWwwAuthenticate:

    def test_bearer_capitalised(self):
        normalised, scheme = _normalize_www_authenticate(
            'bearer realm="https://auth.io/token",service="acr.io"'
        )
        assert scheme == "Bearer"
        assert normalised.startswith("Bearer ")

    def test_basic_capitalised(self):
        _, scheme = _normalize_www_authenticate('basic realm="registry"')
        assert scheme == "Basic"

    def test_unknown_scheme_preserved(self):
        _, scheme = _normalize_www_authenticate('Digest realm="registry"')
        assert scheme == "Digest"

    def test_spaces_stripped_from_params(self):
        normalised, _ = _normalize_www_authenticate(
            'Bearer realm="https://auth.io", service="acr.io", scope="pull"'
        )
        assert ", " not in normalised
        assert "," in normalised

    def test_no_params_scheme_only(self):
        normalised, scheme = _normalize_www_authenticate("Bearer")
        assert normalised == "Bearer"
        assert scheme == "Bearer"

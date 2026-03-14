#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.ping.operations`."""

import pytest
import requests
from unittest.mock import MagicMock, patch

from regshape.libs.errors import AuthError, PingError
from regshape.libs.ping.operations import PingResult, ping
from regshape.libs.transport import RegistryClient, TransportConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTRY = "ghcr.io"


def _mock_client() -> MagicMock:
    client = MagicMock(spec=RegistryClient)
    config = MagicMock(spec=TransportConfig)
    config.registry = REGISTRY
    client.config = config
    return client


def _make_response(
    status_code: int,
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


# ===========================================================================
# PingResult
# ===========================================================================


class TestPingResult:

    def test_to_dict(self):
        result = PingResult(
            reachable=True,
            status_code=200,
            api_version="registry/2.0",
            latency_ms=42.3,
        )
        d = result.to_dict()
        assert d["reachable"] is True
        assert d["status_code"] == 200
        assert d["api_version"] == "registry/2.0"
        assert d["latency_ms"] == 42.3

    def test_to_dict_no_api_version(self):
        result = PingResult(
            reachable=True,
            status_code=200,
            api_version=None,
            latency_ms=10.0,
        )
        assert result.to_dict()["api_version"] is None


# ===========================================================================
# ping()
# ===========================================================================


class TestPing:

    def test_success_with_api_version_header(self):
        client = _mock_client()
        client.get.return_value = _make_response(
            200,
            headers={"Docker-Distribution-API-Version": "registry/2.0"},
        )
        result = ping(client)

        client.get.assert_called_once_with("/v2/")
        assert result.reachable is True
        assert result.status_code == 200
        assert result.api_version == "registry/2.0"
        assert result.latency_ms >= 0

    def test_success_without_api_version_header(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, headers={})

        result = ping(client)
        assert result.reachable is True
        assert result.api_version is None

    def test_non_200_returns_not_reachable(self):
        client = _mock_client()
        client.get.return_value = _make_response(503)

        result = ping(client)
        assert result.reachable is False
        assert result.status_code == 503

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(401)

        with pytest.raises(AuthError, match="requires authentication"):
            ping(client)

    def test_connection_error_raises_ping_error(self):
        client = _mock_client()
        client.get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        with pytest.raises(PingError, match="not reachable"):
            ping(client)

    def test_timeout_raises_ping_error(self):
        client = _mock_client()
        client.get.side_effect = requests.exceptions.Timeout("timed out")

        with pytest.raises(PingError, match="not reachable"):
            ping(client)

    def test_generic_request_exception_raises_ping_error(self):
        client = _mock_client()
        client.get.side_effect = requests.exceptions.RequestException("something went wrong")

        with pytest.raises(PingError, match="not reachable"):
            ping(client)

    def test_auth_error_from_client_propagated(self):
        """AuthError raised by the client (e.g. middleware) propagates directly."""
        client = _mock_client()
        client.get.side_effect = AuthError("Auth failed", "HTTP 401")

        with pytest.raises(AuthError, match="Auth failed"):
            ping(client)

    def test_latency_is_measured(self):
        client = _mock_client()
        client.get.return_value = _make_response(200)

        result = ping(client)
        # Latency should be a non-negative number
        assert isinstance(result.latency_ms, float)
        assert result.latency_ms >= 0

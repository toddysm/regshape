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

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_response_as_result(self, status_code):
        """Opt-in auth results retain known response metadata and diagnostics."""
        client = _mock_client()
        client.get.return_value = _make_response(
            status_code, {"Docker-Distribution-API-Version": "registry/2.0"},
        )
        with patch("regshape.libs.ping.operations.time.monotonic",
                   side_effect=[10.0, 10.125]):
            result = ping(client, raise_on_auth=False)

        client.get.assert_called_once_with("/v2/")
        assert result.to_dict() == {
            "reachable": True,
            "status_code": status_code,
            "api_version": "registry/2.0",
            "latency_ms": 125.0,
            "auth_required": True,
            "error": f"Registry {REGISTRY} requires authentication : HTTP {status_code}",
        }

    def test_auth_exception_as_result_does_not_guess_status(self):
        """Token errors preserve their text without treating token status as registry status."""
        client = _mock_client()
        error = AuthError("Token failed", "403")
        client.get.side_effect = error

        result = ping(client, raise_on_auth=False)

        assert result.to_dict() == {
            "reachable": True,
            "status_code": None,
            "api_version": None,
            "latency_ms": None,
            "auth_required": True,
            "error": str(error),
        }

    def test_default_preserves_raw_403_behavior(self):
        """Existing callers still receive a non-reachable result for a raw 403."""
        client = _mock_client()
        client.get.return_value = _make_response(403)

        result = ping(client)

        assert result.status_code == 403
        assert result.reachable is False
        assert result.auth_required is False
        assert result.error is None

    @pytest.mark.parametrize("status_code", [200, 404, 503])
    def test_result_mode_preserves_non_auth_outcomes(self, status_code):
        """Opting in to auth results does not reinterpret other HTTP responses."""
        client = _mock_client()
        client.get.return_value = _make_response(status_code)

        result = ping(client, raise_on_auth=False)

        assert result.reachable is (status_code == 200)
        assert result.status_code == status_code
        assert result.auth_required is False
        assert result.error is None

    @pytest.mark.parametrize("raise_on_auth", [True, False])
    @pytest.mark.parametrize("error_type", [
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.RequestException,
    ])
    def test_transport_error_detail_in_both_modes(self, error_type, raise_on_auth):
        """Both modes raise PingError with the original transport cause and detail."""
        client = _mock_client()
        error = error_type("transport diagnostic")
        client.get.side_effect = error

        with pytest.raises(PingError) as raised:
            ping(client, raise_on_auth=raise_on_auth)

        assert raised.value.__cause__ is error
        assert raised.value.detail == "transport diagnostic"


class TestPingErrorDetail:
    """SDK diagnostics match legacy CLI selection for chained and direct errors."""

    def test_direct_error_retains_full_message(self):
        """An unchained PingError preserves its existing formatted text."""
        error = PingError("Connection refused", "details")
        assert error.detail == str(error)

    def test_empty_cause_is_not_replaced_by_outer_message(self):
        """Even an empty explicit cause takes precedence over the domain message."""
        error = PingError("Connection refused", "details")
        error.__cause__ = requests.exceptions.ConnectionError()
        assert error.detail == ""

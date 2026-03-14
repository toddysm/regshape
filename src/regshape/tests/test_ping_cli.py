#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.ping`."""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, PingError
from regshape.libs.ping.operations import PingResult


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY = "ghcr.io"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ping_result(
    reachable: bool = True,
    status_code: int = 200,
    api_version: str | None = "registry/2.0",
    latency_ms: float = 42.3,
) -> PingResult:
    return PingResult(
        reachable=reachable,
        status_code=status_code,
        api_version=api_version,
        latency_ms=latency_ms,
    )


def _runner():
    return CliRunner()


# ===========================================================================
# TestPingCommand
# ===========================================================================


class TestPingCommand:

    def test_success_plain_text(self):
        with patch("regshape.cli.ping.ping_registry", return_value=_ping_result()):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY])
        assert result.exit_code == 0
        assert "is reachable" in result.output
        assert "registry/2.0" in result.output
        assert "42ms" in result.output

    def test_success_no_api_version(self):
        with patch("regshape.cli.ping.ping_registry",
                   return_value=_ping_result(api_version=None)):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY])
        assert result.exit_code == 0
        assert "is reachable" in result.output
        assert "API Version" not in result.output

    def test_success_json(self):
        with patch("regshape.cli.ping.ping_registry", return_value=_ping_result()):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY, "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["registry"] == REGISTRY
        assert parsed["reachable"] is True
        assert parsed["status_code"] == 200
        assert parsed["api_version"] == "registry/2.0"
        assert parsed["latency_ms"] == 42.3

    def test_not_reachable_exits_1(self):
        with patch("regshape.cli.ping.ping_registry",
                   return_value=_ping_result(reachable=False, status_code=503)):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY])
        assert result.exit_code == 1

    def test_auth_error_exits_0_reachable(self):
        """Auth failure means registry is reachable but requires credentials."""
        with patch("regshape.cli.ping.ping_registry",
                   side_effect=AuthError("Authentication failed", "HTTP 401")):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY])
        assert result.exit_code == 0
        assert "is reachable" in result.output
        assert "requires authentication" in result.output

    def test_ping_error_exits_1(self):
        with patch("regshape.cli.ping.ping_registry",
                   side_effect=PingError("Connection refused", "details")):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY])
        assert result.exit_code == 1

    def test_request_exception_exits_1(self):
        with patch("regshape.cli.ping.ping_registry",
                   side_effect=requests.exceptions.ConnectionError("connection error")):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY])
        assert result.exit_code == 1

    def test_registry_option_required(self):
        result = _runner().invoke(regshape, ["ping"])
        assert result.exit_code == 2
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_insecure_flag_propagated(self):
        with patch("regshape.cli.ping.ping_registry", return_value=_ping_result()), \
             patch("regshape.cli.ping.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, ["--insecure", "ping", "-r", REGISTRY])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is True

    def test_error_json_format(self):
        with patch("regshape.cli.ping.ping_registry",
                   side_effect=PingError("Connection refused", "details")):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY, "--json"])
        assert result.exit_code == 1

    def test_auth_error_json_shows_reachable(self):
        with patch("regshape.cli.ping.ping_registry",
                   side_effect=AuthError("Token failed", "403")):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY, "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["reachable"] is True
        assert parsed["registry"] == REGISTRY
        assert "authentication" in parsed["note"].lower()

    def test_not_reachable_json_exits_1(self):
        with patch("regshape.cli.ping.ping_registry",
                   return_value=_ping_result(reachable=False, status_code=503)):
            result = _runner().invoke(regshape, ["ping", "-r", REGISTRY, "--json"])
        assert result.exit_code == 1

#!/usr/bin/env python3

"""
:mod: `test_auth_cli` - Test suite for CLI auth commands and credential helpers
================================================================================

    module:: test_auth_cli
    :platform: Unix, Windows
    :synopsis: Tests for ``regshape.libs.auth.credentials`` and the
               ``regshape auth login`` / ``regshape auth logout`` CLI commands.
    moduleauthor:: ToddySM <toddysm@gmail.com>

Test categories:
  - TestResolveCredentials  — unit tests for the credential resolution chain
  - TestStoreCredentials    — unit tests for store_credentials()
  - TestEraseCredentials    — unit tests for erase_credentials()
  - TestAuthLoginCommand    — CLI integration tests for ``auth login``
  - TestAuthLogoutCommand   — CLI integration tests for ``auth logout``
"""

import base64
import contextlib
import json
import os
import tempfile
from urllib.parse import urlparse

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import MagicMock, patch, call

from regshape.cli.main import regshape
from regshape.libs.auth.credentials import (
    _get_auth_from_config,
    _get_cred_helper,
    erase_credentials,
    resolve_credentials,
    store_credentials,
)
from regshape.libs.errors import AuthError


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

REGISTRY = "registry.example.com"


def _b64(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def _auth_config(registry: str, username: str, password: str) -> dict:
    """Return a minimal Docker config dict with an auths entry."""
    return {"auths": {registry: {"auth": _b64(username, password)}}}


def _cred_helper_config(registry: str, helper: str) -> dict:
    """Return a minimal Docker config dict with a credHelpers entry."""
    return {"credHelpers": {registry: helper}}


def _make_response(status_code: int, www_auth: str = None, text: str = "{}") -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    headers = {}
    if www_auth:
        headers["WWW-Authenticate"] = www_auth
    resp.headers = headers
    return resp


# ===========================================================================
# TestResolveCredentials
# ===========================================================================

class TestResolveCredentials:

    def test_explicit_credentials_take_priority(self):
        """Explicit username/password are returned immediately."""
        u, p = resolve_credentials(REGISTRY, username="alice", password="secret")
        assert u == "alice"
        assert p == "secret"

    def test_explicit_credentials_skip_config_lookup(self):
        """When explicit credentials are provided, no disk I/O occurs."""
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config") as mock_load:
            resolve_credentials(REGISTRY, username="alice", password="secret")
            mock_load.assert_not_called()

    def test_cred_helper_used_when_configured(self):
        config = _cred_helper_config(REGISTRY, "desktop")
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config), \
             patch("regshape.libs.auth.credentials.dockercredstore.get",
                   return_value={"Username": "bob", "Secret": "tok"}) as mock_get:
            u, p = resolve_credentials(REGISTRY)
            mock_get.assert_called_once_with(store="desktop", registry=REGISTRY)
            assert u == "bob"
            assert p == "tok"

    def test_cred_helper_failure_falls_through_to_auths(self):
        """If the cred helper raises AuthError, fall through to docker config auths."""
        config = {
            "credHelpers": {REGISTRY: "desktop"},
            "auths": {REGISTRY: {"auth": _b64("charlie", "pass")}},
        }
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config), \
             patch("regshape.libs.auth.credentials.dockercredstore.get",
                   side_effect=AuthError("helper error", "not found")):
            u, p = resolve_credentials(REGISTRY)
            assert u == "charlie"
            assert p == "pass"

    def test_auths_section_decoded(self):
        """Credentials are decoded from the auths Base64 entry."""
        config = _auth_config(REGISTRY, "dave", "pw123")
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config), \
             patch("regshape.libs.auth.credentials.dockercredstore.get",
                   side_effect=AuthError("no helper", "")):
            u, p = resolve_credentials(REGISTRY)
            assert u == "dave"
            assert p == "pw123"

    def test_anonymous_when_no_credentials_found(self):
        """Returns (None, None) when no credentials are found."""
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None):
            u, p = resolve_credentials(REGISTRY)
            assert u is None
            assert p is None

    def test_partial_explicit_credentials_not_short_circuited(self):
        """If only username is given, resolution continues (not treated as explicit)."""
        config = _auth_config(REGISTRY, "eve", "stored")
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config):
            # Only username provided → explicit condition (both) not met → falls
            # through to config lookup and returns stored credentials
            u, p = resolve_credentials(REGISTRY, username="eve")
            assert u == "eve"
            assert p == "stored"

    def test_docker_config_path_forwarded(self):
        """The docker_config_path kwarg is forwarded to dockerconfig.load_config."""
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None) as mock_load:
            resolve_credentials(REGISTRY, docker_config_path="/tmp/config.json")
            mock_load.assert_called_with("/tmp/config.json")


# ===========================================================================
# TestGetAuthFromConfig
# ===========================================================================

class TestGetAuthFromConfig:

    def test_returns_none_for_unknown_registry(self):
        config = _auth_config("other.registry.io", "u", "p")
        assert _get_auth_from_config(REGISTRY, config) is None

    def test_decodes_base64_auth(self):
        config = _auth_config(REGISTRY, "user", "pass")
        result = _get_auth_from_config(REGISTRY, config)
        assert result == ("user", "pass")

    def test_handles_direct_username_password_fields(self):
        config = {"auths": {REGISTRY: {"username": "u2", "password": "p2"}}}
        result = _get_auth_from_config(REGISTRY, config)
        assert result == ("u2", "p2")

    def test_returns_none_for_empty_entry(self):
        config = {"auths": {REGISTRY: {}}}
        assert _get_auth_from_config(REGISTRY, config) is None

    def test_returns_none_for_invalid_base64(self):
        config = {"auths": {REGISTRY: {"auth": "!!!not-valid-base64!!!"}}}
        assert _get_auth_from_config(REGISTRY, config) is None


# ===========================================================================
# TestGetCredHelper
# ===========================================================================

class TestGetCredHelper:

    def test_returns_helper_name(self):
        config = _cred_helper_config(REGISTRY, "ecr-login")
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config):
            assert _get_cred_helper(REGISTRY) == "ecr-login"

    def test_returns_none_when_not_configured(self):
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value={}):
            assert _get_cred_helper(REGISTRY) is None

    def test_returns_none_when_config_missing(self):
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None):
            assert _get_cred_helper(REGISTRY) is None


# ===========================================================================
# TestStoreCredentials
# ===========================================================================

class TestStoreCredentials:

    def test_uses_cred_helper_when_configured(self):
        config = _cred_helper_config(REGISTRY, "desktop")
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config), \
             patch("regshape.libs.auth.credentials.dockercredstore.store") as mock_store:
            store_credentials(REGISTRY, "alice", "secret")
            mock_store.assert_called_once_with(
                store="desktop",
                registry=REGISTRY,
                credentials={"Username": "alice", "Secret": "secret"},
            )

    def test_writes_to_config_file_when_no_helper(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            config_path = f.name

        try:
            with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                       return_value={}), \
                 patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                       return_value=config_path):
                store_credentials(REGISTRY, "bob", "tok123",
                                  docker_config_path=config_path)

            with open(config_path) as f:
                stored = json.load(f)

            assert REGISTRY in stored.get("auths", {})
            auth_b64 = stored["auths"][REGISTRY]["auth"]
            decoded = base64.b64decode(auth_b64).decode()
            assert decoded == "bob:tok123"
        finally:
            os.unlink(config_path)

    def test_creates_config_file_if_not_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, ".docker", "config.json")
            with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                       return_value=None), \
                 patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                       return_value=None), \
                 patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                       return_value=tmpdir), \
                 patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                       os.path.join(".docker", "config.json")):
                store_credentials(REGISTRY, "carol", "pw")

            assert os.path.exists(config_path)
            with open(config_path) as f:
                stored = json.load(f)
            assert REGISTRY in stored.get("auths", {})


# ===========================================================================
# TestEraseCredentials
# ===========================================================================

class TestEraseCredentials:

    def test_uses_cred_helper_when_configured(self):
        config = _cred_helper_config(REGISTRY, "desktop")
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config), \
             patch("regshape.libs.auth.credentials.dockercredstore.erase") as mock_erase:
            result = erase_credentials(REGISTRY)
            mock_erase.assert_called_once_with(store="desktop", registry=REGISTRY)
            assert result is True

    def test_removes_entry_from_config_file(self):
        initial = _auth_config(REGISTRY, "dave", "pw")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(initial, f)
            config_path = f.name

        try:
            with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                       return_value=initial), \
                 patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                       return_value=config_path):
                result = erase_credentials(REGISTRY, docker_config_path=config_path)

            assert result is True
            with open(config_path) as f:
                stored = json.load(f)
            assert REGISTRY not in stored.get("auths", {})
        finally:
            os.unlink(config_path)

    def test_returns_false_when_no_credentials_stored(self):
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value={}), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value="/some/path"):
            result = erase_credentials(REGISTRY)
            assert result is False

    def test_returns_false_when_config_not_found(self):
        with patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None):
            result = erase_credentials(REGISTRY)
            assert result is False


# ===========================================================================
# TestAuthLoginCommand
# ===========================================================================

BEARER_CHALLENGE = (
    'Bearer realm="https://auth.example.com/token",service="registry.example.com"'
)


class TestAuthLoginCommand:
    """CLI-level tests using click.testing.CliRunner."""

    def _runner(self):
        return CliRunner()

    def test_login_success_plain_text(self):
        """Successful login prints 'Login succeeded.' in plain mode."""
        ok_resp = _make_response(200)
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                   return_value="/tmp"), \
             patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                   os.path.join(".docker", "config.json")), \
             patch("requests.get", return_value=ok_resp), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "-r", REGISTRY, "-u", "alice", "-p", "secret"],
            )
        assert result.exit_code == 0, result.output
        assert "Login succeeded." in result.output

    def test_login_success_json_output(self):
        """Successful login with --json outputs a JSON success object."""
        ok_resp = _make_response(200)
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                   return_value="/tmp"), \
             patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                   os.path.join(".docker", "config.json")), \
             patch("requests.get", return_value=ok_resp), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["--json", "auth", "login", "-r", REGISTRY, "-u", "alice", "-p", "secret"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["registry"] == REGISTRY

    def test_login_bearer_challenge_cycle(self):
        """login completes successfully after a 401 Bearer challenge."""
        challenge_resp = _make_response(401, www_auth=BEARER_CHALLENGE)
        ok_after_token = _make_response(200)
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.text = json.dumps({"token": "abc123"})

        def fake_requests_get(url, **kwargs):
            if urlparse(url).hostname == "auth.example.com":
                return token_resp
            if kwargs.get("headers", {}).get("Authorization"):
                return ok_after_token
            return challenge_resp

        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                   return_value="/tmp"), \
             patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                   os.path.join(".docker", "config.json")), \
             patch("requests.get", side_effect=fake_requests_get), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "-r", REGISTRY, "-u", "alice", "-p", "secret"],
            )
        assert result.exit_code == 0, result.output
        assert "Login succeeded." in result.output

    def test_login_wrong_credentials(self):
        """Wrong credentials result in exit code 1 and an error message."""
        challenge_resp = _make_response(401, www_auth=BEARER_CHALLENGE)
        unauthorized_retry = _make_response(401)
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.text = json.dumps({"token": "bad-token"})

        def fake_requests_get(url, **kwargs):
            if urlparse(url).hostname == "auth.example.com":
                return token_resp
            return challenge_resp if not kwargs.get("headers") else unauthorized_retry

        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("requests.get", side_effect=fake_requests_get):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "-r", REGISTRY, "-u", "alice", "-p", "wrong"],
            )
        assert result.exit_code == 1

    def test_login_connection_error(self):
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("requests.get",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "-r", REGISTRY, "-u", "u", "-p", "p"],
            )
        assert result.exit_code == 1

    def test_login_password_stdin(self):
        """Password can be supplied via --password-stdin."""
        ok_resp = _make_response(200)
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                   return_value="/tmp"), \
             patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                   os.path.join(".docker", "config.json")), \
             patch("requests.get", return_value=ok_resp), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "-r", REGISTRY, "-u", "alice", "--password-stdin"],
                input="secret\n",
            )
        assert result.exit_code == 0, result.output
        assert "Login succeeded." in result.output

    def test_login_password_and_stdin_mutually_exclusive(self):
        """Supplying both --password and --password-stdin is an error."""
        result = self._runner().invoke(
            regshape,
            ["auth", "login", "-r", REGISTRY, "-u", "alice",
             "-p", "secret", "--password-stdin"],
            input="token\n",
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_login_insecure_uses_http(self):
        """--insecure causes _verify_credentials to use http:// scheme."""
        ok_resp = _make_response(200)
        captured_urls = []

        def fake_get(url, **kwargs):
            captured_urls.append(url)
            return ok_resp

        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                   return_value="/tmp"), \
             patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                   os.path.join(".docker", "config.json")), \
             patch("requests.get", side_effect=fake_get), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["--insecure", "auth", "login", "-r", REGISTRY, "-u", "u", "-p", "p"],
            )
        assert result.exit_code == 0, result.output
        assert captured_urls[0].startswith("http://"), (
            f"Expected http:// scheme, got: {captured_urls[0]}"
        )

    def test_login_uses_stored_credentials(self):
        """login resolves stored credentials when flags are omitted."""
        config = _auth_config(REGISTRY, "stored_user", "stored_pass")
        ok_resp = _make_response(200)
        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=config), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value="/tmp/config.json"), \
             patch("requests.get", return_value=ok_resp), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "-r", REGISTRY],
            )
        assert result.exit_code == 0, result.output
        assert "Login succeeded." in result.output


# ===========================================================================
# TestAuthLogoutCommand
# ===========================================================================

class TestAuthLogoutCommand:

    def _runner(self):
        return CliRunner()

    def test_logout_removes_credentials(self):
        """logout prints removal message when credentials exist."""
        with patch("regshape.cli.auth.erase_credentials",
                   return_value=True):
            result = self._runner().invoke(
                regshape,
                ["auth", "logout", "-r", REGISTRY],
            )
        assert result.exit_code == 0
        assert f"Removing login credentials for {REGISTRY}" in result.output

    def test_logout_not_logged_in(self):
        """logout prints informational message when no credentials stored."""
        with patch("regshape.cli.auth.erase_credentials",
                   return_value=False):
            result = self._runner().invoke(
                regshape,
                ["auth", "logout", "-r", REGISTRY],
            )
        assert result.exit_code == 0
        assert f"Not logged in to {REGISTRY}" in result.output

    def test_logout_json_output(self):
        """logout with --json outputs a JSON success object."""
        with patch("regshape.cli.auth.erase_credentials",
                   return_value=True):
            result = self._runner().invoke(
                regshape,
                ["--json", "auth", "logout", "-r", REGISTRY],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["registry"] == REGISTRY

    def test_logout_error_exits_nonzero(self):
        """logout exits 1 when erasing credentials raises AuthError."""
        with patch("regshape.cli.auth.erase_credentials",
                   side_effect=AuthError("erase failed", "helper error")):
            result = self._runner().invoke(
                regshape,
                ["auth", "logout", "-r", REGISTRY],
            )
        assert result.exit_code == 1


# ===========================================================================
# TestAuthLoginTelemetry
# ===========================================================================

class TestAuthLoginTelemetry:
    """Verify that telemetry flags emit output to stderr and do not
    contaminate stdout (including structured --json output)."""

    def _runner(self):
        # Click 8.3+ separates stdout/stderr in Result automatically.
        # result.stdout (or result.output) = stdout only
        # result.stderr = stderr only
        return CliRunner()

    def _ok_patches(self):
        """Context managers that make a login call succeed without I/O."""
        return (
            patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                  return_value=None),
            patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                  return_value=None),
            patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                  return_value="/tmp"),
            patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                  os.path.join(".docker", "config.json")),
            patch("requests.get", return_value=_make_response(200)),
            patch("regshape.libs.auth.credentials.store_credentials"),
        )

    def test_time_scenarios_writes_to_stderr_not_stdout(self):
        """--time-scenarios emits [SCENARIO] to stderr; stdout stays clean."""
        with contextlib.ExitStack() as stack:
            for p in self._ok_patches():
                stack.enter_context(p)
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "--time-scenarios",
                 "-r", REGISTRY, "-u", "alice", "-p", "s3cr3t"],
            )
        assert result.exit_code == 0, result.output
        assert "[SCENARIO] auth login" in result.stderr
        assert "[SCENARIO]" not in result.stdout

    def test_time_methods_writes_to_stderr_not_stdout(self):
        """--time-methods emits [TIMING] to stderr; stdout stays clean."""
        with contextlib.ExitStack() as stack:
            for p in self._ok_patches():
                stack.enter_context(p)
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "--time-methods",
                 "-r", REGISTRY, "-u", "alice", "-p", "s3cr3t"],
            )
        assert result.exit_code == 0, result.output
        assert "[TIMING]" in result.stderr
        assert "_verify_credentials" in result.stderr
        assert "[TIMING]" not in result.stdout

    def test_debug_calls_writes_to_stderr_not_stdout(self):
        """--debug-calls emits [CALL] block to stderr; stdout stays clean."""
        with contextlib.ExitStack() as stack:
            for p in self._ok_patches():
                stack.enter_context(p)
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "--debug-calls",
                 "-r", REGISTRY, "-u", "alice", "-p", "s3cr3t"],
            )
        assert result.exit_code == 0, result.output
        assert "[CALL]" in result.stderr
        assert "[RESPONSE HEADERS]" in result.stderr
        assert "[CALL]" not in result.stdout

    def test_debug_calls_redacts_authorization_header(self):
        """--debug-calls never logs the raw Bearer token or password."""
        challenge_resp = _make_response(401, www_auth=BEARER_CHALLENGE)
        ok_resp = _make_response(200)
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.text = json.dumps({"token": "super-secret-token"})

        def fake_get(url, **kwargs):
            if urlparse(url).hostname == "auth.example.com":
                return token_resp
            if kwargs.get("headers", {}).get("Authorization"):
                return ok_resp
            return challenge_resp

        with patch("regshape.libs.auth.credentials.dockerconfig.load_config",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.get_config_file",
                   return_value=None), \
             patch("regshape.libs.auth.credentials.dockerconfig.home_dir",
                   return_value="/tmp"), \
             patch("regshape.libs.auth.credentials.dockerconfig.DOCKER_CONFIG_FILENAME",
                   os.path.join(".docker", "config.json")), \
             patch("requests.get", side_effect=fake_get), \
             patch("regshape.libs.auth.credentials.store_credentials"):
            result = self._runner().invoke(
                regshape,
                ["auth", "login", "--debug-calls",
                 "-r", REGISTRY, "-u", "alice", "-p", "mypassword"],
            )
        assert result.exit_code == 0, result.output
        assert "mypassword" not in result.stderr
        assert "super-secret-token" not in result.stderr
        assert "<redacted>" in result.stderr

    def test_json_stdout_not_contaminated_by_telemetry(self):
        """With --json, stdout is valid JSON even when all telemetry flags active."""
        with contextlib.ExitStack() as stack:
            for p in self._ok_patches():
                stack.enter_context(p)
            result = self._runner().invoke(
                regshape,
                ["--json", "auth", "login",
                 "--time-scenarios", "--time-methods", "--debug-calls",
                 "-r", REGISTRY, "-u", "alice", "-p", "s3cr3t"],
            )
        assert result.exit_code == 0, result.output
        # stdout must be parseable as JSON — telemetry must not have bled in
        data = json.loads(result.stdout)
        assert data["status"] == "success"
        assert data["registry"] == REGISTRY
        # all telemetry lines present on stderr
        assert "[SCENARIO] auth login" in result.stderr
        assert "[TIMING]" in result.stderr
        assert "[CALL]" in result.stderr

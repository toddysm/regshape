"""Thin CLI wiring checks using real SDK ping results and transport failures.

The original ping CLI compatibility tests remain unchanged. These checks cover
the result-based path rather than mocking the operation itself.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError
from regshape.libs.transport import RegistryClient, TransportConfig


@pytest.mark.parametrize("status_code", [200, 401, 403, 503])
@pytest.mark.parametrize("as_json", [False, True])
def test_real_ping_result_rendering(status_code, as_json):
    """Real SDK states retain exact legacy CLI payloads, wording and exit codes."""
    client = MagicMock(spec=RegistryClient)
    client.config = TransportConfig(registry="ghcr.io")
    response = requests.Response()
    response.status_code = status_code
    response.headers["Docker-Distribution-API-Version"] = "registry/2.0"
    client.get.return_value = response
    args = ["ping", "-r", "ghcr.io"] + (["--json"] if as_json else [])

    with (
        patch("regshape.cli.ping.RegistryClient", return_value=client),
        patch(
            "regshape.libs.ping.operations.time.monotonic", side_effect=[10.0, 10.125]
        ),
    ):
        result = CliRunner().invoke(regshape, args)

    client.get.assert_called_once_with("/v2/")
    assert result.exit_code == (1 if status_code in (403, 503) else 0)
    if as_json:
        if status_code == 401:
            expected = {
                "registry": "ghcr.io",
                "reachable": True,
                "api_version": None,
                "latency_ms": None,
                "note": "Registry requires authentication",
                "error": f"Registry ghcr.io requires authentication : HTTP {status_code}",
            }
        elif status_code == 200:
            expected = {
                "registry": "ghcr.io",
                "reachable": True,
                "status_code": 200,
                "api_version": "registry/2.0",
                "latency_ms": 125.0,
            }
        else:
            expected = {
                "registry": "ghcr.io",
                "reachable": False,
                "error": f"HTTP {status_code}",
            }
        assert json.loads(result.output) == expected
    elif status_code == 401:
        assert result.output == (
            "Registry ghcr.io is reachable\n"
            "  Note: Registry requires authentication. "
            "Run 'regshape auth login' to configure credentials.\n"
        )
    elif status_code == 200:
        assert result.output == (
            "Registry ghcr.io is reachable\n"
            "  API Version: registry/2.0\n"
            "  Latency:     125ms\n"
        )
    else:
        assert result.output == (
            f"Error: Registry ghcr.io is not reachable: HTTP {status_code}\n"
        )


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("auth_error", [False, True])
def test_real_ping_exception_rendering(as_json, auth_error):
    """The CLI renders SDK-converted auth failures or SDK-selected transport detail."""
    client = MagicMock(spec=RegistryClient)
    client.config = TransportConfig(registry="ghcr.io")
    error = (
        AuthError("Token failed", "403")
        if auth_error
        else requests.exceptions.Timeout("original timeout text")
    )
    client.get.side_effect = error
    args = ["ping", "-r", "ghcr.io"] + (["--json"] if as_json else [])

    with patch("regshape.cli.ping.RegistryClient", return_value=client):
        result = CliRunner().invoke(regshape, args)

    assert result.exit_code == (0 if auth_error else 1)
    if as_json:
        expected = {
            "registry": "ghcr.io",
            "reachable": auth_error,
            "error": str(error),
        }
        if auth_error:
            expected.update(
                {
                    "api_version": None,
                    "latency_ms": None,
                    "note": "Registry requires authentication",
                }
            )
        assert json.loads(result.output) == expected
    elif auth_error:
        assert "Registry ghcr.io is reachable\n" in result.output
        assert "requires authentication" in result.output
    else:
        assert result.output == (
            "Error: Registry ghcr.io is not reachable: original timeout text\n"
        )

#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.catalog`."""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, CatalogError, CatalogNotSupportedError
from regshape.libs.models.catalog import RepositoryCatalog


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"

_CATALOG_REPOS = ["library/ubuntu", "myrepo/myimage", "myrepo/other"]

_CATALOG_JSON = json.dumps({"repositories": _CATALOG_REPOS})
_CATALOG_EMPTY_JSON = json.dumps({"repositories": []})
_CATALOG_NULL_JSON = json.dumps({"repositories": None})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalog(json_str: str = _CATALOG_JSON) -> RepositoryCatalog:
    """Build a RepositoryCatalog instance from a JSON string."""
    return RepositoryCatalog.from_json(json_str)


def _runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# TestCatalogListCommand
# ---------------------------------------------------------------------------

class TestCatalogListCommand:

    def test_list_prints_one_repo_per_line(self):
        with patch("regshape.cli.catalog.list_catalog", return_value=_catalog()):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 0
        assert "library/ubuntu" in result.output
        assert "myrepo/myimage" in result.output
        assert "myrepo/other" in result.output
        assert "{" not in result.output

    def test_list_json_flag(self):
        with patch("regshape.cli.catalog.list_catalog", return_value=_catalog()):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--json",
            ])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["repositories"] == _CATALOG_REPOS

    def test_list_empty_catalog(self):
        with patch("regshape.cli.catalog.list_catalog",
                   return_value=_catalog(_CATALOG_EMPTY_JSON)):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_null_repositories_normalised(self):
        with patch("regshape.cli.catalog.list_catalog",
                   return_value=_catalog(_CATALOG_NULL_JSON)):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_passes_n_param(self):
        with patch("regshape.cli.catalog.list_catalog",
                   return_value=_catalog()) as mock_list:
            _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--n", "50",
            ])
        assert mock_list.call_args[1]["page_size"] == 50

    def test_list_passes_last_param(self):
        with patch("regshape.cli.catalog.list_catalog",
                   return_value=_catalog()) as mock_list:
            _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--last", "myrepo/other",
            ])
        assert mock_list.call_args[1]["last"] == "myrepo/other"

    def test_list_output_flag_writes_file(self, tmp_path):
        out_file = tmp_path / "repos.txt"
        with patch("regshape.cli.catalog.list_catalog", return_value=_catalog()):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "-o", str(out_file),
            ])
        assert result.exit_code == 0
        content = out_file.read_text()
        assert "library/ubuntu" in content
        assert "myrepo/myimage" in content

    def test_list_uses_secure_transport_by_default(self):
        with patch("regshape.cli.catalog.list_catalog", return_value=_catalog()), \
             patch("regshape.cli.catalog.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is False

    def test_list_uses_insecure_transport_when_flag_set(self):
        with patch("regshape.cli.catalog.list_catalog", return_value=_catalog()), \
             patch("regshape.cli.catalog.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, [
                "--insecure", "catalog", "list", "-r", REGISTRY,
            ])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is True

    # -----------------------------------------------------------------------
    # --all flag
    # -----------------------------------------------------------------------

    def test_list_all_calls_list_catalog_all(self):
        with patch("regshape.cli.catalog.list_catalog_all",
                   return_value=_catalog()) as mock_all:
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--all",
            ])
        assert result.exit_code == 0
        assert mock_all.called

    def test_list_all_passes_n_param(self):
        with patch("regshape.cli.catalog.list_catalog_all",
                   return_value=_catalog()) as mock_all:
            _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--all", "--n", "100",
            ])
        assert mock_all.call_args[1]["page_size"] == 100

    def test_list_all_does_not_accept_last(self):
        result = _runner().invoke(regshape, [
            "catalog", "list", "-r", REGISTRY, "--all", "--last", "some/repo",
        ])
        assert result.exit_code == 2
        assert "--all and --last are mutually exclusive" in result.output

    # -----------------------------------------------------------------------
    # Error handling
    # -----------------------------------------------------------------------

    def test_catalog_not_supported_exits_3(self):
        with patch("regshape.cli.catalog.list_catalog",
                   side_effect=CatalogNotSupportedError(
                       "Registry does not support the catalog API: acr.example.io",
                       "HTTP 404"
                   )):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 3
        assert "does not support" in result.output

    def test_catalog_not_supported_all_exits_3(self):
        with patch("regshape.cli.catalog.list_catalog_all",
                   side_effect=CatalogNotSupportedError(
                       "Registry does not support the catalog API: acr.example.io",
                       "HTTP 405"
                   )):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--all",
            ])
        assert result.exit_code == 3

    def test_auth_error_exits_1(self):
        with patch("regshape.cli.catalog.list_catalog",
                   side_effect=AuthError("Authentication failed", "HTTP 401")):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 1

    def test_catalog_error_exits_1(self):
        with patch("regshape.cli.catalog.list_catalog",
                   side_effect=CatalogError(
                       "Registry error for acr.example.io",
                       "HTTP 500"
                   )):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 1

    def test_connection_error_exits_1(self):
        with patch("regshape.cli.catalog.list_catalog",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY,
            ])
        assert result.exit_code == 1

    def test_missing_registry_exits_nonzero(self):
        result = _runner().invoke(regshape, [
            "catalog", "list",
        ])
        assert result.exit_code != 0

    def test_error_message_format(self):
        with patch("regshape.cli.catalog.list_catalog",
                   side_effect=CatalogNotSupportedError(
                       "Registry does not support the catalog API: acr.example.io",
                       "HTTP 404"
                   )):
            result = _runner().invoke(
                regshape,
                ["catalog", "list", "-r", REGISTRY],
            )
        assert f"Error [{REGISTRY}]:" in result.output

    # -----------------------------------------------------------------------
    # Output format
    # -----------------------------------------------------------------------

    def test_list_json_empty_catalog(self):
        with patch("regshape.cli.catalog.list_catalog",
                   return_value=_catalog(_CATALOG_EMPTY_JSON)):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--json",
            ])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["repositories"] == []

    def test_list_json_output_flag(self, tmp_path):
        out_file = tmp_path / "repos.json"
        with patch("regshape.cli.catalog.list_catalog", return_value=_catalog()):
            result = _runner().invoke(regshape, [
                "catalog", "list", "-r", REGISTRY, "--json", "-o", str(out_file),
            ])
        assert result.exit_code == 0
        parsed = json.loads(out_file.read_text())
        assert parsed["repositories"] == _CATALOG_REPOS

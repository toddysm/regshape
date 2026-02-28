#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.tag`."""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, TagError
from regshape.libs.models.tags import TagList
from regshape.libs.refs import parse_image_ref as _parse_image_ref

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
TAG = "v1.0"
DIGEST = "sha256:" + "a" * 64

_TAG_LIST_JSON = json.dumps({
    "name": f"{REPO}",
    "tags": ["latest", "v1.0", "v2.0"],
})

_TAG_LIST_EMPTY_JSON = json.dumps({
    "name": f"{REPO}",
    "tags": [],
})

_TAG_LIST_NULL_JSON = json.dumps({
    "name": f"{REPO}",
    "tags": None,
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tag_list(json_str: str = _TAG_LIST_JSON) -> TagList:
    """Build a TagList instance from a JSON string."""
    return TagList.from_json(json_str)


def _runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# TestParseImageRef — unit tests for the reference parser
# ---------------------------------------------------------------------------

class TestParseImageRef:

    def test_registry_with_tag(self):
        reg, repo, ref = _parse_image_ref("acr.io/myrepo/myimage:v1")
        assert reg == "acr.io"
        assert repo == "myrepo/myimage"
        assert ref == "v1"

    def test_registry_with_digest(self):
        reg, repo, ref = _parse_image_ref(f"acr.io/myrepo/myimage@{DIGEST}")
        assert reg == "acr.io"
        assert repo == "myrepo/myimage"
        assert ref == DIGEST

    def test_registry_no_tag_defaults_to_latest(self):
        reg, repo, ref = _parse_image_ref("acr.io/myrepo/myimage")
        assert reg == "acr.io"
        assert ref == "latest"

    def test_no_registry_raises(self):
        with pytest.raises(ValueError, match="registry"):
            _parse_image_ref("myrepo/myimage:v1")

    def test_no_repo_raises(self):
        with pytest.raises(ValueError, match="repository"):
            _parse_image_ref("acr.io/")

    def test_localhost_registry(self):
        reg, repo, ref = _parse_image_ref("localhost:5000/myimage:dev")
        assert reg == "localhost:5000"
        assert repo == "myimage"
        assert ref == "dev"


# ---------------------------------------------------------------------------
# TestTagListCommand
# ---------------------------------------------------------------------------

class TestTagListCommand:

    def test_list_prints_one_tag_per_line(self):
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert "latest" in result.output
        assert "v1.0" in result.output
        assert "v2.0" in result.output
        assert "{" not in result.output

    def test_list_json_flag(self):
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--json",
            ])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["name"] == REPO
        assert parsed["tags"] == ["latest", "v1.0", "v2.0"]

    def test_list_empty_repository(self):
        with patch("regshape.cli.tag.list_tags",
                   return_value=_tag_list(_TAG_LIST_EMPTY_JSON)):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_null_tags_normalised(self):
        with patch("regshape.cli.tag.list_tags",
                   return_value=_tag_list(_TAG_LIST_NULL_JSON)):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_passes_n_param(self):
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()) as mock_list:
            _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--n", "5",
            ])
        assert mock_list.call_args[1]["page_size"] == 5

    def test_list_passes_last_param(self):
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()) as mock_list:
            _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--last", "v1.0",
            ])
        assert mock_list.call_args[1]["last"] == "v1.0"

    def test_list_ignores_tag_suffix_in_image_ref(self):
        """A tag suffix in --image-ref is stripped; only registry+repo are used."""
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()) as mock_list:
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}:latest",
            ])
        assert result.exit_code == 0
        assert mock_list.call_args[1]["repo"] == REPO

    def test_list_404_exits_1(self):
        with patch("regshape.cli.tag.list_tags",
                   side_effect=TagError(
                       f"Repository not found: {REGISTRY}/{REPO}", "NAME_UNKNOWN"
                   )):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 1
        assert "Repository not found" in result.output

    def test_list_auth_error_exits_1(self):
        """Authentication failure from transport layer exits with code 1."""
        with patch("regshape.cli.tag.list_tags",
                   side_effect=AuthError("Authentication failed", "HTTP 401")):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 1

    def test_list_output_flag_writes_file(self, tmp_path):
        out_file = tmp_path / "tags.txt"
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "-o", str(out_file),
            ])
        assert result.exit_code == 0
        assert "latest" in out_file.read_text()

    def test_list_bad_image_ref_exits_1(self):
        result = _runner().invoke(regshape, [
            "tag", "list", "-i", "no-registry/myimage",
        ])
        assert result.exit_code == 1

    def test_list_uses_secure_transport_by_default(self):
        """RegistryClient is created with insecure=False by default."""
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()), \
             patch("regshape.cli.tag.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is False

    def test_list_uses_insecure_transport_when_flag_set(self):
        """--insecure flag propagates insecure=True to TransportConfig."""
        with patch("regshape.cli.tag.list_tags", return_value=_tag_list()), \
             patch("regshape.cli.tag.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, [
                "--insecure", "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is True

    def test_list_connection_error_exits_1(self):
        with patch("regshape.cli.tag.list_tags",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TestTagDeleteCommand
# ---------------------------------------------------------------------------

class TestTagDeleteCommand:

    def test_delete_success(self):
        with patch("regshape.cli.tag.delete_tag", return_value=None):
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 0
        assert f"Deleted tag: {REGISTRY}/{REPO}:{TAG}" in result.output

    def test_delete_digest_ref_exits_2(self):
        result = _runner().invoke(regshape, [
            "tag", "delete", "-i", f"{REGISTRY}/{REPO}@{DIGEST}",
        ])
        assert result.exit_code == 2
        assert "manifest delete" in result.output

    def test_delete_404_exits_1(self):
        with patch("regshape.cli.tag.delete_tag",
                   side_effect=TagError(
                       f"Tag not found: {REGISTRY}/{REPO}:{TAG}", "MANIFEST_UNKNOWN"
                   )):
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1

    def test_delete_405_tag_deletion_disabled(self):
        with patch("regshape.cli.tag.delete_tag",
                   side_effect=TagError(
                       "Tag deletion is not supported by this registry", "HTTP 405"
                   )):
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1
        assert "not supported" in result.output

    def test_delete_auth_error_exits_1(self):
        """Authentication failure from transport exits with code 1."""
        with patch("regshape.cli.tag.delete_tag",
                   side_effect=AuthError("Authentication failed", "HTTP 401")):
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1

    def test_delete_uses_secure_transport_by_default(self):
        """RegistryClient is created with insecure=False by default."""
        with patch("regshape.cli.tag.delete_tag", return_value=None), \
             patch("regshape.cli.tag.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is False

    def test_delete_uses_insecure_transport_when_flag_set(self):
        """--insecure flag propagates insecure=True to TransportConfig."""
        with patch("regshape.cli.tag.delete_tag", return_value=None), \
             patch("regshape.cli.tag.RegistryClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            _runner().invoke(regshape, [
                "--insecure", "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        config = mock_client_cls.call_args[0][0]
        assert config.insecure is True

    def test_delete_bad_image_ref_exits_1(self):
        result = _runner().invoke(regshape, [
            "tag", "delete", "-i", "no-registry/myimage:tag",
        ])
        assert result.exit_code == 1

    def test_delete_connection_error_exits_1(self):
        with patch("regshape.cli.tag.delete_tag",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1

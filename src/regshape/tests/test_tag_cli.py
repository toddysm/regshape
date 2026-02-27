#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.tag`."""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from regshape.cli.main import regshape
from regshape.cli.tag import _parse_image_ref

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
    resp.json.return_value = json.loads(body) if body.strip().startswith("{") else {}
    return resp


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
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert "latest" in result.output
        assert "v1.0" in result.output
        assert "v2.0" in result.output
        # one per line — no JSON wrapper
        assert "{" not in result.output

    def test_list_json_flag(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--json",
            ])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["name"] == REPO
        assert parsed["tags"] == ["latest", "v1.0", "v2.0"]

    def test_list_empty_repository(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_EMPTY_JSON)
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_null_tags_normalised(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_NULL_JSON)
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_passes_n_param(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--n", "5",
            ])
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["n"] == 5

    def test_list_passes_last_param(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--last", "v1.0",
            ])
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["last"] == "v1.0"

    def test_list_ignores_tag_suffix_in_image_ref(self):
        """A tag suffix in --image-ref is stripped; only registry+repo are used."""
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}:latest",
            ])
        assert result.exit_code == 0
        # URL should not include the tag
        url_arg = mock_req.call_args[0][0]
        assert "/tags/list" in url_arg
        assert ":latest" not in url_arg

    def test_list_404_exits_1(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(
                404,
                json.dumps({"errors": [{"code": "NAME_UNKNOWN", "message": "repo not found"}]}),
            )
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 1
        assert "Repository not found" in result.output

    def test_list_auth_challenge_bearer(self):
        """401 with Bearer WWW-Authenticate triggers token exchange and retry."""
        auth_resp = _make_response(
            401,
            www_auth='Bearer realm="https://auth.example.io/token",service="acr.example.io"',
        )
        ok_resp = _make_response(200, _TAG_LIST_JSON)
        with patch("regshape.cli.tag.http_request", side_effect=[auth_resp, ok_resp]), \
             patch("regshape.cli.tag.resolve_credentials", return_value=("alice", "secret")), \
             patch("regshape.cli.tag.registryauth.authenticate", return_value="mytoken"):
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 0
        assert "latest" in result.output

    def test_list_401_no_www_authenticate_exits_1(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(401)
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 1

    def test_list_basic_auth_no_credentials_exits_1(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(
                401, www_auth='Basic realm="registry"'
            )
            result = _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        assert result.exit_code == 1

    def test_list_output_flag_writes_file(self, tmp_path):
        out_file = tmp_path / "tags.txt"
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
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

    def test_list_url_uses_https_by_default(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            _runner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        url = mock_req.call_args[0][0]
        assert url.startswith("https://")

    def test_list_url_uses_http_when_insecure(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(200, _TAG_LIST_JSON)
            _runner().invoke(regshape, [
                "--insecure", "tag", "list", "-i", f"{REGISTRY}/{REPO}",
            ])
        url = mock_req.call_args[0][0]
        assert url.startswith("http://")


# ---------------------------------------------------------------------------
# TestTagDeleteCommand
# ---------------------------------------------------------------------------

class TestTagDeleteCommand:

    def test_delete_success(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(202)
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
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(
                404,
                json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message": "tag not found"}]}),
            )
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1

    def test_delete_405_tag_deletion_disabled(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(405)
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1
        assert "not supported" in result.output

    def test_delete_auth_challenge_bearer(self):
        auth_resp = _make_response(
            401,
            www_auth='Bearer realm="https://auth.example.io/token",service="acr.example.io"',
        )
        ok_resp = _make_response(202)
        with patch("regshape.cli.tag.http_request", side_effect=[auth_resp, ok_resp]), \
             patch("regshape.cli.tag.resolve_credentials", return_value=("alice", "secret")), \
             patch("regshape.cli.tag.registryauth.authenticate", return_value="mytoken"):
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 0
        assert "Deleted tag" in result.output

    def test_delete_401_no_www_authenticate_exits_1(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(401)
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1

    def test_delete_basic_auth_no_credentials_exits_1(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(
                401, www_auth='Basic realm="registry"'
            )
            result = _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        assert result.exit_code == 1

    def test_delete_uses_manifests_endpoint(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(202)
            _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        url = mock_req.call_args[0][0]
        assert f"/v2/{REPO}/manifests/{TAG}" in url

    def test_delete_url_uses_https_by_default(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(202)
            _runner().invoke(regshape, [
                "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        url = mock_req.call_args[0][0]
        assert url.startswith("https://")

    def test_delete_url_uses_http_when_insecure(self):
        with patch("regshape.cli.tag.http_request") as mock_req, \
             patch("regshape.cli.tag.resolve_credentials", return_value=(None, None)):
            mock_req.return_value = _make_response(202)
            _runner().invoke(regshape, [
                "--insecure", "tag", "delete", "-i", f"{REGISTRY}/{REPO}:{TAG}",
            ])
        url = mock_req.call_args[0][0]
        assert url.startswith("http://")

    def test_delete_bad_image_ref_exits_1(self):
        result = _runner().invoke(regshape, [
            "tag", "delete", "-i", "no-registry/myimage:tag",
        ])
        assert result.exit_code == 1

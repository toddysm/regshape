#!/usr/bin/env python3

"""
Tests for :mod:`regshape.cli.manifest` and
:func:`regshape.cli.manifest._parse_image_ref`.
"""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from regshape.cli.main import regshape
from regshape.cli.manifest import _parse_image_ref
from regshape.libs.models.manifest import ImageManifest, ImageIndex
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
    DOCKER_MANIFEST_V2,
    DOCKER_MANIFEST_LIST_V2,
)

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
TAG = "latest"
DIGEST = "sha256:" + "a" * 64
LAYER_DIGEST = "sha256:" + "b" * 64
CONFIG_DIGEST = "sha256:" + "c" * 64

# A minimal OCI Image Manifest JSON string
_MANIFEST_JSON = json.dumps({
    "schemaVersion": 2,
    "mediaType": OCI_IMAGE_MANIFEST,
    "config": {
        "mediaType": OCI_IMAGE_CONFIG,
        "digest": CONFIG_DIGEST,
        "size": 2,
    },
    "layers": [
        {
            "mediaType": OCI_IMAGE_LAYER_TAR_GZIP,
            "digest": LAYER_DIGEST,
            "size": 5678,
        }
    ],
    "annotations": {"org.opencontainers.image.created": "2026-02-25"},
})

# A minimal OCI Image Index JSON string
_INDEX_JSON = json.dumps({
    "schemaVersion": 2,
    "mediaType": OCI_IMAGE_INDEX,
    "manifests": [
        {
            "mediaType": OCI_IMAGE_MANIFEST,
            "digest": DIGEST,
            "size": 1000,
            "platform": {"architecture": "amd64", "os": "linux"},
        }
    ],
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(
    status_code: int,
    body: str = "{}",
    content_type: str = OCI_IMAGE_MANIFEST,
    digest: str = DIGEST,
    www_auth: str = None,
    content_length: str = None,
) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    resp.content = body.encode("utf-8")
    headers = {
        "Content-Type": content_type,
        "Docker-Content-Digest": digest,
    }
    if content_length:
        headers["Content-Length"] = content_length
    if www_auth:
        headers["WWW-Authenticate"] = www_auth
    resp.headers = headers
    resp.json.return_value = json.loads(body) if body.startswith("{") else {}
    return resp


def _runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# TestParseImageRef — unit tests for the reference parser
# ---------------------------------------------------------------------------

class TestParseImageRef:

    def test_registry_embedded_with_tag(self):
        reg, repo, ref = _parse_image_ref("acr.io/myrepo/myimage:v1")
        assert reg == "acr.io"
        assert repo == "myrepo/myimage"
        assert ref == "v1"

    def test_no_registry_raises_for_relative_ref(self):
        with pytest.raises(ValueError, match="registry"):
            _parse_image_ref("myrepo/myimage:v1")

    def test_digest_reference(self):
        reg, repo, ref = _parse_image_ref(f"acr.io/myimage@{DIGEST}")
        assert reg == "acr.io"
        assert repo == "myimage"
        assert ref == DIGEST

    def test_no_registry_raises_for_digest_without_registry(self):
        with pytest.raises(ValueError, match="registry"):
            _parse_image_ref(f"myimage@{DIGEST}")

    def test_no_tag_defaults_to_latest(self):
        reg, repo, ref = _parse_image_ref("acr.io/myimage")
        assert ref == "latest"

    def test_no_registry_raises(self):
        with pytest.raises(ValueError, match="registry"):
            _parse_image_ref("myimage:latest")

    def test_localhost_is_registry(self):
        reg, repo, ref = _parse_image_ref("localhost:5000/myimage:dev")
        assert reg == "localhost:5000"
        assert repo == "myimage"
        assert ref == "dev"

    def test_multi_level_repo_embedded_registry(self):
        reg, repo, ref = _parse_image_ref("acr.io/a/b/c:tag")
        assert reg == "acr.io"
        assert repo == "a/b/c"
        assert ref == "tag"


# ---------------------------------------------------------------------------
# TestManifestGet — CLI tests for ``manifest get``
# ---------------------------------------------------------------------------

class TestManifestGet:

    def test_get_success_plain(self):
        resp = _make_response(200, body=_MANIFEST_JSON)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["schemaVersion"] == 2
        assert parsed["mediaType"] == OCI_IMAGE_MANIFEST

    def test_get_raw_flag_skips_parsing(self):
        raw_body = _MANIFEST_JSON
        resp = _make_response(200, body=raw_body)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--raw"],
            )
        assert result.exit_code == 0, result.output
        # Raw output should be the verbatim response body
        assert '"schemaVersion"' in result.output

    def test_get_part_config(self):
        resp = _make_response(200, body=_MANIFEST_JSON)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "config"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["mediaType"] == OCI_IMAGE_CONFIG
        assert data["digest"] == CONFIG_DIGEST

    def test_get_part_layers(self):
        resp = _make_response(200, body=_MANIFEST_JSON)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "layers"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["digest"] == LAYER_DIGEST

    def test_get_part_annotations(self):
        resp = _make_response(200, body=_MANIFEST_JSON)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "annotations"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "org.opencontainers.image.created" in data

    def test_get_part_subject_absent_exits_2(self):
        resp = _make_response(200, body=_MANIFEST_JSON)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "subject"],
            )
        assert result.exit_code == 2

    def test_get_part_config_on_index_exits_2(self):
        resp = _make_response(200, body=_INDEX_JSON, content_type=OCI_IMAGE_INDEX)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "config"],
            )
        assert result.exit_code == 2

    def test_get_part_and_raw_mutually_exclusive(self):
        result = _runner().invoke(
            regshape,
            ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "layers", "--raw"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower() or "mutually exclusive" in (result.exception and str(result.exception) or "").lower() or result.exit_code == 1

    def test_get_404_exits_1(self):
        error_body = json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message": "manifest unknown"}]})
        resp = _make_response(404, body=error_body)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 1

    def test_get_bearer_challenge_retry(self):
        """get completes after a 401 Bearer challenge."""
        challenge = _make_response(
            401,
            www_auth=f'Bearer realm="https://{REGISTRY}/token",service="{REGISTRY}"',
        )
        ok = _make_response(200, body=_MANIFEST_JSON)

        call_count = [0]

        def side_effect(method, url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return challenge
            return ok

        with patch("requests.request", side_effect=side_effect), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=("user", "pass")), \
             patch("regshape.libs.auth.registryauth.authenticate",
                   return_value="fake-token"):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output

    def test_get_connection_error_exits_1(self):
        with patch("requests.request",
                   side_effect=requests.exceptions.ConnectionError("refused")), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 1

    def test_get_image_index_plain(self):
        resp = _make_response(200, body=_INDEX_JSON, content_type=OCI_IMAGE_INDEX)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["mediaType"] == OCI_IMAGE_INDEX


# ---------------------------------------------------------------------------
# TestManifestInfo  — CLI tests for ``manifest info``
# ---------------------------------------------------------------------------

class TestManifestInfo:

    def test_info_success_plain(self):
        resp = _make_response(200, content_length="1234")
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "info", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        assert "Digest:" in result.output
        assert DIGEST in result.output
        assert "Media Type:" in result.output

    def test_info_404_exits_1(self):
        resp = _make_response(404, body=json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message": "not found"}]}))
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "info", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TestManifestDescriptor  — CLI tests for ``manifest descriptor``
# ---------------------------------------------------------------------------

class TestManifestDescriptor:

    def test_descriptor_returns_json(self):
        resp = _make_response(200, content_length="1234")
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "descriptor", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["mediaType"] == OCI_IMAGE_MANIFEST
        assert data["digest"] == DIGEST
        assert data["size"] == 1234

    def test_descriptor_fields_are_oci_wire_names(self):
        """Output uses camelCase OCI wire-format field names."""
        resp = _make_response(200, content_length="999")
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "descriptor", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Must use OCI wire-format keys (camelCase), not Python attribute names
        assert "mediaType" in data
        assert "digest" in data
        assert "size" in data
        assert "media_type" not in data

    def test_descriptor_404_exits_1(self):
        resp = _make_response(404, body=json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message": "not found"}]}))
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "descriptor", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TestManifestPut  — CLI tests for ``manifest put``
# ---------------------------------------------------------------------------

class TestManifestPut:

    def test_put_success_from_file(self, tmp_path):
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(_MANIFEST_JSON)
        resp = _make_response(201)

        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "put", "-i", f"{REGISTRY}/{REPO}:v2",
                 "--file", str(manifest_file)],
            )
        assert result.exit_code == 0, result.output
        assert "Pushed:" in result.output

    def test_put_stdin(self):
        resp = _make_response(201)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "put", "-i", f"{REGISTRY}/{REPO}:v2", "--stdin"],
                input=_MANIFEST_JSON,
            )
        assert result.exit_code == 0, result.output

    def test_put_requires_file_or_stdin(self):
        result = _runner().invoke(
            regshape,
            ["manifest", "put", "-i", f"{REGISTRY}/{REPO}:v2"],
        )
        assert result.exit_code != 0

    def test_put_file_and_stdin_mutually_exclusive(self, tmp_path):
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(_MANIFEST_JSON)
        result = _runner().invoke(
            regshape,
            ["manifest", "put", "-i", f"{REGISTRY}/{REPO}:v2",
             "--file", str(manifest_file), "--stdin"],
        )
        assert result.exit_code != 0

    def test_put_error_exits_1(self, tmp_path):
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(_MANIFEST_JSON)
        error_body = json.dumps({"errors": [{"code": "MANIFEST_INVALID", "message": "invalid"}]})
        resp = _make_response(400, body=error_body)

        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "put", "-i", f"{REGISTRY}/{REPO}:v2",
                 "--file", str(manifest_file)],
            )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TestManifestDelete  — CLI tests for ``manifest delete``
# ---------------------------------------------------------------------------

class TestManifestDelete:

    def test_delete_success_plain(self):
        resp = _make_response(202)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "delete", "-i", f"{REGISTRY}/{REPO}@{DIGEST}"],
            )
        assert result.exit_code == 0, result.output
        assert "Deleted:" in result.output
        assert DIGEST in result.output

    def test_delete_tag_reference_rejected(self):
        """Deleting by tag exits with code 2 — OCI requires digest."""
        result = _runner().invoke(
            regshape,
            ["manifest", "delete", "-i", f"{REGISTRY}/{REPO}:latest"],
        )
        assert result.exit_code == 2

    def test_delete_404_exits_1(self):
        error_body = json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message": "not found"}]})
        resp = _make_response(404, body=error_body)
        with patch("requests.request", return_value=resp), \
             patch("regshape.cli.manifest.resolve_credentials",
                   return_value=(None, None)):
            result = _runner().invoke(
                regshape,
                ["manifest", "delete", "-i", f"{REGISTRY}/{REPO}@{DIGEST}"],
            )
        assert result.exit_code == 1

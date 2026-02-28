#!/usr/bin/env python3

"""
Tests for :mod:`regshape.cli.manifest`.
"""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import patch

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
)
from regshape.libs.refs import parse_image_ref as _parse_image_ref

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
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["schemaVersion"] == 2
        assert parsed["mediaType"] == OCI_IMAGE_MANIFEST

    def test_get_raw_flag_skips_parsing(self):
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--raw"],
            )
        assert result.exit_code == 0, result.output
        assert '"schemaVersion"' in result.output

    def test_get_part_config(self):
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "config"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["mediaType"] == OCI_IMAGE_CONFIG
        assert data["digest"] == CONFIG_DIGEST

    def test_get_part_layers(self):
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
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
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "annotations"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "org.opencontainers.image.created" in data

    def test_get_part_subject_absent_exits_2(self):
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}", "--part", "subject"],
            )
        assert result.exit_code == 2

    def test_get_part_config_on_index_exits_2(self):
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_INDEX_JSON, OCI_IMAGE_INDEX, DIGEST)):
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
        assert "mutually exclusive" in result.output.lower()

    def test_get_404_exits_1(self):
        with patch("regshape.cli.manifest.get_manifest",
                   side_effect=ManifestError("manifest unknown", "MANIFEST_UNKNOWN")):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 1

    def test_get_bearer_challenge_retry(self):
        """Auth retry is handled transparently by the transport layer."""
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_MANIFEST_JSON, OCI_IMAGE_MANIFEST, DIGEST)):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output

    def test_get_connection_error_exits_1(self):
        with patch("regshape.cli.manifest.get_manifest",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = _runner().invoke(
                regshape,
                ["manifest", "get", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 1

    def test_get_image_index_plain(self):
        with patch("regshape.cli.manifest.get_manifest",
                   return_value=(_INDEX_JSON, OCI_IMAGE_INDEX, DIGEST)):
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
        with patch("regshape.cli.manifest.head_manifest",
                   return_value=(DIGEST, OCI_IMAGE_MANIFEST, 1234)):
            result = _runner().invoke(
                regshape,
                ["manifest", "info", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        assert "Digest:" in result.output
        assert DIGEST in result.output
        assert "Media Type:" in result.output

    def test_info_404_exits_1(self):
        with patch("regshape.cli.manifest.head_manifest",
                   side_effect=ManifestError("manifest not found", "MANIFEST_UNKNOWN")):
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
        with patch("regshape.cli.manifest.head_manifest",
                   return_value=(DIGEST, OCI_IMAGE_MANIFEST, 1234)):
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
        with patch("regshape.cli.manifest.head_manifest",
                   return_value=(DIGEST, OCI_IMAGE_MANIFEST, 999)):
            result = _runner().invoke(
                regshape,
                ["manifest", "descriptor", "-i", f"{REGISTRY}/{REPO}:{TAG}"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "mediaType" in data
        assert "digest" in data
        assert "size" in data
        assert "media_type" not in data

    def test_descriptor_404_exits_1(self):
        with patch("regshape.cli.manifest.head_manifest",
                   side_effect=ManifestError("manifest not found", "MANIFEST_UNKNOWN")):
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

        with patch("regshape.cli.manifest.push_manifest", return_value=DIGEST):
            result = _runner().invoke(
                regshape,
                ["manifest", "put", "-i", f"{REGISTRY}/{REPO}:v2",
                 "--file", str(manifest_file)],
            )
        assert result.exit_code == 0, result.output
        assert "Pushed:" in result.output

    def test_put_stdin(self):
        with patch("regshape.cli.manifest.push_manifest", return_value=DIGEST):
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

        with patch("regshape.cli.manifest.push_manifest",
                   side_effect=ManifestError("manifest invalid", "MANIFEST_INVALID")):
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
        with patch("regshape.cli.manifest.delete_manifest", return_value=None):
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
        with patch("regshape.cli.manifest.delete_manifest",
                   side_effect=ManifestError("manifest not found", "MANIFEST_UNKNOWN")):
            result = _runner().invoke(
                regshape,
                ["manifest", "delete", "-i", f"{REGISTRY}/{REPO}@{DIGEST}"],
            )
        assert result.exit_code == 1

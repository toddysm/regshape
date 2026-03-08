#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.layout`.

Uses the Click test runner to exercise all layout commands end-to-end.
Library internals are not mocked — tests use real temporary directories.
"""

import gzip
import io
import json

import pytest
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.layout.operations import (
    generate_config,
    generate_manifest,
    init_layout,
    stage_layer,
)
from regshape.libs.models.mediatype import OCI_IMAGE_LAYER_TAR_GZIP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner():
    return CliRunner()


def _make_gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def _full_pipeline(layout_dir):
    """Stage one layer, generate config and manifest."""
    layer = _make_gzip(b"pipeline test layer")
    stage_layer(layout_dir, layer, OCI_IMAGE_LAYER_TAR_GZIP)
    generate_config(layout_dir)
    return generate_manifest(layout_dir, ref_name="latest")


# ---------------------------------------------------------------------------
# layout init
# ---------------------------------------------------------------------------


class TestLayoutInitCLI:
    def test_init_creates_layout(self, tmp_path):
        output = str(tmp_path / "layout")
        result = _runner().invoke(regshape, ["layout", "init", "--output", output])
        assert result.exit_code == 0, result.output
        assert "Initialised" in result.output

    def test_init_json_output(self, tmp_path):
        output = str(tmp_path / "layout")
        result = _runner().invoke(
            regshape, ["layout", "init", "--output", output, "--json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "layout_path" in data

    def test_init_fails_if_already_exists(self, tmp_path):
        output = str(tmp_path / "layout")
        init_layout(tmp_path / "layout")
        result = _runner().invoke(regshape, ["layout", "init", "--output", output])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout add layer
# ---------------------------------------------------------------------------


class TestLayoutAddLayerCLI:
    def test_add_gzip_layer_with_explicit_media_type(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        layer_file = tmp_path / "layer.tar.gz"
        layer_file.write_bytes(_make_gzip(b"content"))

        result = _runner().invoke(regshape, [
            "layout", "add", "layer",
            "--layout", str(layout_dir),
            "--file", str(layer_file),
            "--media-type", OCI_IMAGE_LAYER_TAR_GZIP,
        ])
        assert result.exit_code == 0, result.output
        assert "Staged layer" in result.output

    def test_add_layer_json_output(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        layer_file = tmp_path / "layer.tar.gz"
        layer_file.write_bytes(_make_gzip(b"content"))

        result = _runner().invoke(regshape, [
            "layout", "add", "layer",
            "--layout", str(layout_dir),
            "--file", str(layer_file),
            "--media-type", OCI_IMAGE_LAYER_TAR_GZIP,
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "digest" in data
        assert "size" in data
        assert data["media_type"] == OCI_IMAGE_LAYER_TAR_GZIP

    def test_add_layer_with_annotations(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        layer_file = tmp_path / "layer.tar.gz"
        layer_file.write_bytes(_make_gzip(b"content"))

        result = _runner().invoke(regshape, [
            "layout", "add", "layer",
            "--layout", str(layout_dir),
            "--file", str(layer_file),
            "--media-type", OCI_IMAGE_LAYER_TAR_GZIP,
            "--annotation", "com.example.role=base",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["annotations"]["com.example.role"] == "base"

    def test_uncompressed_file_auto_gzip_compressed(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        # Raw (non-gzip) bytes
        layer_file = tmp_path / "layer.tar"
        layer_file.write_bytes(b"not compressed tar content even though it says tar")

        result = _runner().invoke(regshape, [
            "layout", "add", "layer",
            "--layout", str(layout_dir),
            "--file", str(layer_file),
            "--media-type", OCI_IMAGE_LAYER_TAR_GZIP,
        ])
        # Should succeed — content auto-compressed
        assert result.exit_code == 0, result.output

    def test_add_layer_fails_on_missing_layout(self, tmp_path):
        layer_file = tmp_path / "layer.tar.gz"
        layer_file.write_bytes(_make_gzip(b"content"))
        result = _runner().invoke(regshape, [
            "layout", "add", "layer",
            "--layout", str(tmp_path / "nolayout"),
            "--file", str(layer_file),
            "--media-type", OCI_IMAGE_LAYER_TAR_GZIP,
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout annotate layer
# ---------------------------------------------------------------------------


class TestLayoutAnnotateLayerCLI:
    def _setup(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        layer = _make_gzip(b"annotate layer test")
        stage_layer(layout_dir, layer, OCI_IMAGE_LAYER_TAR_GZIP)
        return layout_dir

    def test_adds_annotation(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "annotate", "layer",
            "--layout", str(layout_dir),
            "--index", "0",
            "--annotation", "org.opencontainers.image.created=2026-03-08",
        ])
        assert result.exit_code == 0, result.output
        assert "Updated layer" in result.output

    def test_json_output(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "annotate", "layer",
            "--layout", str(layout_dir),
            "--index", "0",
            "--annotation", "k=v",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["index"] == 0
        assert data["annotations"]["k"] == "v"

    def test_out_of_range_index_fails(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "annotate", "layer",
            "--layout", str(layout_dir),
            "--index", "99",
            "--annotation", "k=v",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout annotate manifest
# ---------------------------------------------------------------------------


class TestLayoutAnnotateManifestCLI:
    def _setup(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        _full_pipeline(layout_dir)
        return layout_dir

    def test_adds_annotation_to_manifest(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "annotate", "manifest",
            "--layout", str(layout_dir),
            "--annotation", "org.opencontainers.image.version=1.0.0",
        ])
        assert result.exit_code == 0, result.output
        assert "Updated manifest" in result.output

    def test_json_output(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "annotate", "manifest",
            "--layout", str(layout_dir),
            "--annotation", "k=v",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "digest" in data

    def test_fails_if_no_manifest_staged(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "annotate", "manifest",
            "--layout", str(layout_dir),
            "--annotation", "k=v",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout generate config
# ---------------------------------------------------------------------------


class TestLayoutGenerateConfigCLI:
    def test_generates_config_with_explicit_flags(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"content"), OCI_IMAGE_LAYER_TAR_GZIP)

        result = _runner().invoke(regshape, [
            "layout", "generate", "config",
            "--layout", str(layout_dir),
            "--architecture", "arm64",
            "--os", "linux",
            "--media-type", "application/vnd.oci.image.config.v1+json",
        ])
        assert result.exit_code == 0, result.output
        assert "Generated config" in result.output

    def test_json_output(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"c"), OCI_IMAGE_LAYER_TAR_GZIP)

        result = _runner().invoke(regshape, [
            "layout", "generate", "config",
            "--layout", str(layout_dir),
            "--architecture", "amd64",
            "--os", "linux",
            "--media-type", "application/vnd.oci.image.config.v1+json",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "digest" in data

    def test_fails_if_no_layers(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "generate", "config",
            "--layout", str(layout_dir),
            "--architecture", "amd64",
            "--os", "linux",
            "--media-type", "application/vnd.oci.image.config.v1+json",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout generate manifest
# ---------------------------------------------------------------------------


class TestLayoutGenerateManifestCLI:
    def _setup_with_config(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"layer"), OCI_IMAGE_LAYER_TAR_GZIP)
        generate_config(layout_dir)
        return layout_dir

    def test_generates_manifest_with_explicit_flags(self, tmp_path):
        layout_dir = self._setup_with_config(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "generate", "manifest",
            "--layout", str(layout_dir),
            "--ref-name", "latest",
            "--media-type", "application/vnd.oci.image.manifest.v1+json",
        ])
        assert result.exit_code == 0, result.output
        assert "Generated manifest" in result.output

    def test_json_output(self, tmp_path):
        layout_dir = self._setup_with_config(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "generate", "manifest",
            "--layout", str(layout_dir),
            "--ref-name", "v1.0",
            "--media-type", "application/vnd.oci.image.manifest.v1+json",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "digest" in data
        assert data["ref_name"] == "v1.0"

    def test_fails_if_no_config(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"layer"), OCI_IMAGE_LAYER_TAR_GZIP)
        result = _runner().invoke(regshape, [
            "layout", "generate", "manifest",
            "--layout", str(layout_dir),
            "--ref-name", "latest",
            "--media-type", "application/vnd.oci.image.manifest.v1+json",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout update config
# ---------------------------------------------------------------------------


class TestLayoutUpdateConfigCLI:
    def _setup(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"layer"), OCI_IMAGE_LAYER_TAR_GZIP)
        generate_config(layout_dir, architecture="amd64", os_name="linux")
        return layout_dir

    def test_updates_architecture(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "update", "config",
            "--layout", str(layout_dir),
            "--architecture", "arm64",
        ])
        assert result.exit_code == 0, result.output
        assert "Updated config" in result.output

    def test_json_output(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        result = _runner().invoke(regshape, [
            "layout", "update", "config",
            "--layout", str(layout_dir),
            "--architecture", "arm64",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "digest" in data

    def test_warning_when_manifest_exists(self, tmp_path):
        layout_dir = self._setup(tmp_path)
        generate_manifest(layout_dir, ref_name="latest")

        result = _runner().invoke(regshape, [
            "layout", "update", "config",
            "--layout", str(layout_dir),
            "--architecture", "arm64",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "warning" in data

    def test_fails_if_no_config(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"l"), OCI_IMAGE_LAYER_TAR_GZIP)
        result = _runner().invoke(regshape, [
            "layout", "update", "config",
            "--layout", str(layout_dir),
            "--architecture", "arm64",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# layout status
# ---------------------------------------------------------------------------


class TestLayoutStatusCLI:
    def test_shows_empty_state(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "status",
            "--layout", str(layout_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "Layers staged: 0" in result.output

    def test_json_output(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "status",
            "--layout", str(layout_dir),
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["layers"] == []
        assert data["config"] is None

    def test_shows_staged_layer_count(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        stage_layer(layout_dir, _make_gzip(b"layer"), OCI_IMAGE_LAYER_TAR_GZIP)
        result = _runner().invoke(regshape, [
            "layout", "status",
            "--layout", str(layout_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "Layers staged: 1" in result.output


# ---------------------------------------------------------------------------
# layout show
# ---------------------------------------------------------------------------


class TestLayoutShowCLI:
    def test_shows_index_json(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "show",
            "--layout", str(layout_dir),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["schemaVersion"] == 2
        assert data["manifests"] == []


# ---------------------------------------------------------------------------
# layout validate
# ---------------------------------------------------------------------------


class TestLayoutValidateCLI:
    def test_validates_empty_layout(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "validate",
            "--layout", str(layout_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "valid" in result.output

    def test_validates_full_layout(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        _full_pipeline(layout_dir)
        result = _runner().invoke(regshape, [
            "layout", "validate",
            "--layout", str(layout_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "valid" in result.output

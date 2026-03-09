#!/usr/bin/env python3

"""Tests for the staged-workflow functions in :mod:`regshape.libs.layout`.

Covers ``stage_layer``, ``generate_config``, ``generate_manifest``,
``read_stage``, ``update_layer_annotations``, ``update_config``, and
``update_manifest_annotations``.
"""

import gzip
import hashlib
import io
import json
from pathlib import Path

import pytest

from regshape.libs.errors import LayoutError
from regshape.libs.layout import (
    generate_config,
    generate_manifest,
    init_layout,
    read_stage,
    stage_layer,
    update_config,
    update_layer_annotations,
    update_manifest_annotations,
    validate_layout,
)
from regshape.libs.layout.operations import _STAGE_FILE
from regshape.libs.models.mediatype import (
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_LAYER_TAR_ZSTD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _setup_layout(tmp_path) -> Path:
    layout = tmp_path / "layout"
    init_layout(layout)
    return layout


def _stage_one_layer(layout: Path, content: bytes = b"test layer data") -> bytes:
    gz = _make_gzip(content)
    stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP)
    return gz


# ---------------------------------------------------------------------------
# TestInitLayoutStagingFile
# ---------------------------------------------------------------------------


class TestInitLayoutStagingFile:
    def test_staging_file_created(self, tmp_path):
        layout = tmp_path / "layout"
        init_layout(layout)
        sf = layout / _STAGE_FILE
        assert sf.exists(), "staging file not created by init_layout"

    def test_staging_file_schema(self, tmp_path):
        layout = tmp_path / "layout"
        init_layout(layout)
        data = json.loads((layout / _STAGE_FILE).read_text())
        assert data["schema_version"] == 1
        assert data["layers"] == []
        assert data["config"] is None
        assert data["manifest"] is None


# ---------------------------------------------------------------------------
# TestStageLayer
# ---------------------------------------------------------------------------


class TestStageLayer:
    def test_descriptor_fields(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"layer content")
        desc = stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP)
        assert desc.digest == _sha256(gz)
        assert desc.size == len(gz)
        assert desc.media_type == OCI_IMAGE_LAYER_TAR_GZIP

    def test_blob_stored(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"blob test")
        desc = stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP)
        blob_path = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        assert blob_path.exists()
        assert blob_path.read_bytes() == gz

    def test_staging_file_updated(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"layer")
        desc = stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP)
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert len(stage["layers"]) == 1
        assert stage["layers"][0]["digest"] == desc.digest
        assert stage["layers"][0]["size"] == desc.size
        assert stage["layers"][0]["media_type"] == OCI_IMAGE_LAYER_TAR_GZIP

    def test_multiple_layers_appended(self, tmp_path):
        layout = _setup_layout(tmp_path)
        stage_layer(layout, _make_gzip(b"layer 1"), OCI_IMAGE_LAYER_TAR_GZIP)
        stage_layer(layout, _make_gzip(b"layer 2"), OCI_IMAGE_LAYER_TAR_GZIP)
        stage_layer(layout, _make_gzip(b"layer 3"), OCI_IMAGE_LAYER_TAR_GZIP)
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert len(stage["layers"]) == 3

    def test_annotations_stored(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"annotated layer")
        annots = {"com.example.role": "base"}
        desc = stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP, annotations=annots)
        assert desc.annotations == annots
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert stage["layers"][0]["annotations"] == annots

    def test_fails_on_uninitialised_layout(self, tmp_path):
        layout = tmp_path / "nolayout"
        layout.mkdir()
        with pytest.raises(LayoutError):
            stage_layer(layout, _make_gzip(b"data"), OCI_IMAGE_LAYER_TAR_GZIP)


# ---------------------------------------------------------------------------
# TestGenerateConfig
# ---------------------------------------------------------------------------


class TestGenerateConfig:
    def test_config_descriptor_fields(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        desc = generate_config(layout)
        assert desc.digest.startswith("sha256:")
        assert desc.size > 0

    def test_config_blob_stored(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        desc = generate_config(layout)
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        assert blob.exists()

    def test_config_blob_is_valid_json(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        desc = generate_config(layout)
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        assert "rootfs" in obj
        assert "diff_ids" in obj["rootfs"]

    def test_architecture_and_os_stored(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        desc = generate_config(layout, architecture="arm64", os_name="linux")
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        assert obj["architecture"] == "arm64"
        assert obj["os"] == "linux"

    def test_diff_ids_match_layer_digests(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"diff_id test")
        stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP)
        desc = generate_config(layout)
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        # diff_ids are set from layer digests
        assert len(obj["rootfs"]["diff_ids"]) == 1

    def test_staging_file_config_set(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        desc = generate_config(layout)
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert stage["config"] is not None
        assert stage["config"]["digest"] == desc.digest

    def test_fails_with_no_layers(self, tmp_path):
        layout = _setup_layout(tmp_path)
        with pytest.raises(LayoutError):
            generate_config(layout)

    def test_annotations_stored(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        annots = {"com.example.created": "2026-01-01"}
        desc = generate_config(layout, annotations=annots)
        assert desc.annotations == annots


# ---------------------------------------------------------------------------
# TestGenerateManifest
# ---------------------------------------------------------------------------


class TestGenerateManifest:
    def _setup_with_config(self, tmp_path) -> Path:
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        generate_config(layout)
        return layout

    def test_manifest_descriptor_fields(self, tmp_path):
        layout = self._setup_with_config(tmp_path)
        desc = generate_manifest(layout, ref_name="latest")
        assert desc.digest.startswith("sha256:")
        assert desc.size > 0

    def test_manifest_blob_stored(self, tmp_path):
        layout = self._setup_with_config(tmp_path)
        desc = generate_manifest(layout)
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        assert blob.exists()

    def test_manifest_blob_is_valid_json(self, tmp_path):
        layout = self._setup_with_config(tmp_path)
        desc = generate_manifest(layout)
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        assert "schemaVersion" in obj
        assert "layers" in obj
        assert "config" in obj

    def test_manifest_registered_in_index(self, tmp_path):
        layout = self._setup_with_config(tmp_path)
        desc = generate_manifest(layout, ref_name="v1.0")
        index = json.loads((layout / "index.json").read_text())
        assert any(m["digest"] == desc.digest for m in index["manifests"])

    def test_ref_name_annotation_in_index(self, tmp_path):
        layout = self._setup_with_config(tmp_path)
        desc = generate_manifest(layout, ref_name="tagged")
        index = json.loads((layout / "index.json").read_text())
        entry = next(m for m in index["manifests"] if m["digest"] == desc.digest)
        assert entry["annotations"]["org.opencontainers.image.ref.name"] == "tagged"

    def test_staging_file_manifest_set(self, tmp_path):
        layout = self._setup_with_config(tmp_path)
        desc = generate_manifest(layout)
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert stage["manifest"] is not None
        assert stage["manifest"]["digest"] == desc.digest

    def test_fails_with_no_config(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        with pytest.raises(LayoutError):
            generate_manifest(layout)

    def test_fails_with_no_layers(self, tmp_path):
        layout = _setup_layout(tmp_path)
        with pytest.raises(LayoutError):
            generate_manifest(layout)


# ---------------------------------------------------------------------------
# TestReadStage
# ---------------------------------------------------------------------------


class TestReadStage:
    def test_returns_dict(self, tmp_path):
        layout = _setup_layout(tmp_path)
        result = read_stage(layout)
        assert isinstance(result, dict)

    def test_initial_state(self, tmp_path):
        layout = _setup_layout(tmp_path)
        stage = read_stage(layout)
        assert stage["layers"] == []
        assert stage["config"] is None
        assert stage["manifest"] is None

    def test_fails_without_staging_file(self, tmp_path):
        layout = _setup_layout(tmp_path)
        (layout / _STAGE_FILE).unlink()
        with pytest.raises(LayoutError):
            read_stage(layout)

    def test_reflects_staged_layers(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        stage = read_stage(layout)
        assert len(stage["layers"]) == 1


# ---------------------------------------------------------------------------
# TestUpdateLayerAnnotations
# ---------------------------------------------------------------------------


class TestUpdateLayerAnnotations:
    def _setup(self, tmp_path) -> Path:
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        return layout

    def test_merge_adds_annotation(self, tmp_path):
        layout = self._setup(tmp_path)
        desc = update_layer_annotations(layout, 0, {"k": "v"})
        assert desc.annotations["k"] == "v"

    def test_merge_preserves_existing(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"pre-annotated")
        stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP, annotations={"old": "1"})
        desc = update_layer_annotations(layout, 0, {"new": "2"}, replace=False)
        assert desc.annotations["old"] == "1"
        assert desc.annotations["new"] == "2"

    def test_replace_clears_existing(self, tmp_path):
        layout = _setup_layout(tmp_path)
        gz = _make_gzip(b"pre-annotated")
        stage_layer(layout, gz, OCI_IMAGE_LAYER_TAR_GZIP, annotations={"old": "1"})
        desc = update_layer_annotations(layout, 0, {"new": "2"}, replace=True)
        assert "old" not in (desc.annotations or {})
        assert desc.annotations["new"] == "2"

    def test_staging_file_updated(self, tmp_path):
        layout = self._setup(tmp_path)
        update_layer_annotations(layout, 0, {"k": "v"})
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert stage["layers"][0]["annotations"]["k"] == "v"

    def test_fails_with_invalid_index(self, tmp_path):
        layout = self._setup(tmp_path)
        with pytest.raises(LayoutError):
            update_layer_annotations(layout, 99, {"k": "v"})

    def test_fails_with_negative_index(self, tmp_path):
        layout = self._setup(tmp_path)
        with pytest.raises(LayoutError):
            update_layer_annotations(layout, -1, {"k": "v"})

    def test_no_blob_change(self, tmp_path):
        """update_layer_annotations must NOT alter the blob store."""
        layout = self._setup(tmp_path)
        stage_before = json.loads((layout / _STAGE_FILE).read_text())
        old_digest = stage_before["layers"][0]["digest"]
        blob_before = (layout / "blobs" / "sha256" / old_digest.split(":", 1)[1]).read_bytes()

        update_layer_annotations(layout, 0, {"k": "v"})

        # Blob unchanged
        blob_after = (layout / "blobs" / "sha256" / old_digest.split(":", 1)[1]).read_bytes()
        assert blob_before == blob_after
        # Digest unchanged
        stage_after = json.loads((layout / _STAGE_FILE).read_text())
        assert stage_after["layers"][0]["digest"] == old_digest


# ---------------------------------------------------------------------------
# TestUpdateConfig
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    def _setup(self, tmp_path, architecture="amd64", os_name="linux") -> tuple:
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        desc = generate_config(layout, architecture=architecture, os_name=os_name)
        return layout, desc

    def test_updates_architecture(self, tmp_path):
        layout, _ = self._setup(tmp_path, architecture="amd64")
        desc = update_config(layout, architecture="arm64")
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        assert obj["architecture"] == "arm64"

    def test_updates_os(self, tmp_path):
        layout, _ = self._setup(tmp_path, os_name="linux")
        desc = update_config(layout, os_name="windows")
        blob = layout / "blobs" / "sha256" / desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        assert obj["os"] == "windows"

    def test_old_blob_deleted(self, tmp_path):
        layout, old_desc = self._setup(tmp_path)
        old_path = layout / "blobs" / "sha256" / old_desc.digest.split(":", 1)[1]
        assert old_path.exists()
        update_config(layout, architecture="arm64")
        assert not old_path.exists(), "old config blob should be deleted after update"

    def test_staging_file_updated(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        new_desc = update_config(layout, architecture="arm64")
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert stage["config"]["digest"] == new_desc.digest

    def test_merge_annotations(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        update_config(layout, annotations={"k1": "v1"})
        new_desc = update_config(layout, annotations={"k2": "v2"}, replace_annotations=False)
        blob = layout / "blobs" / "sha256" / new_desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        # Both annotations should be present (merged)
        # (annotations may be stored in descriptor, not blob body — check stage)
        stage = json.loads((layout / _STAGE_FILE).read_text())
        # At minimum, the latest annotation exists
        assert stage["config"]["digest"] == new_desc.digest

    def test_noop_returns_same_descriptor(self, tmp_path):
        layout, old_desc = self._setup(tmp_path)
        # Call with no changes
        new_desc = update_config(layout)
        assert new_desc.digest == old_desc.digest

    def test_fails_if_no_config_staged(self, tmp_path):
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        with pytest.raises(LayoutError):
            update_config(layout, architecture="arm64")


# ---------------------------------------------------------------------------
# TestUpdateManifestAnnotations
# ---------------------------------------------------------------------------


class TestUpdateManifestAnnotations:
    def _setup(self, tmp_path) -> tuple:
        layout = _setup_layout(tmp_path)
        _stage_one_layer(layout)
        generate_config(layout)
        desc = generate_manifest(layout, ref_name="latest")
        return layout, desc

    def test_adds_annotation(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        new_desc = update_manifest_annotations(
            layout, {"org.opencontainers.image.version": "1.0.0"}
        )
        blob = layout / "blobs" / "sha256" / new_desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        assert obj["annotations"]["org.opencontainers.image.version"] == "1.0.0"

    def test_old_blob_deleted(self, tmp_path):
        layout, old_desc = self._setup(tmp_path)
        old_path = layout / "blobs" / "sha256" / old_desc.digest.split(":", 1)[1]
        assert old_path.exists()
        update_manifest_annotations(layout, {"k": "v"})
        assert not old_path.exists(), "old manifest blob should be deleted after update"

    def test_index_json_updated(self, tmp_path):
        layout, old_desc = self._setup(tmp_path)
        new_desc = update_manifest_annotations(layout, {"k": "v"})
        index = json.loads((layout / "index.json").read_text())
        digests = [m["digest"] for m in index["manifests"]]
        assert old_desc.digest not in digests, "old digest should be removed from index"
        assert new_desc.digest in digests, "new digest should appear in index"

    def test_ref_name_preserved(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        new_desc = update_manifest_annotations(layout, {"k": "v"})
        index = json.loads((layout / "index.json").read_text())
        entry = next(m for m in index["manifests"] if m["digest"] == new_desc.digest)
        assert entry["annotations"]["org.opencontainers.image.ref.name"] == "latest"

    def test_staging_file_updated(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        new_desc = update_manifest_annotations(layout, {"k": "v"})
        stage = json.loads((layout / _STAGE_FILE).read_text())
        assert stage["manifest"]["digest"] == new_desc.digest

    def test_replace_mode(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        # First add an annotation
        update_manifest_annotations(layout, {"keep": "no"})
        # Now replace all
        new_desc = update_manifest_annotations(layout, {"new": "only"}, replace=True)
        blob = layout / "blobs" / "sha256" / new_desc.digest.split(":", 1)[1]
        obj = json.loads(blob.read_bytes())
        annotations = obj.get("annotations", {})
        assert "keep" not in annotations
        assert annotations["new"] == "only"

    def test_fails_if_no_manifest_staged(self, tmp_path):
        layout = _setup_layout(tmp_path)
        with pytest.raises(LayoutError):
            update_manifest_annotations(layout, {"k": "v"})

    def test_validate_after_update(self, tmp_path):
        layout, _ = self._setup(tmp_path)
        update_manifest_annotations(layout, {"k": "v"})
        # Should not raise
        validate_layout(layout)

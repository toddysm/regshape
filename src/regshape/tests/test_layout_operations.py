#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.layout.operations`."""

import hashlib
import json
import os

import pytest

from regshape.libs.errors import LayoutError
from regshape.libs.layout.operations import (
    add_blob,
    add_manifest,
    init_layout,
    read_blob,
    read_index,
    validate_layout,
)
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.models.manifest import ImageIndex, ImageManifest
from regshape.libs.models.mediatype import (
    OCI_EMPTY,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _make_minimal_manifest(config_desc: Descriptor, layer_desc: Descriptor) -> bytes:
    """Build a minimal OCI image manifest JSON bytes."""
    manifest = ImageManifest(
        schema_version=2,
        media_type=OCI_IMAGE_MANIFEST,
        config=config_desc,
        layers=[layer_desc],
    )
    return manifest.to_json().encode("utf-8")


# ---------------------------------------------------------------------------
# init_layout
# ---------------------------------------------------------------------------

class TestInitLayout:
    def test_creates_oci_layout_marker(self, tmp_path):
        init_layout(tmp_path / "layout")
        marker = tmp_path / "layout" / "oci-layout"
        assert marker.exists()
        data = json.loads(marker.read_text())
        assert data == {"imageLayoutVersion": "1.0.0"}

    def test_creates_index_json(self, tmp_path):
        init_layout(tmp_path / "layout")
        index_file = tmp_path / "layout" / "index.json"
        assert index_file.exists()
        data = json.loads(index_file.read_text())
        assert data["schemaVersion"] == 2
        assert data["mediaType"] == OCI_IMAGE_INDEX
        assert data["manifests"] == []

    def test_creates_blobs_sha256_dir(self, tmp_path):
        init_layout(tmp_path / "layout")
        assert (tmp_path / "layout" / "blobs" / "sha256").is_dir()

    def test_creates_missing_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "layout"
        init_layout(target)
        assert (target / "oci-layout").exists()

    def test_raises_if_already_initialised(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        with pytest.raises(LayoutError, match="already an OCI Image Layout"):
            init_layout(layout_dir)


# ---------------------------------------------------------------------------
# add_blob
# ---------------------------------------------------------------------------

class TestAddBlob:
    def test_stores_content_at_correct_path(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        content = b"hello world"
        digest, size = add_blob(layout_dir, content)

        hex_ = hashlib.sha256(content).hexdigest()
        blob_path = layout_dir / "blobs" / "sha256" / hex_
        assert blob_path.exists()
        assert blob_path.read_bytes() == content

    def test_returns_correct_digest_and_size(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        content = b"test blob content"
        digest, size = add_blob(layout_dir, content)

        assert digest == _sha256(content)
        assert size == len(content)

    def test_idempotent_for_same_content(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        content = b"repeated content"

        d1, s1 = add_blob(layout_dir, content)
        d2, s2 = add_blob(layout_dir, content)

        assert d1 == d2
        assert s1 == s2

    def test_raises_if_not_a_layout(self, tmp_path):
        non_layout = tmp_path / "not-a-layout"
        non_layout.mkdir()
        with pytest.raises(LayoutError, match="not an OCI Image Layout"):
            add_blob(non_layout, b"data")

    def test_raises_on_uninitialised_path(self, tmp_path):
        with pytest.raises(LayoutError):
            add_blob(tmp_path / "nonexistent", b"data")


# ---------------------------------------------------------------------------
# add_manifest
# ---------------------------------------------------------------------------

class TestAddManifest:
    def _setup_layout_with_blobs(self, tmp_path):
        """Return a layout dir with one config blob and one layer blob."""
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)

        config_content = b"{}"
        layer_content = b"fake layer bytes"

        config_digest, config_size = add_blob(layout_dir, config_content)
        layer_digest, layer_size = add_blob(layout_dir, layer_content)

        config_desc = Descriptor(
            media_type=OCI_EMPTY,
            digest=config_digest,
            size=config_size,
        )
        layer_desc = Descriptor(
            media_type=OCI_IMAGE_LAYER_TAR_GZIP,
            digest=layer_digest,
            size=layer_size,
        )
        return layout_dir, config_desc, layer_desc

    def test_writes_manifest_blob(self, tmp_path):
        layout_dir, config_desc, layer_desc = self._setup_layout_with_blobs(tmp_path)
        manifest_bytes = _make_minimal_manifest(config_desc, layer_desc)

        descriptor = add_manifest(layout_dir, manifest_bytes, OCI_IMAGE_MANIFEST)

        expected_hex = hashlib.sha256(manifest_bytes).hexdigest()
        blob_path = layout_dir / "blobs" / "sha256" / expected_hex
        assert blob_path.exists()
        assert blob_path.read_bytes() == manifest_bytes

    def test_updates_index_json(self, tmp_path):
        layout_dir, config_desc, layer_desc = self._setup_layout_with_blobs(tmp_path)
        manifest_bytes = _make_minimal_manifest(config_desc, layer_desc)

        descriptor = add_manifest(layout_dir, manifest_bytes, OCI_IMAGE_MANIFEST)

        index = read_index(layout_dir)
        assert len(index.manifests) == 1
        assert index.manifests[0].digest == descriptor.digest
        assert index.manifests[0].media_type == OCI_IMAGE_MANIFEST

    def test_with_ref_name_adds_annotation(self, tmp_path):
        layout_dir, config_desc, layer_desc = self._setup_layout_with_blobs(tmp_path)
        manifest_bytes = _make_minimal_manifest(config_desc, layer_desc)

        descriptor = add_manifest(
            layout_dir, manifest_bytes, OCI_IMAGE_MANIFEST, ref_name="latest"
        )

        assert descriptor.annotations is not None
        assert descriptor.annotations.get("org.opencontainers.image.ref.name") == "latest"

        index = read_index(layout_dir)
        assert index.manifests[0].annotations == {
            "org.opencontainers.image.ref.name": "latest"
        }

    def test_with_extra_annotations(self, tmp_path):
        layout_dir, config_desc, layer_desc = self._setup_layout_with_blobs(tmp_path)
        manifest_bytes = _make_minimal_manifest(config_desc, layer_desc)

        descriptor = add_manifest(
            layout_dir,
            manifest_bytes,
            OCI_IMAGE_MANIFEST,
            ref_name="v1.0",
            annotations={"org.example.key": "value"},
        )

        assert descriptor.annotations["org.opencontainers.image.ref.name"] == "v1.0"
        assert descriptor.annotations["org.example.key"] == "value"

    def test_multiple_manifests_accumulate_in_index(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)

        manifest1 = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.empty.v1+json","digest":"sha256:' + hashlib.sha256(b"c1").hexdigest().encode() + b'","size":2},"layers":[]}'
        manifest2 = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.empty.v1+json","digest":"sha256:' + hashlib.sha256(b"c2").hexdigest().encode() + b'","size":2},"layers":[]}'

        add_blob(layout_dir, b"c1")
        add_blob(layout_dir, b"c2")
        add_manifest(layout_dir, manifest1, OCI_IMAGE_MANIFEST, ref_name="v1")
        add_manifest(layout_dir, manifest2, OCI_IMAGE_MANIFEST, ref_name="v2")

        index = read_index(layout_dir)
        assert len(index.manifests) == 2

    def test_raises_if_not_a_layout(self, tmp_path):
        non_layout = tmp_path / "not-a-layout"
        non_layout.mkdir()
        with pytest.raises(LayoutError):
            add_manifest(non_layout, b"{}", OCI_IMAGE_MANIFEST)


# ---------------------------------------------------------------------------
# read_index
# ---------------------------------------------------------------------------

class TestReadIndex:
    def test_returns_image_index_after_init(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        index = read_index(layout_dir)
        assert isinstance(index, ImageIndex)
        assert index.schema_version == 2
        assert index.media_type == OCI_IMAGE_INDEX
        assert index.manifests == []

    def test_reflects_added_manifests(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)

        # Add a dummy manifest blob directly
        content = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.empty.v1+json","digest":"sha256:' + hashlib.sha256(b"x").hexdigest().encode() + b'","size":1},"layers":[]}'
        add_blob(layout_dir, b"x")
        add_manifest(layout_dir, content, OCI_IMAGE_MANIFEST, ref_name="test")

        index = read_index(layout_dir)
        assert len(index.manifests) == 1
        assert index.manifests[0].annotations["org.opencontainers.image.ref.name"] == "test"

    def test_raises_if_not_a_layout(self, tmp_path):
        with pytest.raises(LayoutError, match="not an OCI Image Layout"):
            read_index(tmp_path / "nonexistent")

    def test_raises_on_malformed_index(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        # Corrupt index.json
        (layout_dir / "index.json").write_text("this is not json", encoding="utf-8")
        with pytest.raises(LayoutError):
            read_index(layout_dir)


# ---------------------------------------------------------------------------
# read_blob
# ---------------------------------------------------------------------------

class TestReadBlob:
    def test_returns_stored_content(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        content = b"some blob data"
        digest, _ = add_blob(layout_dir, content)
        result = read_blob(layout_dir, digest)
        assert result == content

    def test_raises_if_blob_not_found(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        fake_digest = "sha256:" + "a" * 64
        with pytest.raises(LayoutError, match="not found"):
            read_blob(layout_dir, fake_digest)

    def test_raises_on_digest_mismatch(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        content = b"original"
        digest, _ = add_blob(layout_dir, content)

        # Corrupt the blob on disk
        hex_ = digest.split(":")[1]
        blob_path = layout_dir / "blobs" / "sha256" / hex_
        blob_path.write_bytes(b"corrupted")

        with pytest.raises(LayoutError, match="[Dd]igest mismatch"):
            read_blob(layout_dir, digest)

    def test_raises_on_invalid_digest_format(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        with pytest.raises(LayoutError, match="[Ii]nvalid digest"):
            read_blob(layout_dir, "notadigest")

    def test_raises_on_unsupported_algorithm(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        with pytest.raises(LayoutError, match="[Uu]nsupported"):
            read_blob(layout_dir, "md5:" + "a" * 32)


# ---------------------------------------------------------------------------
# validate_layout
# ---------------------------------------------------------------------------

class TestValidateLayout:
    def _build_valid_layout(self, tmp_path):
        """Return a layout dir containing one manifest with its blobs."""
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)

        config_content = b"{}"
        layer_content = b"fake layer"

        config_digest, config_size = add_blob(layout_dir, config_content)
        layer_digest, layer_size = add_blob(layout_dir, layer_content)

        config_desc = Descriptor(
            media_type=OCI_EMPTY,
            digest=config_digest,
            size=config_size,
        )
        layer_desc = Descriptor(
            media_type=OCI_IMAGE_LAYER_TAR_GZIP,
            digest=layer_digest,
            size=layer_size,
        )
        manifest_bytes = _make_minimal_manifest(config_desc, layer_desc)
        add_manifest(layout_dir, manifest_bytes, OCI_IMAGE_MANIFEST, ref_name="latest")

        return layout_dir

    def test_passes_for_valid_layout(self, tmp_path):
        layout_dir = self._build_valid_layout(tmp_path)
        # Should not raise
        validate_layout(layout_dir)

    def test_passes_for_empty_layout(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        validate_layout(layout_dir)

    def test_raises_if_oci_layout_missing(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        (layout_dir / "oci-layout").unlink()
        with pytest.raises(LayoutError, match="not an OCI Image Layout"):
            validate_layout(layout_dir)

    def test_raises_if_oci_layout_wrong_version(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        (layout_dir / "oci-layout").write_text(
            json.dumps({"imageLayoutVersion": "2.0.0"}), encoding="utf-8"
        )
        with pytest.raises(LayoutError, match="[Ii]nvalid imageLayoutVersion"):
            validate_layout(layout_dir)

    def test_raises_if_manifest_blob_missing(self, tmp_path):
        layout_dir = self._build_valid_layout(tmp_path)

        # Remove one manifest blob
        index = read_index(layout_dir)
        manifest_hex = index.manifests[0].digest.split(":")[1]
        blob_path = layout_dir / "blobs" / "sha256" / manifest_hex
        blob_path.unlink()

        with pytest.raises(LayoutError, match="does not exist"):
            validate_layout(layout_dir)

    def test_raises_if_layer_blob_missing(self, tmp_path):
        layout_dir = self._build_valid_layout(tmp_path)

        # Find the layer blob and remove it
        index = read_index(layout_dir)
        manifest_bytes = read_blob(layout_dir, index.manifests[0].digest)
        manifest_obj = json.loads(manifest_bytes)
        layer_hex = manifest_obj["layers"][0]["digest"].split(":")[1]
        (layout_dir / "blobs" / "sha256" / layer_hex).unlink()

        with pytest.raises(LayoutError, match="does not exist"):
            validate_layout(layout_dir)

    def test_raises_on_manifest_blob_digest_mismatch(self, tmp_path):
        layout_dir = self._build_valid_layout(tmp_path)

        index = read_index(layout_dir)
        manifest_hex = index.manifests[0].digest.split(":")[1]
        blob_path = layout_dir / "blobs" / "sha256" / manifest_hex
        blob_path.write_bytes(b"tampered content")

        with pytest.raises(LayoutError, match="[Dd]igest mismatch"):
            validate_layout(layout_dir)

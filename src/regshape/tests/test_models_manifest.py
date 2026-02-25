#!/usr/bin/env python3

"""
Tests for :mod:`regshape.libs.models.descriptor`,
:mod:`regshape.libs.models.manifest`, and
:mod:`regshape.libs.models.mediatype`.
"""

import hashlib
import json

import pytest

from regshape.libs.errors import ManifestError
from regshape.libs.models.descriptor import Descriptor, Platform
from regshape.libs.models.manifest import ImageIndex, ImageManifest, parse_manifest
from regshape.libs.models.mediatype import (
    DOCKER_MANIFEST_LIST_V2,
    DOCKER_MANIFEST_V2,
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_VALID_DIGEST = "sha256:" + "a" * 64
_VALID_DIGEST_2 = "sha256:" + "b" * 64
_VALID_SHA512 = "sha512:" + "c" * 128


def _make_descriptor(**kwargs) -> Descriptor:
    defaults = dict(media_type=OCI_IMAGE_LAYER_TAR_GZIP, digest=_VALID_DIGEST, size=1024)
    defaults.update(kwargs)
    return Descriptor(**defaults)


def _make_manifest(**kwargs) -> ImageManifest:
    defaults = dict(
        schema_version=2,
        media_type=OCI_IMAGE_MANIFEST,
        config=_make_descriptor(media_type=OCI_IMAGE_CONFIG, digest=_VALID_DIGEST_2, size=2),
        layers=[_make_descriptor()],
    )
    defaults.update(kwargs)
    return ImageManifest(**defaults)


def _make_index(**kwargs) -> ImageIndex:
    defaults = dict(
        schema_version=2,
        media_type=OCI_IMAGE_INDEX,
        manifests=[
            _make_descriptor(
                media_type=OCI_IMAGE_MANIFEST,
                platform=Platform(architecture="amd64", os="linux"),
            )
        ],
    )
    defaults.update(kwargs)
    return ImageIndex(**defaults)


# ---------------------------------------------------------------------------
# Platform tests
# ---------------------------------------------------------------------------

class TestPlatform:
    def test_basic_construction(self):
        p = Platform(architecture="amd64", os="linux")
        assert p.architecture == "amd64"
        assert p.os == "linux"
        assert p.variant is None

    def test_full_construction(self):
        p = Platform(
            architecture="arm",
            os="linux",
            os_version="5.10",
            os_features=["feature1"],
            variant="v7",
        )
        assert p.os_version == "5.10"
        assert p.variant == "v7"

    def test_empty_architecture_raises(self):
        with pytest.raises(ValueError, match="architecture"):
            Platform(architecture="", os="linux")

    def test_empty_os_raises(self):
        with pytest.raises(ValueError, match="os"):
            Platform(architecture="amd64", os="")

    def test_to_dict_minimal(self):
        d = Platform(architecture="amd64", os="linux").to_dict()
        assert d == {"architecture": "amd64", "os": "linux"}
        assert "os.version" not in d
        assert "os.features" not in d

    def test_to_dict_with_optional(self):
        d = Platform(
            architecture="arm64", os="linux", variant="v8", os_version="5.15"
        ).to_dict()
        assert d["variant"] == "v8"
        assert d["os.version"] == "5.15"

    def test_from_dict_roundtrip(self):
        original = Platform(
            architecture="arm64",
            os="linux",
            os_version="5.15",
            os_features=["feat"],
            variant="v8",
        )
        restored = Platform.from_dict(original.to_dict())
        assert restored == original

    def test_from_dict_missing_required_raises(self):
        with pytest.raises(ValueError, match="architecture"):
            Platform.from_dict({"os": "linux"})


# ---------------------------------------------------------------------------
# Descriptor tests
# ---------------------------------------------------------------------------

class TestDescriptor:
    def test_basic_construction(self):
        d = _make_descriptor()
        assert d.media_type == OCI_IMAGE_LAYER_TAR_GZIP
        assert d.size == 1024

    def test_sha512_digest_accepted(self):
        d = _make_descriptor(digest=_VALID_SHA512)
        assert d.digest == _VALID_SHA512

    def test_empty_media_type_raises(self):
        with pytest.raises(ValueError, match="media_type"):
            _make_descriptor(media_type="")

    def test_invalid_digest_format_raises(self):
        with pytest.raises(ValueError, match="digest"):
            _make_descriptor(digest="md5:abc")

    def test_negative_size_raises(self):
        with pytest.raises(ValueError, match="size"):
            _make_descriptor(size=-1)

    def test_zero_size_allowed(self):
        d = _make_descriptor(size=0)
        assert d.size == 0

    def test_to_dict_minimal(self):
        d = _make_descriptor()
        wire = d.to_dict()
        assert wire == {
            "mediaType": OCI_IMAGE_LAYER_TAR_GZIP,
            "digest": _VALID_DIGEST,
            "size": 1024,
        }
        assert "platform" not in wire
        assert "annotations" not in wire

    def test_to_dict_with_platform(self):
        p = Platform(architecture="amd64", os="linux")
        d = _make_descriptor(platform=p)
        wire = d.to_dict()
        assert "platform" in wire
        assert wire["platform"]["architecture"] == "amd64"

    def test_to_dict_with_annotations(self):
        d = _make_descriptor(annotations={"key": "val"})
        assert d.to_dict()["annotations"] == {"key": "val"}

    def test_from_dict_roundtrip(self):
        original = _make_descriptor(
            platform=Platform(architecture="arm64", os="linux", variant="v8"),
            annotations={"a": "b"},
            artifact_type="application/vnd.example",
            urls=["https://example.com/blob"],
        )
        restored = Descriptor.from_dict(original.to_dict())
        assert restored == original

    def test_from_dict_missing_required_raises(self):
        with pytest.raises(ValueError, match="mediaType"):
            Descriptor.from_dict({"digest": _VALID_DIGEST, "size": 10})


# ---------------------------------------------------------------------------
# ImageManifest tests
# ---------------------------------------------------------------------------

class TestImageManifest:
    def test_basic_construction(self):
        m = _make_manifest()
        assert m.schema_version == 2
        assert m.media_type == OCI_IMAGE_MANIFEST

    def test_schema_version_not_2_raises(self):
        with pytest.raises(ValueError, match="schema_version"):
            _make_manifest(schema_version=1)

    def test_empty_media_type_raises(self):
        with pytest.raises(ValueError, match="media_type"):
            _make_manifest(media_type="")

    def test_layers_not_list_raises(self):
        with pytest.raises(ValueError, match="layers"):
            _make_manifest(layers=None)

    def test_empty_layers_allowed(self):
        m = _make_manifest(layers=[])
        assert m.layers == []

    def test_to_json_is_valid_json(self):
        m = _make_manifest()
        parsed = json.loads(m.to_json())
        assert parsed["schemaVersion"] == 2

    def test_to_json_canonical_no_whitespace(self):
        m = _make_manifest()
        raw = m.to_json()
        assert " " not in raw

    def test_to_json_deterministic(self):
        m = _make_manifest()
        assert m.to_json() == m.to_json()

    def test_digest_format(self):
        m = _make_manifest()
        d = m.digest()
        assert d.startswith("sha256:")
        assert len(d) == len("sha256:") + 64

    def test_digest_matches_sha256_of_json(self):
        m = _make_manifest()
        expected = "sha256:" + hashlib.sha256(m.to_json().encode("utf-8")).hexdigest()
        assert m.digest() == expected

    def test_from_json_roundtrip(self):
        original = _make_manifest(
            subject=_make_descriptor(media_type=OCI_IMAGE_MANIFEST, digest=_VALID_DIGEST_2, size=999),
            annotations={"created": "2026-02-25"},
            artifact_type="application/vnd.example.sbom",
        )
        restored = ImageManifest.from_json(original.to_json())
        assert restored.schema_version == original.schema_version
        assert restored.media_type == original.media_type
        assert restored.config == original.config
        assert restored.layers == original.layers
        assert restored.subject == original.subject
        assert restored.annotations == original.annotations
        assert restored.artifact_type == original.artifact_type

    def test_from_json_malformed_raises(self):
        with pytest.raises(ManifestError, match="parse"):
            ImageManifest.from_json("{not valid json")

    def test_from_json_missing_required_raises(self):
        bad = json.dumps({"schemaVersion": 2, "mediaType": OCI_IMAGE_MANIFEST})
        with pytest.raises(ManifestError):
            ImageManifest.from_json(bad)

    def test_docker_v2_media_type_accepted(self):
        m = _make_manifest(media_type=DOCKER_MANIFEST_V2)
        assert m.media_type == DOCKER_MANIFEST_V2


# ---------------------------------------------------------------------------
# ImageIndex tests
# ---------------------------------------------------------------------------

class TestImageIndex:
    def test_basic_construction(self):
        idx = _make_index()
        assert idx.schema_version == 2
        assert idx.media_type == OCI_IMAGE_INDEX

    def test_schema_version_not_2_raises(self):
        with pytest.raises(ValueError, match="schema_version"):
            _make_index(schema_version=1)

    def test_manifests_not_list_raises(self):
        with pytest.raises(ValueError, match="manifests"):
            _make_index(manifests=None)

    def test_empty_manifests_allowed(self):
        idx = _make_index(manifests=[])
        assert idx.manifests == []

    def test_to_json_is_valid_json(self):
        idx = _make_index()
        parsed = json.loads(idx.to_json())
        assert parsed["schemaVersion"] == 2

    def test_digest_format(self):
        idx = _make_index()
        d = idx.digest()
        assert d.startswith("sha256:")
        assert len(d) == len("sha256:") + 64

    def test_from_json_roundtrip(self):
        original = _make_index(
            manifests=[
                _make_descriptor(
                    media_type=OCI_IMAGE_MANIFEST,
                    platform=Platform(architecture="amd64", os="linux"),
                ),
                _make_descriptor(
                    media_type=OCI_IMAGE_MANIFEST,
                    digest=_VALID_DIGEST_2,
                    platform=Platform(architecture="arm64", os="linux", variant="v8"),
                ),
            ],
            annotations={"created": "2026-02-25"},
        )
        restored = ImageIndex.from_json(original.to_json())
        assert restored.schema_version == original.schema_version
        assert restored.media_type == original.media_type
        assert len(restored.manifests) == 2
        assert restored.manifests[0].platform == original.manifests[0].platform
        assert restored.annotations == original.annotations

    def test_from_json_malformed_raises(self):
        with pytest.raises(ManifestError, match="parse"):
            ImageIndex.from_json("{bad json")

    def test_subject_roundtrip(self):
        original = _make_index(
            subject=_make_descriptor(media_type=OCI_IMAGE_MANIFEST, digest=_VALID_DIGEST_2, size=42)
        )
        restored = ImageIndex.from_json(original.to_json())
        assert restored.subject == original.subject

    def test_docker_manifest_list_media_type_accepted(self):
        idx = _make_index(media_type=DOCKER_MANIFEST_LIST_V2)
        assert idx.media_type == DOCKER_MANIFEST_LIST_V2


# ---------------------------------------------------------------------------
# parse_manifest factory tests
# ---------------------------------------------------------------------------

class TestParseManifest:
    def test_dispatches_to_image_manifest(self):
        m = _make_manifest()
        result = parse_manifest(m.to_json())
        assert isinstance(result, ImageManifest)

    def test_dispatches_to_docker_v2_manifest(self):
        m = _make_manifest(media_type=DOCKER_MANIFEST_V2)
        result = parse_manifest(m.to_json())
        assert isinstance(result, ImageManifest)

    def test_dispatches_to_image_index(self):
        idx = _make_index()
        result = parse_manifest(idx.to_json())
        assert isinstance(result, ImageIndex)

    def test_dispatches_to_docker_manifest_list(self):
        idx = _make_index(media_type=DOCKER_MANIFEST_LIST_V2)
        result = parse_manifest(idx.to_json())
        assert isinstance(result, ImageIndex)

    def test_unknown_media_type_raises(self):
        bad = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.unknown.type",
            "manifests": [],
        })
        with pytest.raises(ManifestError, match="Unknown manifest mediaType"):
            parse_manifest(bad)

    def test_malformed_json_raises(self):
        with pytest.raises(ManifestError, match="parse"):
            parse_manifest("not json at all")

#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.models.referrer`."""

import json

import pytest

from regshape.libs.errors import ReferrerError
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.models.referrer import ReferrerList


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIGEST_1 = "sha256:" + "a" * 64
DIGEST_2 = "sha256:" + "b" * 64
SBOM_TYPE = "application/vnd.example.sbom.v1"
SIG_TYPE = "application/vnd.cncf.notary.signature"
MANIFEST_MT = "application/vnd.oci.image.manifest.v1+json"
INDEX_MT = "application/vnd.oci.image.index.v1+json"


def _descriptor_dict(digest: str = DIGEST_1, artifact_type: str = SBOM_TYPE, size: int = 1234) -> dict:
    return {
        "mediaType": MANIFEST_MT,
        "digest": digest,
        "size": size,
        "artifactType": artifact_type,
    }


def _full_response(manifests: list[dict] | None = None) -> dict:
    d: dict = {
        "schemaVersion": 2,
        "mediaType": INDEX_MT,
    }
    if manifests is not None:
        d["manifests"] = manifests
    return d


# ---------------------------------------------------------------------------
# ReferrerList.from_dict
# ---------------------------------------------------------------------------

class TestReferrerListFromDict:

    def test_happy_path(self):
        data = _full_response([_descriptor_dict(DIGEST_1), _descriptor_dict(DIGEST_2, SIG_TYPE)])
        rl = ReferrerList.from_dict(data)
        assert len(rl.manifests) == 2
        assert rl.manifests[0].digest == DIGEST_1
        assert rl.manifests[1].digest == DIGEST_2

    def test_empty_manifests(self):
        data = _full_response([])
        rl = ReferrerList.from_dict(data)
        assert rl.manifests == []

    def test_null_manifests_normalised_to_empty_list(self):
        data = _full_response()
        data["manifests"] = None
        rl = ReferrerList.from_dict(data)
        assert rl.manifests == []

    def test_missing_manifests_key_normalised_to_empty_list(self):
        data = {"schemaVersion": 2, "mediaType": INDEX_MT}
        rl = ReferrerList.from_dict(data)
        assert rl.manifests == []

    def test_non_dict_raises_referrer_error(self):
        with pytest.raises(ReferrerError, match="expected a dict"):
            ReferrerList.from_dict(["not", "a", "dict"])

    def test_none_input_raises_referrer_error(self):
        with pytest.raises(ReferrerError, match="expected a dict"):
            ReferrerList.from_dict(None)

    def test_non_list_manifests_raises_referrer_error(self):
        data = _full_response()
        data["manifests"] = "not-a-list"
        with pytest.raises(ReferrerError, match="must be a list"):
            ReferrerList.from_dict(data)

    def test_invalid_descriptor_raises_referrer_error(self):
        data = _full_response([{"bad": "descriptor"}])
        with pytest.raises(ReferrerError, match="failed to parse descriptor"):
            ReferrerList.from_dict(data)

    def test_preserves_descriptor_order(self):
        descs = [_descriptor_dict(DIGEST_2, SIG_TYPE, 100), _descriptor_dict(DIGEST_1, SBOM_TYPE, 200)]
        data = _full_response(descs)
        rl = ReferrerList.from_dict(data)
        assert rl.manifests[0].digest == DIGEST_2
        assert rl.manifests[1].digest == DIGEST_1

    def test_annotations_preserved(self):
        desc = _descriptor_dict()
        desc["annotations"] = {"org.opencontainers.image.created": "2025-06-15T10:30:00Z"}
        data = _full_response([desc])
        rl = ReferrerList.from_dict(data)
        assert rl.manifests[0].annotations == {"org.opencontainers.image.created": "2025-06-15T10:30:00Z"}


# ---------------------------------------------------------------------------
# ReferrerList.from_json
# ---------------------------------------------------------------------------

class TestReferrerListFromJson:

    def test_happy_path(self):
        payload = json.dumps(_full_response([_descriptor_dict()]))
        rl = ReferrerList.from_json(payload)
        assert len(rl.manifests) == 1
        assert rl.manifests[0].digest == DIGEST_1

    def test_malformed_json_raises_referrer_error(self):
        with pytest.raises(ReferrerError, match="Failed to parse referrers JSON"):
            ReferrerList.from_json("{not valid json}")

    def test_empty_manifests_in_json(self):
        payload = json.dumps(_full_response([]))
        rl = ReferrerList.from_json(payload)
        assert rl.manifests == []

    def test_non_object_json_raises_referrer_error(self):
        with pytest.raises(ReferrerError, match="expected a dict"):
            ReferrerList.from_json('["list", "not", "object"]')


# ---------------------------------------------------------------------------
# ReferrerList.to_dict
# ---------------------------------------------------------------------------

class TestReferrerListToDict:

    def test_emits_image_index_envelope(self):
        rl = ReferrerList(manifests=[])
        d = rl.to_dict()
        assert d["schemaVersion"] == 2
        assert d["mediaType"] == INDEX_MT
        assert d["manifests"] == []

    def test_round_trip(self):
        desc = _descriptor_dict()
        data = _full_response([desc])
        rl = ReferrerList.from_dict(data)
        d = rl.to_dict()
        assert d["schemaVersion"] == 2
        assert d["mediaType"] == INDEX_MT
        assert len(d["manifests"]) == 1
        assert d["manifests"][0]["digest"] == DIGEST_1

    def test_empty_manifests_emitted(self):
        rl = ReferrerList(manifests=[])
        assert rl.to_dict()["manifests"] == []


# ---------------------------------------------------------------------------
# ReferrerList.to_json
# ---------------------------------------------------------------------------

class TestReferrerListToJson:

    def test_produces_valid_json(self):
        rl = ReferrerList.from_dict(_full_response([_descriptor_dict()]))
        parsed = json.loads(rl.to_json())
        assert parsed["schemaVersion"] == 2
        assert len(parsed["manifests"]) == 1

    def test_compact_separators(self):
        rl = ReferrerList(manifests=[])
        s = rl.to_json()
        assert " " not in s

    def test_sort_keys(self):
        rl = ReferrerList(manifests=[])
        s = rl.to_json()
        assert s.index('"manifests"') < s.index('"mediaType"') < s.index('"schemaVersion"')

    def test_round_trip_via_json(self):
        rl = ReferrerList.from_dict(_full_response([_descriptor_dict()]))
        rl2 = ReferrerList.from_json(rl.to_json())
        assert len(rl2.manifests) == len(rl.manifests)
        assert rl2.manifests[0].digest == rl.manifests[0].digest


# ---------------------------------------------------------------------------
# ReferrerList.filter_by_artifact_type
# ---------------------------------------------------------------------------

class TestFilterByArtifactType:

    def test_filters_matching_type(self):
        descs = [_descriptor_dict(DIGEST_1, SBOM_TYPE), _descriptor_dict(DIGEST_2, SIG_TYPE)]
        rl = ReferrerList.from_dict(_full_response(descs))
        filtered = rl.filter_by_artifact_type(SBOM_TYPE)
        assert len(filtered.manifests) == 1
        assert filtered.manifests[0].digest == DIGEST_1

    def test_returns_empty_when_no_match(self):
        descs = [_descriptor_dict(DIGEST_1, SBOM_TYPE)]
        rl = ReferrerList.from_dict(_full_response(descs))
        filtered = rl.filter_by_artifact_type("no/match")
        assert filtered.manifests == []

    def test_returns_all_when_all_match(self):
        descs = [_descriptor_dict(DIGEST_1, SBOM_TYPE), _descriptor_dict(DIGEST_2, SBOM_TYPE)]
        rl = ReferrerList.from_dict(_full_response(descs))
        filtered = rl.filter_by_artifact_type(SBOM_TYPE)
        assert len(filtered.manifests) == 2

    def test_does_not_mutate_original(self):
        descs = [_descriptor_dict(DIGEST_1, SBOM_TYPE), _descriptor_dict(DIGEST_2, SIG_TYPE)]
        rl = ReferrerList.from_dict(_full_response(descs))
        rl.filter_by_artifact_type(SBOM_TYPE)
        assert len(rl.manifests) == 2


# ---------------------------------------------------------------------------
# ReferrerList.merge
# ---------------------------------------------------------------------------

class TestMerge:

    def test_merges_two_lists(self):
        rl1 = ReferrerList.from_dict(_full_response([_descriptor_dict(DIGEST_1)]))
        rl2 = ReferrerList.from_dict(_full_response([_descriptor_dict(DIGEST_2)]))
        merged = rl1.merge(rl2)
        assert len(merged.manifests) == 2
        assert merged.manifests[0].digest == DIGEST_1
        assert merged.manifests[1].digest == DIGEST_2

    def test_merge_with_empty(self):
        rl1 = ReferrerList.from_dict(_full_response([_descriptor_dict(DIGEST_1)]))
        rl2 = ReferrerList(manifests=[])
        merged = rl1.merge(rl2)
        assert len(merged.manifests) == 1

    def test_merge_empty_with_nonempty(self):
        rl1 = ReferrerList(manifests=[])
        rl2 = ReferrerList.from_dict(_full_response([_descriptor_dict(DIGEST_1)]))
        merged = rl1.merge(rl2)
        assert len(merged.manifests) == 1

    def test_does_not_mutate_originals(self):
        rl1 = ReferrerList.from_dict(_full_response([_descriptor_dict(DIGEST_1)]))
        rl2 = ReferrerList.from_dict(_full_response([_descriptor_dict(DIGEST_2)]))
        rl1.merge(rl2)
        assert len(rl1.manifests) == 1
        assert len(rl2.manifests) == 1


# ---------------------------------------------------------------------------
# __post_init__ validation
# ---------------------------------------------------------------------------

class TestPostInit:

    def test_rejects_non_list_manifests(self):
        with pytest.raises(ValueError, match="manifests must be a list"):
            ReferrerList(manifests="not-a-list")  # type: ignore[arg-type]

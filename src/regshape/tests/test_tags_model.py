#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.models.tags`."""

import json
import pytest

from regshape.libs.errors import TagError
from regshape.libs.models.tags import TagList


# ---------------------------------------------------------------------------
# TagList.from_dict
# ---------------------------------------------------------------------------

class TestTagListFromDict:
    def test_happy_path(self):
        data = {"name": "myrepo/myimage", "tags": ["latest", "v1.0", "v2.0"]}
        tl = TagList.from_dict(data)
        assert tl.namespace == "myrepo/myimage"
        assert tl.tags == ["latest", "v1.0", "v2.0"]

    def test_null_tags_normalised_to_empty_list(self):
        data = {"name": "myrepo/myimage", "tags": None}
        tl = TagList.from_dict(data)
        assert tl.tags == []

    def test_missing_tags_key_normalised_to_empty_list(self):
        data = {"name": "myrepo/myimage"}
        tl = TagList.from_dict(data)
        assert tl.tags == []

    def test_empty_tags_list(self):
        data = {"name": "myrepo/myimage", "tags": []}
        tl = TagList.from_dict(data)
        assert tl.tags == []

    def test_missing_name_raises_tag_error(self):
        with pytest.raises(TagError, match="missing required field"):
            TagList.from_dict({"tags": ["latest"]})

    def test_non_dict_raises_tag_error(self):
        with pytest.raises(TagError, match="expected a dict"):
            TagList.from_dict(["not", "a", "dict"])

    def test_none_input_raises_tag_error(self):
        with pytest.raises(TagError, match="expected a dict"):
            TagList.from_dict(None)

    def test_empty_namespace_raises_value_error(self):
        with pytest.raises(ValueError, match="namespace must not be empty"):
            TagList.from_dict({"name": "", "tags": []})

    def test_tags_not_list_after_normalisation_raises_value_error(self):
        # A non-list, non-null "tags" value falls through normalisation
        # as a non-list and is passed to __post_init__, which rejects it.
        with pytest.raises(ValueError, match="tags must be a list"):
            TagList(namespace="myrepo/myimage", tags="not-a-list")  # type: ignore[arg-type]

    def test_preserves_tag_order(self):
        tags = ["z-tag", "a-tag", "m-tag"]
        tl = TagList.from_dict({"name": "repo", "tags": tags})
        assert tl.tags == tags

    def test_simple_namespace(self):
        tl = TagList.from_dict({"name": "library/ubuntu", "tags": ["22.04"]})
        assert tl.namespace == "library/ubuntu"


# ---------------------------------------------------------------------------
# TagList.from_json
# ---------------------------------------------------------------------------

class TestTagListFromJson:
    def test_happy_path(self):
        payload = json.dumps({"name": "myrepo/myimage", "tags": ["v1", "v2"]})
        tl = TagList.from_json(payload)
        assert tl.namespace == "myrepo/myimage"
        assert tl.tags == ["v1", "v2"]

    def test_malformed_json_raises_tag_error(self):
        with pytest.raises(TagError, match="Failed to parse tag-list JSON"):
            TagList.from_json("{not valid json}")

    def test_null_tags_in_json(self):
        payload = json.dumps({"name": "myrepo/myimage", "tags": None})
        tl = TagList.from_json(payload)
        assert tl.tags == []

    def test_missing_name_in_json_raises_tag_error(self):
        payload = json.dumps({"tags": ["latest"]})
        with pytest.raises(TagError, match="missing required field"):
            TagList.from_json(payload)

    def test_non_object_json_raises_tag_error(self):
        with pytest.raises(TagError, match="expected a dict"):
            TagList.from_json('["list", "not", "object"]')


# ---------------------------------------------------------------------------
# TagList.to_dict
# ---------------------------------------------------------------------------

class TestTagListToDict:
    def test_round_trip(self):
        original = {"name": "myrepo/myimage", "tags": ["latest", "v1.0"]}
        tl = TagList.from_dict(original)
        assert tl.to_dict() == original

    def test_namespace_serialised_as_name(self):
        tl = TagList(namespace="ns/repo", tags=["t1"])
        d = tl.to_dict()
        assert "name" in d
        assert d["name"] == "ns/repo"
        assert "namespace" not in d

    def test_empty_tags_emitted(self):
        tl = TagList(namespace="ns/repo", tags=[])
        assert tl.to_dict() == {"name": "ns/repo", "tags": []}


# ---------------------------------------------------------------------------
# TagList.to_json
# ---------------------------------------------------------------------------

class TestTagListToJson:
    def test_produces_valid_json(self):
        tl = TagList(namespace="myrepo/myimage", tags=["v1", "v2"])
        parsed = json.loads(tl.to_json())
        assert parsed["name"] == "myrepo/myimage"
        assert parsed["tags"] == ["v1", "v2"]

    def test_compact_separators(self):
        tl = TagList(namespace="repo", tags=["t"])
        s = tl.to_json()
        assert " " not in s

    def test_sort_keys(self):
        tl = TagList(namespace="repo", tags=["b", "a"])
        s = tl.to_json()
        # "name" comes before "tags" alphabetically
        assert s.index('"name"') < s.index('"tags"')

    def test_round_trip_via_json(self):
        tl = TagList(namespace="myrepo/myimage", tags=["latest", "v1.0"])
        tl2 = TagList.from_json(tl.to_json())
        assert tl2.namespace == tl.namespace
        assert tl2.tags == tl.tags


# ---------------------------------------------------------------------------
# TagList construction validation
# ---------------------------------------------------------------------------

class TestTagListValidation:
    def test_empty_namespace_raises(self):
        with pytest.raises(ValueError, match="namespace must not be empty"):
            TagList(namespace="", tags=[])

    def test_tags_not_a_list_raises(self):
        with pytest.raises(ValueError, match="tags must be a list"):
            TagList(namespace="repo", tags=("t1", "t2"))  # type: ignore[arg-type]

    def test_valid_construction(self):
        tl = TagList(namespace="a/b", tags=["x"])
        assert tl.namespace == "a/b"
        assert tl.tags == ["x"]

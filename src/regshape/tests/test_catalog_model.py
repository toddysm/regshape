#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.models.catalog`."""

import json
import pytest

from regshape.libs.errors import CatalogError, CatalogNotSupportedError, RegShapeError
from regshape.libs.models.catalog import RepositoryCatalog


# ---------------------------------------------------------------------------
# RepositoryCatalog.from_dict
# ---------------------------------------------------------------------------

class TestRepositoryCatalogFromDict:
    def test_happy_path(self):
        data = {"repositories": ["library/ubuntu", "myrepo/myimage", "myrepo/other"]}
        rc = RepositoryCatalog.from_dict(data)
        assert rc.repositories == ["library/ubuntu", "myrepo/myimage", "myrepo/other"]

    def test_null_repositories_normalised_to_empty_list(self):
        data = {"repositories": None}
        rc = RepositoryCatalog.from_dict(data)
        assert rc.repositories == []

    def test_missing_repositories_key_normalised_to_empty_list(self):
        rc = RepositoryCatalog.from_dict({})
        assert rc.repositories == []

    def test_empty_repositories_list(self):
        data = {"repositories": []}
        rc = RepositoryCatalog.from_dict(data)
        assert rc.repositories == []

    def test_non_dict_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="expected a dict"):
            RepositoryCatalog.from_dict(["not", "a", "dict"])

    def test_none_input_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="expected a dict"):
            RepositoryCatalog.from_dict(None)

    def test_integer_input_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="expected a dict"):
            RepositoryCatalog.from_dict(42)

    def test_non_list_repositories_raises_value_error(self):
        with pytest.raises(ValueError, match="repositories must be a list"):
            RepositoryCatalog.from_dict({"repositories": "not-a-list"})

    def test_preserves_repository_order(self):
        repos = ["z/repo", "a/repo", "m/repo"]
        rc = RepositoryCatalog.from_dict({"repositories": repos})
        assert rc.repositories == repos

    def test_single_repository(self):
        rc = RepositoryCatalog.from_dict({"repositories": ["library/ubuntu"]})
        assert rc.repositories == ["library/ubuntu"]

    def test_extra_keys_ignored(self):
        data = {"repositories": ["a/b"], "next": "cursor-token"}
        rc = RepositoryCatalog.from_dict(data)
        assert rc.repositories == ["a/b"]


# ---------------------------------------------------------------------------
# RepositoryCatalog.from_json
# ---------------------------------------------------------------------------

class TestRepositoryCatalogFromJson:
    def test_happy_path(self):
        payload = json.dumps({"repositories": ["library/ubuntu", "myrepo/myimage"]})
        rc = RepositoryCatalog.from_json(payload)
        assert rc.repositories == ["library/ubuntu", "myrepo/myimage"]

    def test_malformed_json_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="Failed to parse catalog JSON"):
            RepositoryCatalog.from_json("{not valid json}")

    def test_null_repositories_in_json(self):
        payload = json.dumps({"repositories": None})
        rc = RepositoryCatalog.from_json(payload)
        assert rc.repositories == []

    def test_missing_repositories_key_in_json(self):
        payload = json.dumps({})
        rc = RepositoryCatalog.from_json(payload)
        assert rc.repositories == []

    def test_non_object_json_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="expected a dict"):
            RepositoryCatalog.from_json('["list", "not", "object"]')

    def test_empty_repositories_in_json(self):
        payload = json.dumps({"repositories": []})
        rc = RepositoryCatalog.from_json(payload)
        assert rc.repositories == []


# ---------------------------------------------------------------------------
# RepositoryCatalog.to_dict
# ---------------------------------------------------------------------------

class TestRepositoryCatalogToDict:
    def test_round_trip(self):
        original = {"repositories": ["library/ubuntu", "myrepo/myimage"]}
        rc = RepositoryCatalog.from_dict(original)
        assert rc.to_dict() == original

    def test_empty_repositories_emitted(self):
        rc = RepositoryCatalog(repositories=[])
        assert rc.to_dict() == {"repositories": []}

    def test_no_extra_keys(self):
        rc = RepositoryCatalog(repositories=["a/b"])
        d = rc.to_dict()
        assert set(d.keys()) == {"repositories"}

    def test_repositories_key_present(self):
        rc = RepositoryCatalog(repositories=["a/b", "c/d"])
        d = rc.to_dict()
        assert "repositories" in d
        assert d["repositories"] == ["a/b", "c/d"]


# ---------------------------------------------------------------------------
# RepositoryCatalog.to_json
# ---------------------------------------------------------------------------

class TestRepositoryCatalogToJson:
    def test_produces_valid_json(self):
        rc = RepositoryCatalog(repositories=["library/ubuntu", "myrepo/myimage"])
        parsed = json.loads(rc.to_json())
        assert parsed["repositories"] == ["library/ubuntu", "myrepo/myimage"]

    def test_compact_separators(self):
        rc = RepositoryCatalog(repositories=["a/b"])
        s = rc.to_json()
        assert " " not in s

    def test_empty_repositories_emitted_as_empty_array(self):
        rc = RepositoryCatalog(repositories=[])
        s = rc.to_json()
        assert '"repositories":[]' in s

    def test_round_trip_via_json(self):
        rc = RepositoryCatalog(repositories=["library/ubuntu", "myrepo/myimage"])
        rc2 = RepositoryCatalog.from_json(rc.to_json())
        assert rc.repositories == rc2.repositories


# ---------------------------------------------------------------------------
# RepositoryCatalog direct construction / __post_init__ validation
# ---------------------------------------------------------------------------

class TestRepositoryCatalogConstruction:
    def test_constructor_rejects_non_list_repositories(self):
        with pytest.raises(ValueError, match="repositories must be a list"):
            RepositoryCatalog(repositories="not-a-list")  # type: ignore[arg-type]

    def test_constructor_accepts_empty_list(self):
        rc = RepositoryCatalog(repositories=[])
        assert rc.repositories == []

    def test_constructor_accepts_populated_list(self):
        rc = RepositoryCatalog(repositories=["a/b", "c/d"])
        assert rc.repositories == ["a/b", "c/d"]


# ---------------------------------------------------------------------------
# Error class hierarchy
# ---------------------------------------------------------------------------

class TestCatalogErrorHierarchy:
    def test_catalog_error_is_regshape_error(self):
        assert issubclass(CatalogError, RegShapeError)

    def test_catalog_not_supported_error_is_catalog_error(self):
        assert issubclass(CatalogNotSupportedError, CatalogError)

    def test_catalog_not_supported_error_is_regshape_error(self):
        assert issubclass(CatalogNotSupportedError, RegShapeError)

    def test_catalog_not_supported_error_can_be_caught_as_catalog_error(self):
        with pytest.raises(CatalogError):
            raise CatalogNotSupportedError(
                "Registry does not support the catalog API", "404 Not Found"
            )

    def test_catalog_error_not_caught_as_not_supported(self):
        err = CatalogError("malformed response", "missing key")
        assert not isinstance(err, CatalogNotSupportedError)

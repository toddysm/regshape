#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.tags.operations`."""

import json

import pytest
import requests
from unittest.mock import MagicMock

from regshape.libs.errors import AuthError, TagError
from regshape.libs.tags.operations import (
    _raise_for_delete_error,
    _raise_for_list_error,
    delete_tag,
    list_tags,
)
from regshape.libs.transport import RegistryClient, TransportConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
TAG = "v1.0"
TAG_LIST_JSON = json.dumps({"name": REPO, "tags": ["v1.0", "v2.0", "latest"]})


def _mock_client() -> MagicMock:
    client = MagicMock(spec=RegistryClient)
    config = MagicMock(spec=TransportConfig)
    config.registry = REGISTRY
    client.config = config
    return client


def _make_response(
    status_code: int,
    body: str = "{}",
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    resp.headers = {}
    return resp


# ===========================================================================
# list_tags
# ===========================================================================

class TestListTags:

    def test_returns_tag_list(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=TAG_LIST_JSON)
        result = list_tags(client, REPO)
        assert result.namespace == REPO
        assert "v1.0" in result.tags

    def test_get_called_with_correct_path(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=TAG_LIST_JSON)
        list_tags(client, REPO)
        client.get.assert_called_once()
        assert client.get.call_args[0][0] == f"/v2/{REPO}/tags/list"

    def test_no_pagination_params_by_default(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=TAG_LIST_JSON)
        list_tags(client, REPO)
        call_kwargs = client.get.call_args[1]
        assert call_kwargs.get("params") is None

    def test_page_size_forwarded_as_n(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=TAG_LIST_JSON)
        list_tags(client, REPO, page_size=10)
        params = client.get.call_args[1].get("params")
        assert params == {"n": 10}

    def test_last_param_forwarded(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=TAG_LIST_JSON)
        list_tags(client, REPO, last="v1.0")
        params = client.get.call_args[1].get("params")
        assert params == {"last": "v1.0"}

    def test_page_size_and_last_combined(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=TAG_LIST_JSON)
        list_tags(client, REPO, page_size=5, last="v1.0")
        params = client.get.call_args[1].get("params")
        assert params == {"n": 5, "last": "v1.0"}

    def test_404_raises_tag_error_repository_not_found(self):
        client = _mock_client()
        client.get.return_value = _make_response(404)
        with pytest.raises(TagError, match="not found"):
            list_tags(client, REPO)

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(401)
        with pytest.raises(AuthError):
            list_tags(client, REPO)

    def test_500_raises_tag_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(500)
        with pytest.raises(TagError):
            list_tags(client, REPO)

    def test_invalid_json_raises_tag_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body="not json")
        with pytest.raises(TagError, match="Failed to parse"):
            list_tags(client, REPO)


# ===========================================================================
# delete_tag
# ===========================================================================

class TestDeleteTag:

    def test_succeeds_silently_on_202(self):
        client = _mock_client()
        client.delete.return_value = _make_response(202)
        delete_tag(client, REPO, TAG)  # should not raise

    def test_delete_called_with_correct_path(self):
        client = _mock_client()
        client.delete.return_value = _make_response(202)
        delete_tag(client, REPO, TAG)
        assert client.delete.call_args[0][0] == f"/v2/{REPO}/manifests/{TAG}"

    def test_404_raises_tag_error_tag_not_found(self):
        client = _mock_client()
        client.delete.return_value = _make_response(404)
        with pytest.raises(TagError, match="Tag not found"):
            delete_tag(client, REPO, TAG)

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.delete.return_value = _make_response(401)
        with pytest.raises(AuthError):
            delete_tag(client, REPO, TAG)

    def test_405_raises_tag_error_not_supported(self):
        client = _mock_client()
        client.delete.return_value = _make_response(405)
        with pytest.raises(TagError, match="not supported"):
            delete_tag(client, REPO, TAG)

    def test_400_raises_tag_error_not_supported(self):
        client = _mock_client()
        client.delete.return_value = _make_response(400)
        with pytest.raises(TagError, match="not supported"):
            delete_tag(client, REPO, TAG)

    def test_500_raises_tag_error(self):
        client = _mock_client()
        client.delete.return_value = _make_response(500)
        with pytest.raises(TagError):
            delete_tag(client, REPO, TAG)


# ===========================================================================
# _raise_for_list_error — direct tests
# ===========================================================================

class TestRaiseForListError:

    def test_2xx_is_silent(self):
        for code in (200, 201, 202):
            _raise_for_list_error(_make_response(code), REGISTRY, REPO)

    def test_404_raises_tag_error_with_repository_in_message(self):
        with pytest.raises(TagError, match=REPO):
            _raise_for_list_error(_make_response(404), REGISTRY, REPO)

    def test_401_raises_auth_error_with_registry_in_message(self):
        with pytest.raises(AuthError, match=REGISTRY):
            _raise_for_list_error(_make_response(401), REGISTRY, REPO)

    def test_oci_error_detail_used_when_present(self):
        body = json.dumps({"errors": [{"code": "NAME_UNKNOWN", "message": "repo not found"}]})
        resp = _make_response(404, body=body)
        with pytest.raises(TagError, match="repo not found"):
            _raise_for_list_error(resp, REGISTRY, REPO)

    def test_503_raises_tag_error(self):
        with pytest.raises(TagError, match="Registry error"):
            _raise_for_list_error(_make_response(503), REGISTRY, REPO)


# ===========================================================================
# _raise_for_delete_error — direct tests
# ===========================================================================

class TestRaiseForDeleteError:

    def test_2xx_is_silent(self):
        for code in (200, 202):
            _raise_for_delete_error(_make_response(code), REGISTRY, REPO, TAG)

    def test_404_raises_tag_error_tag_not_found(self):
        with pytest.raises(TagError, match="Tag not found"):
            _raise_for_delete_error(_make_response(404), REGISTRY, REPO, TAG)

    def test_401_raises_auth_error(self):
        with pytest.raises(AuthError):
            _raise_for_delete_error(_make_response(401), REGISTRY, REPO, TAG)

    def test_405_raises_tag_error_not_supported(self):
        with pytest.raises(TagError, match="not supported"):
            _raise_for_delete_error(_make_response(405), REGISTRY, REPO, TAG)

    def test_400_raises_tag_error_not_supported(self):
        with pytest.raises(TagError, match="not supported"):
            _raise_for_delete_error(_make_response(400), REGISTRY, REPO, TAG)

    def test_oci_error_detail_used_when_present(self):
        body = json.dumps({"errors": [{"code": "UNSUPPORTED", "message": "deletion disabled"}]})
        resp = _make_response(405, body=body)
        with pytest.raises(TagError, match="deletion disabled"):
            _raise_for_delete_error(resp, REGISTRY, REPO, TAG)

#!/usr/bin/env python3

"""
Tests for :mod:`regshape.libs.catalog.operations`.

Exercises the domain functions directly (without the CLI layer). The
:class:`~regshape.libs.transport.RegistryClient` and HTTP responses are
replaced with lightweight mocks so no network I/O is performed.
"""

import json
from unittest.mock import MagicMock, call

import pytest

from regshape.libs.catalog.operations import (
    _parse_next_cursor,
    _raise_for_catalog_error,
    list_catalog,
    list_catalog_all,
)
from regshape.libs.errors import AuthError, CatalogError, CatalogNotSupportedError
from regshape.libs.models.catalog import RepositoryCatalog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"


def _make_client(registry: str = REGISTRY) -> MagicMock:
    """Return a minimal mock RegistryClient."""
    client = MagicMock()
    client.config.registry = registry
    return client


def _make_response(
    repositories: list[str] | None = None,
    status_code: int = 200,
    link: str | None = None,
    body: str | None = None,
) -> MagicMock:
    """Return a mock HTTP response for a catalog endpoint."""
    response = MagicMock()
    response.status_code = status_code
    if body is not None:
        response.text = body
    elif repositories is not None:
        response.text = json.dumps({"repositories": repositories})
    else:
        response.text = json.dumps({"repositories": []})
    headers = {}
    if link is not None:
        headers["Link"] = link
    response.headers = headers
    return response


def _next_link(last: str, n: int = 100) -> str:
    return f"</v2/_catalog?last={last}&n={n}>; rel=\"next\""


# ===========================================================================
# list_catalog — basic behaviour
# ===========================================================================

class TestListCatalog:

    def test_happy_path_returns_repository_catalog(self):
        repos = ["library/ubuntu", "myrepo/myimage"]
        client = _make_client()
        client.get.return_value = _make_response(repos)

        result = list_catalog(client)

        assert isinstance(result, RepositoryCatalog)
        assert result.repositories == repos

    def test_calls_correct_path(self):
        client = _make_client()
        client.get.return_value = _make_response([])

        list_catalog(client)

        client.get.assert_called_once_with("/v2/_catalog", params=None)

    def test_page_size_sent_as_n_param(self):
        client = _make_client()
        client.get.return_value = _make_response([])

        list_catalog(client, page_size=50)

        client.get.assert_called_once_with("/v2/_catalog", params={"n": 50})

    def test_last_sent_as_last_param(self):
        client = _make_client()
        client.get.return_value = _make_response([])

        list_catalog(client, last="myrepo/other")

        client.get.assert_called_once_with(
            "/v2/_catalog", params={"last": "myrepo/other"}
        )

    def test_both_page_size_and_last(self):
        client = _make_client()
        client.get.return_value = _make_response([])

        list_catalog(client, page_size=25, last="myrepo/other")

        client.get.assert_called_once_with(
            "/v2/_catalog", params={"n": 25, "last": "myrepo/other"}
        )

    def test_empty_repository_list(self):
        client = _make_client()
        client.get.return_value = _make_response([])

        result = list_catalog(client)

        assert result.repositories == []

    def test_null_repositories_normalised_to_empty(self):
        client = _make_client()
        client.get.return_value = _make_response(
            body=json.dumps({"repositories": None})
        )

        result = list_catalog(client)

        assert result.repositories == []


# ===========================================================================
# list_catalog — error propagation from _raise_for_catalog_error
# ===========================================================================

class TestListCatalogErrors:

    def test_404_raises_catalog_not_supported(self):
        client = _make_client()
        client.get.return_value = _make_response(status_code=404)

        with pytest.raises(CatalogNotSupportedError, match="does not support"):
            list_catalog(client)

    def test_405_raises_catalog_not_supported(self):
        client = _make_client()
        client.get.return_value = _make_response(status_code=405)

        with pytest.raises(CatalogNotSupportedError, match="does not support"):
            list_catalog(client)

    def test_401_raises_auth_error(self):
        client = _make_client()
        client.get.return_value = _make_response(status_code=401)

        with pytest.raises(AuthError, match="Authentication failed"):
            list_catalog(client)

    def test_403_raises_auth_error(self):
        client = _make_client()
        client.get.return_value = _make_response(status_code=403)

        with pytest.raises(AuthError, match="Authorisation denied"):
            list_catalog(client)

    def test_500_raises_catalog_error(self):
        client = _make_client()
        client.get.return_value = _make_response(status_code=500)

        with pytest.raises(CatalogError, match="Registry error"):
            list_catalog(client)

    def test_malformed_json_raises_catalog_error(self):
        client = _make_client()
        client.get.return_value = _make_response(body="{not valid json}")

        with pytest.raises(CatalogError):
            list_catalog(client)


# ===========================================================================
# list_catalog_all
# ===========================================================================

class TestListCatalogAll:

    def test_single_page_no_link_header(self):
        repos = ["library/ubuntu", "myrepo/myimage"]
        client = _make_client()
        client.get.return_value = _make_response(repos)

        result = list_catalog_all(client)

        assert result.repositories == repos
        assert client.get.call_count == 1

    def test_two_pages_merged(self):
        page1_repos = ["a/one", "a/two"]
        page2_repos = ["b/three", "b/four"]
        client = _make_client()
        client.get.side_effect = [
            _make_response(page1_repos, link=_next_link("a/two")),
            _make_response(page2_repos),
        ]

        result = list_catalog_all(client)

        assert result.repositories == page1_repos + page2_repos
        assert client.get.call_count == 2

    def test_three_pages_merged(self):
        pages = [["a/one"], ["b/two"], ["c/three"]]
        client = _make_client()
        client.get.side_effect = [
            _make_response(pages[0], link=_next_link("a/one")),
            _make_response(pages[1], link=_next_link("b/two")),
            _make_response(pages[2]),
        ]

        result = list_catalog_all(client)

        assert result.repositories == ["a/one", "b/two", "c/three"]
        assert client.get.call_count == 3

    def test_second_request_uses_cursor_from_link(self):
        client = _make_client()
        client.get.side_effect = [
            _make_response(["a/one"], link=_next_link("a/one", n=1)),
            _make_response(["b/two"]),
        ]

        list_catalog_all(client, page_size=1)

        calls = client.get.call_args_list
        assert calls[0] == call("/v2/_catalog", params={"n": 1})
        assert calls[1] == call("/v2/_catalog", params={"n": 1, "last": "a/one"})

    def test_no_page_size_omits_n_param(self):
        client = _make_client()
        client.get.return_value = _make_response(["a/one"])

        list_catalog_all(client)

        client.get.assert_called_once_with("/v2/_catalog", params=None)

    def test_returns_repository_catalog_instance(self):
        client = _make_client()
        client.get.return_value = _make_response(["a/b"])

        result = list_catalog_all(client)

        assert isinstance(result, RepositoryCatalog)

    def test_empty_registry_returns_empty_catalog(self):
        client = _make_client()
        client.get.return_value = _make_response([])

        result = list_catalog_all(client)

        assert result.repositories == []

    def test_404_propagates_as_catalog_not_supported(self):
        client = _make_client()
        client.get.return_value = _make_response(status_code=404)

        with pytest.raises(CatalogNotSupportedError):
            list_catalog_all(client)

    def test_error_on_second_page_propagates(self):
        client = _make_client()
        client.get.side_effect = [
            _make_response(["a/one"], link=_next_link("a/one")),
            _make_response(status_code=500),
        ]

        with pytest.raises(CatalogError, match="Registry error"):
            list_catalog_all(client)


# ===========================================================================
# _raise_for_catalog_error
# ===========================================================================

class TestRaiseForCatalogError:

    def _response(self, status_code: int, text: str = "") -> MagicMock:
        r = MagicMock()
        r.status_code = status_code
        r.text = text
        r.headers = {}
        return r

    def test_2xx_does_not_raise(self):
        for code in (200, 201, 204):
            _raise_for_catalog_error(self._response(code), REGISTRY)  # no exception

    def test_401_raises_auth_error(self):
        with pytest.raises(AuthError, match="Authentication failed"):
            _raise_for_catalog_error(self._response(401), REGISTRY)

    def test_403_raises_auth_error(self):
        with pytest.raises(AuthError, match="Authorisation denied"):
            _raise_for_catalog_error(self._response(403), REGISTRY)

    def test_404_raises_catalog_not_supported(self):
        with pytest.raises(CatalogNotSupportedError, match="does not support"):
            _raise_for_catalog_error(self._response(404), REGISTRY)

    def test_405_raises_catalog_not_supported(self):
        with pytest.raises(CatalogNotSupportedError, match="does not support"):
            _raise_for_catalog_error(self._response(405), REGISTRY)

    def test_500_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="Registry error"):
            _raise_for_catalog_error(self._response(500), REGISTRY)

    def test_503_raises_catalog_error(self):
        with pytest.raises(CatalogError, match="Registry error"):
            _raise_for_catalog_error(self._response(503), REGISTRY)

    def test_not_supported_is_subclass_of_catalog_error(self):
        with pytest.raises(CatalogError):
            _raise_for_catalog_error(self._response(404), REGISTRY)

    def test_registry_name_in_error_message(self):
        with pytest.raises(CatalogNotSupportedError, match=REGISTRY):
            _raise_for_catalog_error(self._response(404), REGISTRY)

    def test_auth_error_includes_registry_name(self):
        with pytest.raises(AuthError, match=REGISTRY):
            _raise_for_catalog_error(self._response(401), REGISTRY)


# ===========================================================================
# _parse_next_cursor
# ===========================================================================

class TestParseNextCursor:

    def test_standard_link_header(self):
        headers = {"Link": '</v2/_catalog?last=myrepo/image&n=100>; rel="next"'}
        assert _parse_next_cursor(headers) == "myrepo/image"

    def test_link_header_lowercase_key(self):
        headers = {"link": '</v2/_catalog?last=myrepo/image&n=100>; rel="next"'}
        assert _parse_next_cursor(headers) == "myrepo/image"

    def test_no_link_header_returns_none(self):
        assert _parse_next_cursor({}) is None

    def test_empty_link_header_returns_none(self):
        assert _parse_next_cursor({"Link": ""}) is None

    def test_rel_not_next_returns_none(self):
        headers = {"Link": '</v2/_catalog?last=myrepo/image&n=100>; rel="prev"'}
        assert _parse_next_cursor(headers) is None

    def test_no_last_param_in_url_returns_none(self):
        headers = {"Link": '</v2/_catalog?n=100>; rel="next"'}
        assert _parse_next_cursor(headers) is None

    def test_multiple_relations_picks_next(self):
        headers = {
            "Link": (
                '</v2/_catalog?last=a/one&n=100>; rel="prev", '
                '</v2/_catalog?last=a/two&n=100>; rel="next"'
            )
        }
        assert _parse_next_cursor(headers) == "a/two"

    def test_rel_without_quotes(self):
        headers = {"Link": "</v2/_catalog?last=myrepo/image&n=100>; rel=next"}
        assert _parse_next_cursor(headers) == "myrepo/image"

    def test_cursor_with_slash(self):
        headers = {"Link": '</v2/_catalog?last=org/repo/name&n=50>; rel="next"'}
        assert _parse_next_cursor(headers) == "org/repo/name"

    def test_page_size_does_not_affect_cursor(self):
        headers = {"Link": '</v2/_catalog?last=a/b&n=25>; rel="next"'}
        assert _parse_next_cursor(headers) == "a/b"

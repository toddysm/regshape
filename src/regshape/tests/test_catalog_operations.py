#!/usr/bin/env python3

"""
Tests for :mod:`regshape.libs.catalog.operations`.

Exercises the domain functions with lightweight client mocks. Contract tests
also use the real :class:`~regshape.libs.transport.RegistryClient` with mocked
HTTP responses. No network I/O is performed.
"""

import json
from unittest.mock import MagicMock, call, patch

import pytest

from regshape.libs.catalog.operations import (
    _parse_next_cursor,
    _raise_for_catalog_error,
    list_catalog,
    list_catalog_all,
)
from regshape.libs.errors import AuthError, CatalogError, CatalogNotSupportedError
from regshape.libs.models.catalog import RepositoryCatalog
from regshape.libs.transport import RegistryClient, TransportConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"


def _make_client(
    registry: str = REGISTRY,
    *,
    responses: list[MagicMock] | None = None,
) -> MagicMock:
    """Build a mock whose configured response sequence mirrors client metadata.

    A real RegistryClient updates ``last_response`` before returning from
    ``get``. Pagination fixtures must do the same so the operation can read
    the continuation headers from the page it just fetched.
    """
    client = MagicMock()
    client.config.registry = registry
    client.last_response = None
    if responses is not None:
        pages = iter(responses)

        def get(*_args: object, **_kwargs: object) -> MagicMock:
            """Return the next queued response and publish its pagination metadata."""
            response = next(pages)
            client.last_response = response
            return response

        client.get.side_effect = get
    return client


def _make_response(
    repositories: list[str] | None = None,
    status_code: int = 200,
    link: str | None = None,
    body: str | None = None,
) -> MagicMock:
    """Return catalog response text, bytes, and headers for mock or real clients."""
    response = MagicMock()
    response.status_code = status_code
    if body is not None:
        response.text = body
    elif repositories is not None:
        response.text = json.dumps({"repositories": repositories})
    else:
        response.text = json.dumps({"repositories": []})
    response.content = response.text.encode("utf-8")
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
    """Verify traversal with faithful mocks and the real client metadata contract."""

    def test_single_page_no_link_header(self) -> None:
        """Stop after a page whose actual response has no continuation link."""
        repos = ["library/ubuntu", "myrepo/myimage"]
        client = _make_client(responses=[_make_response(repos)])

        result = list_catalog_all(client)

        assert result.repositories == repos
        assert client.get.call_count == 1

    def test_two_pages_merged(self) -> None:
        """Merge both pages after the client publishes the first continuation."""
        page1_repos = ["a/one", "a/two"]
        page2_repos = ["b/three", "b/four"]
        client = _make_client(responses=[
            _make_response(page1_repos, link=_next_link("a/two")),
            _make_response(page2_repos),
        ])

        result = list_catalog_all(client)

        assert result.repositories == page1_repos + page2_repos
        assert client.get.call_count == 2

    def test_three_pages_merged(self) -> None:
        """Follow successive continuation headers until the final page."""
        pages = [["a/one"], ["b/two"], ["c/three"]]
        client = _make_client(responses=[
            _make_response(pages[0], link=_next_link("a/one")),
            _make_response(pages[1], link=_next_link("b/two")),
            _make_response(pages[2]),
        ])

        result = list_catalog_all(client)

        assert result.repositories == ["a/one", "b/two", "c/three"]
        assert client.get.call_count == 3

    def test_second_request_uses_cursor_from_link(self) -> None:
        """Keep the configured page size and send the cursor from the prior response."""
        client = _make_client(responses=[
            _make_response(["a/one"], link=_next_link("a/one", n=1)),
            _make_response(["b/two"]),
        ])

        list_catalog_all(client, page_size=1)

        calls = client.get.call_args_list
        assert calls[0] == call("/v2/_catalog", params={"n": 1})
        assert calls[1] == call("/v2/_catalog", params={"n": 1, "last": "a/one"})

    def test_no_page_size_omits_n_param(self) -> None:
        """Omit n when the caller leaves the page size unspecified."""
        client = _make_client(responses=[_make_response(["a/one"])])

        list_catalog_all(client)

        client.get.assert_called_once_with("/v2/_catalog", params=None)

    def test_returns_repository_catalog_instance(self) -> None:
        """Return the public catalog model after traversal."""
        client = _make_client(responses=[_make_response(["a/b"])])

        result = list_catalog_all(client)

        assert isinstance(result, RepositoryCatalog)

    def test_empty_registry_returns_empty_catalog(self) -> None:
        """Treat an empty final page as a successful empty catalog."""
        client = _make_client(responses=[_make_response([])])

        result = list_catalog_all(client)

        assert result.repositories == []

    def test_404_propagates_as_catalog_not_supported(self) -> None:
        """Preserve the unsupported-catalog error rather than returning an empty list."""
        client = _make_client(responses=[_make_response(status_code=404)])

        with pytest.raises(CatalogNotSupportedError):
            list_catalog_all(client)

    def test_error_on_second_page_propagates(self) -> None:
        """Reach the second page and propagate its registry error."""
        client = _make_client(responses=[
            _make_response(["a/one"], link=_next_link("a/one")),
            _make_response(status_code=500),
        ])

        with pytest.raises(CatalogError, match="Registry error"):
            list_catalog_all(client)
        assert client.get.call_count == 2

    @pytest.mark.parametrize("enable_middleware", [False, True])
    @pytest.mark.parametrize("second_status", [200, 500])
    def test_real_client_response_metadata_drives_pagination(
        self, enable_middleware: bool, second_status: int
    ) -> None:
        """Verify the mock contract against both real transports, without network I/O."""
        responses = [
            _make_response(["a/one"], link=_next_link("a/one", n=1)),
            _make_response(["b/two"], status_code=second_status),
        ]
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("regshape.libs.transport.client.http_request", side_effect=responses) as http,
        ):
            client = RegistryClient(TransportConfig(
                registry=REGISTRY, enable_middleware=enable_middleware,
            ))
            if second_status == 500:
                with pytest.raises(CatalogError, match="Registry error"):
                    list_catalog_all(client, page_size=1)
            else:
                result = list_catalog_all(client, page_size=1)
                assert result.repositories == ["a/one", "b/two"]

        assert client.last_response is responses[1]
        assert http.call_count == 2
        urls = [
            entry.kwargs["url"] if "url" in entry.kwargs else entry.args[0]
            for entry in http.call_args_list
        ]
        assert urls == [f"https://{REGISTRY}/v2/_catalog"] * 2
        assert http.call_args_list[0].kwargs["params"] == {"n": 1}
        assert http.call_args_list[1].kwargs["params"] == {"n": 1, "last": "a/one"}


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

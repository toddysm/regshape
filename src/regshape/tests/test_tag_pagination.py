#!/usr/bin/env python3

"""OCI tag-pagination regressions across library, CLI, and transport boundaries."""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, TagError
from regshape.libs.models.tags import TagList
from regshape.libs.tags import list_tags, list_tags_all
from regshape.libs.tags.operations import _next_tag_link
from regshape.libs.transport import RegistryClient, TransportConfig

REGISTRY = "registry.example.com"
REPO = "team/image"
PATH = f"/v2/{REPO}/tags/list"
BASE = f"https://{REGISTRY}"


def _page(
    tags: object,
    *,
    link: str | None = None,
    status: int = 200,
    url: str | None = None,
    namespace: str = REPO,
) -> requests.Response:
    """Build a real in-memory response, including intentionally invalid tag data."""
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({"name": namespace, "tags": tags}).encode()
    response.headers["Content-Type"] = "application/json"
    if link is not None:
        response.headers["Link"] = link
    if url is not None:
        response.url = url
    return response


def _client(
    pages: list[requests.Response | Exception], *, insecure: bool = False
) -> MagicMock:
    """Provide deterministic pages without credential lookup or network access."""
    client = MagicMock(spec=RegistryClient)
    client.config = TransportConfig(registry=REGISTRY, insecure=insecure)
    client.base_url = f"{'http' if insecure else 'https'}://{REGISTRY}"
    client.get.side_effect = pages
    return client


class TestListTagsAll:
    """Cover OCI link preference, legacy cursors, and explicit failure boundaries."""

    @pytest.mark.parametrize("tags", [[], ["A", "b"], None])
    def test_default_page_size_and_result_shape(self, tags: object) -> None:
        """Return one normalized model with a positive all-pages default."""
        client = _client([_page(tags)])
        result = list_tags_all(client, REPO)
        assert result.to_dict() == {"name": REPO, "tags": tags or []}
        client.get.assert_called_once_with(f"{PATH}?n=100", params=None)

    @pytest.mark.parametrize("size", [0, -1, True, 1.5, "2"])
    def test_invalid_page_size_fails_before_network(self, size: object) -> None:
        """Reject invalid runtime input rather than reporting an incomplete success."""
        client = _client([])
        with pytest.raises(TagError, match="positive page size"):
            list_tags_all(client, REPO, page_size=size)  # type: ignore[arg-type]
        client.get.assert_not_called()

    def test_single_page_zero_and_last_only_remain_supported(self) -> None:
        """Do not impose all-pages validation on the existing one-page operation."""
        client = _client([_page([]), _page(["b"])])
        assert list_tags(client, REPO, page_size=0).tags == []
        assert list_tags(client, REPO, last="a").tags == ["b"]
        assert client.get.call_args_list == [
            call(PATH, params={"n": 0}),
            call(PATH, params={"last": "a"}),
        ]

    def test_exact_multiple_without_links_uses_final_empty_page(self) -> None:
        """Use exclusive tag values, including numeric tags, rather than offsets."""
        client = _client([_page(["10", "20"]), _page(["A", "b"]), _page([])])
        result = list_tags_all(client, REPO, page_size=2, last="0")
        assert result.tags == ["10", "20", "A", "b"]
        assert client.get.call_args_list == [
            call(f"{PATH}?n=2&last=0", params=None),
            call(f"{PATH}?n=2&last=20", params=None),
            call(f"{PATH}?n=2&last=b", params=None),
        ]

    @pytest.mark.parametrize("target", [
        f"{BASE}{PATH}?n=2&last=a",
        f"https://REGISTRY.EXAMPLE.COM:443{PATH}?n=2&last=a",
        f"{PATH}?n=2&last=a",
        "list?n=2&last=a",
        "?n=2&last=a",
    ])
    @pytest.mark.parametrize("first_tags", [[], ["a"]])
    def test_next_link_wins_on_short_or_empty_page(
        self, target: str, first_tags: list[str]
    ) -> None:
        """Follow relative/absolute next links even when page length is below n."""
        client = _client([
            _page(first_tags, link=f'<{target}>; rel="next"'),
            _page(["b"]),
        ])
        client.last_response = _page([], link='<https://wrong.example/>; rel="next"')
        result = list_tags_all(client, REPO, page_size=2)
        assert result.tags == first_tags + ["b"]
        assert client.get.call_args_list == [
            call(f"{PATH}?n=2", params=None),
            call(f"{PATH}?n=2&last=a", params=None),
        ]

    def test_response_url_and_linked_query_are_authoritative(self) -> None:
        """Resolve links against the actual response and preserve opaque query values."""
        target = "2?n=2&cursor=a,b;c==d&token=x%2Fy"
        header = f'<ignored>; rel="prev", <{target}>; title="a, <b>; c"; rel="next"'
        client = _client([
            _page(["a"], link=header, url=f"{BASE}/pages/1?n=5"),
            _page(["b"]),
        ])
        assert list_tags_all(client, REPO, page_size=5).tags == ["a", "b"]
        assert client.get.call_args_list[1] == call(
            "/pages/2?n=2&cursor=a,b;c==d&token=x%2Fy", params=None,
        )

    def test_linkless_fallback_uses_changed_page_size_and_keeps_query(self) -> None:
        """A linked n=2 page must not be mistaken for a short initial n=5 page."""
        client = _client([
            _page(["a"], link='<?n=2&cursor=next>; rel="next"'),
            _page(["b", "c"]),
            _page(["d"]),
        ])
        assert list_tags_all(client, REPO, page_size=5).tags == ["a", "b", "c", "d"]
        assert client.get.call_args_list == [
            call(f"{PATH}?n=5", params=None),
            call(f"{PATH}?n=2&cursor=next", params=None),
            call(f"{PATH}?n=2&cursor=next&last=c", params=None),
        ]

    def test_link_may_omit_n_when_using_last(self) -> None:
        """Without a requested limit or next link, the linked response is complete."""
        client = _client([
            _page(["a"], link='<?last=a>; rel="next"'),
            _page(["b", "c", "d"]),
        ])
        assert list_tags_all(client, REPO, page_size=1).tags == ["a", "b", "c", "d"]
        assert client.get.call_count == 2
        assert client.get.call_args == call(f"{PATH}?last=a", params=None)

    def test_absolute_link_with_empty_path_uses_registry_root(self) -> None:
        """Normalize an absolute origin-only target to an HTTP path, not a bare query."""
        client = _client([
            _page(["a"], link=f'<{BASE}?n=2&last=a>; rel="next"'),
            _page(["b"]),
        ])
        assert list_tags_all(client, REPO, page_size=2).tags == ["a", "b"]
        assert client.get.call_args == call("/?n=2&last=a", params=None)

    @pytest.mark.parametrize("query", [
        "n=0", "n=-1", "n=oops", "n=1_0", "n=+2", "n=2.0",
        "n=1&n=2", "last=a&last=b", "last=",
    ])
    def test_bad_linked_parameters_fail_before_following(self, query: str) -> None:
        """Never fetch a linked page whose pagination state is invalid or ambiguous."""
        client = _client([_page(["a"], link=f'<?{query}>; rel="next"')])
        with pytest.raises(TagError, match="pagination"):
            list_tags_all(client, REPO, page_size=2)
        assert client.get.call_count == 1

    @pytest.mark.parametrize("target", [
        "https://other.example/tags",
        "//other.example/tags",
        f"http://{REGISTRY}{PATH}",
        f"https://{REGISTRY}:444{PATH}",
        f"https://{REGISTRY}:0{PATH}",
        f"https://user:secret@{REGISTRY}{PATH}",
        f"https://{REGISTRY}:invalid{PATH}",
        "https://[broken",
        "ftp://registry.example.com/tags",
        "/\\other.example/tags",
        "/tags?cursor=raw space",
    ])
    def test_unsafe_next_links_are_rejected_before_credentials_can_be_sent(
        self, target: str
    ) -> None:
        """Reject cross-origin, downgrade, userinfo, and malformed destinations."""
        client = _client([_page(["a"], link=f'<{target}>; rel="next"')])
        with pytest.raises(TagError, match="pagination URL") as error:
            list_tags_all(client, REPO)
        assert "secret" not in str(error.value)
        assert client.get.call_count == 1

    def test_explicit_insecure_registry_can_follow_same_origin_http_links(self) -> None:
        """HTTP is allowed only when it is the configured registry origin."""
        client = _client([
            _page(["a"], link=f'<http://{REGISTRY}{PATH}?n=2&last=a>; rel="next"'),
            _page(["b"]),
        ], insecure=True)
        assert list_tags_all(client, REPO, page_size=2).tags == ["a", "b"]

    def test_url_cycle_is_bounded_even_for_empty_pages(self) -> None:
        """Stop a repeated next-page URL without requesting it a second time."""
        client = _client([
            _page([], link='<?n=2&page=2>; rel="next"'),
            _page([], link='<?n=%32#another-fragment>; rel="next"'),
        ])
        with pytest.raises(TagError, match="repeated a page"):
            list_tags_all(client, REPO, page_size=2)
        assert client.get.call_count == 2

    def test_duplicate_pages_with_changing_links_are_not_progress(self) -> None:
        """Changing an opaque URL must not hide repeatedly returned tags."""
        client = _client([
            _page(["a"], link='<?n=2&page=2>; rel="next"'),
            _page(["a"], link='<?n=2&page=3>; rel="next"'),
        ])
        with pytest.raises(TagError, match="no progress"):
            list_tags_all(client, REPO, page_size=2)
        assert client.get.call_count == 2

    def test_cursor_does_not_repeat_the_initial_cursor(self) -> None:
        """Reject a full page that would continue with an already used last value."""
        client = _client([_page(["new", "old"])])
        with pytest.raises(TagError, match="repeated a cursor"):
            list_tags_all(client, REPO, page_size=2, last="old")
        assert client.get.call_count == 1

    @pytest.mark.parametrize("bad_page", [
        _page([123]),
        _page([""]),
        _page("not a list"),
        _page([], namespace="other/repository"),
    ])
    def test_malformed_page_cannot_be_aggregated(self, bad_page: requests.Response) -> None:
        """Reject bad tag values, list shapes, or repository identity."""
        client = _client([bad_page])
        with pytest.raises(TagError):
            list_tags_all(client, REPO)
        assert client.get.call_count == 1

    def test_malformed_json_is_a_domain_error(self) -> None:
        """Use the existing tag parser error rather than leaking a decoding failure."""
        response = _page([])
        response._content = b"not-json"
        with pytest.raises(TagError, match="parse"):
            list_tags_all(_client([response]), REPO)

    @pytest.mark.parametrize(("status", "error"), [(401, AuthError), (404, TagError), (500, TagError)])
    def test_later_page_registry_errors_propagate(self, status: int, error: type[Exception]) -> None:
        """A failed later page must not become a successful partial TagList."""
        client = _client([_page(["a"]), _page([], status=status)])
        with pytest.raises(error):
            list_tags_all(client, REPO, page_size=1)
        assert client.get.call_count == 2

    def test_later_page_transport_error_propagates(self) -> None:
        """Keep transport failures explicit after a successful first page."""
        client = _client([_page(["a"]), requests.exceptions.Timeout("timed out")])
        with pytest.raises(requests.exceptions.Timeout, match="timed out"):
            list_tags_all(client, REPO, page_size=1)
        assert client.get.call_count == 2

    @pytest.mark.parametrize("middleware", [False, True])
    def test_real_transport_supports_both_client_modes(self, middleware: bool) -> None:
        """Exercise requests through RegistryClient, including its legacy URL handling."""
        pages = [_page(["a"], link='<?n=2&last=a>; rel="next"'), _page(["b"])]
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("regshape.libs.decorators.call_details.requests.request", side_effect=pages) as http,
        ):
            client = RegistryClient(TransportConfig(
                registry=REGISTRY, enable_middleware=middleware,
            ))
            assert list_tags_all(client, REPO, page_size=2).tags == ["a", "b"]
        assert [entry.args[:2] for entry in http.call_args_list] == [
            ("GET", f"{BASE}{PATH}?n=2"),
            ("GET", f"{BASE}{PATH}?n=2&last=a"),
        ]


class TestNextTagLink:
    """Check RFC link syntax beyond comma-splitting and substring matching."""

    @pytest.mark.parametrize(("header", "expected"), [
        ("", None),
        ('<previous>; rel="prev"', None),
        ('<next-page>; rel=next', "next-page"),
        ('<next-page>; rel="prev next"', "next-page"),
        ('<next-page>; rel="NEXT"', "next-page"),
        ('<ignored>; rel=prev; rel=next', None),
        ('<next-page>; extension; title="comma, and ; semicolon"; rel=next', "next-page"),
        ('<next-page?a=b,c;d=e>; title="escaped \\"quote\\""; rel=next', "next-page?a=b,c;d=e"),
        ('<ignored>; title="contains rel=next", <next-page>; rel=next', "next-page"),
        ('<same>; rel=next, <same>; rel=next', "same"),
    ])
    def test_selects_the_next_relation(self, header: str, expected: str | None) -> None:
        """Honor relation tokens and quoted parameters without altering URI contents."""
        assert _next_tag_link(header) == expected

    @pytest.mark.parametrize("header", [
        "broken",
        "<next>; rel=",
        '<next>; rel=""',
        '<next>; rel=" "',
        '<next>; rel="unterminated',
        "<next>; rel=next trailing",
        "<next>; rel",
        '<next>; anchor="/elsewhere"; rel=next',
        "<one>; rel=next, <two>; rel=next",
    ])
    def test_malformed_or_ambiguous_continuations_fail(self, header: str) -> None:
        """Do not silently treat an unusable pagination header as end of results."""
        with pytest.raises(TagError):
            _next_tag_link(header)


class TestTagPaginationCLI:
    """Preserve CLI formatting and fail before emitting incomplete results."""

    @pytest.mark.parametrize("size", ["0", "-2"])
    def test_invalid_all_size_is_a_usage_error_before_client_creation(self, size: str) -> None:
        """Reject invalid all-pages sizes before credential lookup or requests."""
        with patch("regshape.cli.tag.RegistryClient") as factory:
            result = CliRunner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--all", "--n", size,
            ])
        assert result.exit_code == 2
        assert "--n" in result.stderr
        factory.assert_not_called()

    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("write_file", [False, True])
    def test_all_preserves_options_and_output_shape(
        self, tmp_path: Path, as_json: bool, write_file: bool
    ) -> None:
        """Forward page size/cursor and serialize the complete result exactly once."""
        output = tmp_path / "tags.txt"
        tags = TagList(namespace=REPO, tags=["B", "c"])
        args = [
            "tag", "list", "-i", f"{REGISTRY}/{REPO}:ignored",
            "--all", "--n", "2", "--last", "a",
        ]
        if as_json:
            args.append("--json")
        if write_file:
            args.extend(["--output", str(output)])
        with (
            patch("regshape.cli.tag.RegistryClient") as factory,
            patch("regshape.cli.tag.list_tags_all", return_value=tags) as fetch,
            patch("regshape.cli.tag.list_tags") as single,
        ):
            result = CliRunner().invoke(regshape, args)
        assert result.exit_code == 0, result.output
        fetch.assert_called_once_with(
            client=factory.return_value, repo=REPO, page_size=2, last="a",
        )
        single.assert_not_called()
        content = output.read_text() if write_file else result.stdout
        if write_file:
            assert result.stdout == ""
        if as_json:
            assert json.loads(content) == tags.to_dict()
        else:
            assert content == "B\nc\n"

    def test_single_page_zero_remains_a_cli_option(self) -> None:
        """Keep n=0 available when automatic traversal is not requested."""
        with (
            patch("regshape.cli.tag.RegistryClient"),
            patch("regshape.cli.tag.list_tags", return_value=TagList(REPO, [])) as single,
            patch("regshape.cli.tag.list_tags_all") as all_pages,
        ):
            result = CliRunner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--n", "0",
            ])
        assert result.exit_code == 0
        assert single.call_args.kwargs["page_size"] == 0
        all_pages.assert_not_called()

    @pytest.mark.parametrize("insecure", [False, True])
    def test_complete_cli_transport_flow_and_telemetry(self, insecure: bool) -> None:
        """Traverse through the real library/client and preserve telemetry separation."""
        args = ["--insecure"] if insecure else []
        args.extend([
            "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--all", "--n", "1",
            "--json", "--debug-calls", "--time-scenarios",
        ])
        pages = [_page(["a"]), _page(["b"]), _page([])]
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("regshape.libs.decorators.call_details.requests.request", side_effect=pages) as http,
        ):
            result = CliRunner().invoke(regshape, args)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"name": REPO, "tags": ["a", "b"]}
        assert "tag list" in result.stderr
        assert "GET" in result.stderr
        scheme = "http" if insecure else "https"
        assert [entry.args[1] for entry in http.call_args_list] == [
            f"{scheme}://{REGISTRY}{PATH}?n=1",
            f"{scheme}://{REGISTRY}{PATH}?n=1&last=a",
            f"{scheme}://{REGISTRY}{PATH}?n=1&last=b",
        ]

    def test_later_page_failure_preserves_existing_output(self, tmp_path: Path) -> None:
        """Do not print success or overwrite a file with only the first page."""
        output = tmp_path / "tags.json"
        output.write_text("unchanged\n")
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("regshape.libs.decorators.call_details.requests.request",
                  side_effect=[_page(["a"]), _page([], status=500)]),
        ):
            result = CliRunner().invoke(regshape, [
                "tag", "list", "-i", f"{REGISTRY}/{REPO}", "--all", "--n", "1",
                "--json", "--output", str(output),
            ])
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Registry error" in result.stderr
        assert output.read_text() == "unchanged\n"

    def test_help_explains_all_pages_and_default(self) -> None:
        """Make the opt-in behavior and positive all-pages default discoverable."""
        result = CliRunner().invoke(regshape, ["tag", "list", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output
        assert "100" in result.output
        assert "one page only" in " ".join(result.output.split())

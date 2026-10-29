#!/usr/bin/env python3

"""
:mod:`regshape.libs.tags.operations` - OCI tag operations
==========================================================

.. module:: regshape.libs.tags.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for listing and deleting OCI image
              tags against OCI Distribution-compliant registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting is the caller's responsibility.
"""

import re
from typing import Optional
from urllib.parse import (
    unquote_plus,
    unquote_to_bytes,
    urlencode,
    urlsplit,
    urlunsplit,
)

import requests

from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, TagError
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.models.tags import TagList
from regshape.libs.refs import format_ref
from regshape.libs.transport import RegistryClient

_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_REDIRECTS = 30
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_LINK_TARGET_RE = re.compile(r"\s*<([^<>]*)>")
_LINK_PARAMETER_RE = re.compile(
    r'''\s*;\s*([!#$%&'*+.^_`|~0-9A-Za-z-]+)(?:\s*=\s*("(?:[^"\\]|\\.)*"|[^;,\s"]+))?'''
)


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def list_tags(
    client: RegistryClient,
    repo: str,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> TagList:
    """Fetch the tag list for *repo*.

    Issues a GET request to ``/v2/{repo}/tags/list``.  The 401→auth→retry
    cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param page_size: Maximum number of tags to return (OCI ``n`` query
                      parameter).  ``None`` omits the parameter.
    :param last: Lexicographic cursor for pagination (OCI ``last`` query
                 parameter).  ``None`` omits the parameter.
    :returns: A :class:`~regshape.libs.models.tags.TagList` instance.
    :raises AuthError: On authentication failure.
    :raises TagError: On a non-2xx registry response or parse failure.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/tags/list"
    params: dict = {}
    if page_size is not None:
        params["n"] = page_size
    if last is not None:
        params["last"] = last

    response = client.get(path, params=params if params else None)
    return _parse_tag_page(response, client.config.registry, repo)


@track_scenario("tag list all")
def list_tags_all(
    client: RegistryClient,
    repo: str,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> TagList:
    """Fetch all remaining tags using OCI links or exclusive tag cursors.

    Prefer ``Link: rel="next"`` even on short or empty pages. Without a next
    link, continue a full page using its last tag and the effective ``n`` of
    that request. Preserve server ordering, spelling, and linked query values.
    Follow at most 30 validated same-origin redirects per page, with automatic
    redirects disabled before the transport processes authentication.
    No consistent snapshot is guaranteed if tags change during traversal.

    :param client: Transport client for the target registry.
    :param repo: Repository namespace.
    :param page_size: Positive initial page size; ``None`` uses 100.
    :param last: Optional exclusive initial tag cursor.
    :returns: One aggregated :class:`TagList`, only after traversal succeeds.
    :raises TagError: For invalid input, malformed pages or links, unsafe
        continuation origins, repeated pages/cursors, or registry errors.
    :raises AuthError: On authentication failure on any page.
    :raises requests.exceptions.RequestException: On transport failures.
    """
    size = _DEFAULT_PAGE_SIZE if page_size is None else page_size
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise TagError("All-pages tag listing requires a positive page size")
    if last is not None and (not isinstance(last, str) or not last):
        raise TagError("The initial tag cursor must be a non-empty tag")

    params = [("n", str(size))]
    if last is not None:
        params.append(("last", last))
    url = _safe_tag_page_url(
        client.base_url,
        f"/v2/{repo}/tags/list?{urlencode(params)}",
        client.base_url,
    )
    tags: list[str] = []
    seen_tags: set[str] = set()
    seen_pages: set[tuple[str, Optional[int], Optional[str], tuple[str, ...]]] = set()
    used_cursors: set[str] = set()
    redirects = 0

    while True:
        parts = urlsplit(url)
        key = _tag_page_key(url)
        if key in seen_pages:
            raise TagError(f"Tag pagination repeated a page for {client.config.registry}/{repo}")
        seen_pages.add(key)

        # Relative requests also work with the client's legacy transport mode.
        path = urlunsplit(("", "", parts.path, parts.query, ""))
        response = client.get(path, params=None, allow_redirects=False)
        response_url = _safe_tag_page_url(client.base_url, response.url or url, url)
        query, effective_size, cursor = _tag_page_parameters(response_url)
        response_parts = urlsplit(response_url)
        response_key = _tag_page_key(response_url)
        if response_key != key and response_key in seen_pages:
            raise TagError(f"Tag pagination revisited a page for {client.config.registry}/{repo}")
        seen_pages.add(response_key)
        if cursor is not None:
            used_cursors.add(cursor)

        if response.status_code in _REDIRECT_STATUSES:
            if redirects >= _MAX_PAGE_REDIRECTS:
                raise TagError("Tag pagination redirect limit exceeded")
            location = response.headers.get("Location")
            if not location:
                raise TagError("Tag pagination redirect is missing Location")
            url = _safe_tag_page_url(client.base_url, location, response_url)
            redirects += 1
            continue
        redirects = 0
        page = _parse_tag_page(response, client.config.registry, repo)
        if page.namespace != repo:
            raise TagError("Tag pagination returned a different repository namespace")
        if any(not isinstance(tag, str) or not tag for tag in page.tags):
            raise TagError("Tag pagination returned an invalid tag value")
        if page.tags and all(tag in seen_tags for tag in page.tags):
            raise TagError(f"Tag pagination made no progress for {client.config.registry}/{repo}")
        tags.extend(page.tags)
        seen_tags.update(page.tags)

        next_link = _next_tag_link(response.headers.get("Link", ""))
        if next_link is not None:
            url = _safe_tag_page_url(client.base_url, next_link, response_url)
            continue
        if effective_size is None or len(page.tags) < effective_size:
            return TagList(namespace=repo, tags=tags)

        next_cursor = page.tags[-1]
        if next_cursor in used_cursors:
            raise TagError(f"Tag pagination repeated a cursor for {client.config.registry}/{repo}")
        fields = [field for name, field in query if name != b"last"]
        fields.append(urlencode({"last": next_cursor}))
        url = urlunsplit(response_parts._replace(query="&".join(fields)))


@track_time
def delete_tag(
    client: RegistryClient,
    repo: str,
    tag: str,
) -> None:
    """Delete *tag* from *repo*.

    The OCI Distribution Spec routes tag deletion through the manifests
    endpoint: ``DELETE /v2/{repo}/manifests/{tag}``.  The 401→auth→retry
    cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param tag: Tag name to delete (must not be a digest).
    :raises AuthError: On authentication failure.
    :raises TagError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/manifests/{tag}"
    response = client.delete(path)
    _raise_for_delete_error(response, client.config.registry, repo, tag)


# ===========================================================================
# Private error helpers
# ===========================================================================


def _parse_tag_page(
    response: requests.Response,
    registry: str,
    repo: str,
) -> TagList:
    """Parse one page using the shared tag error mapping.

    The caller retains the response for pagination and validates any redirects
    before parsing. Parsing failures use :class:`TagError`; authentication
    failures retain their :class:`AuthError` mapping.
    """
    _raise_for_list_error(response, registry, repo)
    try:
        page = TagList.from_json(response.text)
    except TagError:
        raise
    except (TypeError, ValueError) as exc:
        raise TagError(
            f"Failed to parse tag-list response from {registry}/{repo}",
            str(exc),
        ) from exc
    return page


def _tag_url_origin(url: str) -> tuple[str, str, int]:
    """Return a normalized HTTP origin; reject userinfo, bad ports, and schemes."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Pagination requires an HTTP or HTTPS URL with a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Pagination URLs must not contain credentials")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname.encode("idna").decode("ascii").lower(), port


def _safe_tag_page_url(base_url: str, target: str, context_url: str) -> str:
    """Resolve a continuation against its response URL and enforce same origin.

    Reject userinfo, cross-origin links, scheme changes, and ambiguous control
    characters before dispatch. Fragments are not sent in HTTP requests and
    are removed so they cannot conceal pagination cycles. Invalid URLs raise
    :class:`TagError` without exposing credential-bearing link values, parser
    diagnostics, or the original exception chain.
    """
    try:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in target):
            raise ValueError("Pagination URLs must not contain whitespace or control characters")
        if "\\" in target:
            raise ValueError("Pagination URLs must not contain backslashes")
        resolved = _resolve_tag_reference(context_url, target)
        if _tag_url_origin(resolved) != _tag_url_origin(base_url):
            raise ValueError("Pagination must remain on the configured registry origin")
        parts = urlsplit(resolved)
        return urlunsplit(parts._replace(path=parts.path or "/"))
    except ValueError:
        raise TagError(
            "Unsafe or invalid tag pagination URL",
            "URLs must be valid, credential-free, and on the configured registry origin",
        ) from None


def _resolve_tag_reference(context_url: str, target: str) -> str:
    """Resolve an RFC 3986 reference without collapsing empty path segments.

    Unlike ``urljoin()``, preserve empty segments in both base and reference
    paths and distinguish an absent query from an explicitly empty query.
    Remove fragments because they are not part of an HTTP request target.
    """
    base = urlsplit(context_url)
    reference = urlsplit(target)
    if reference.scheme:
        resolved = reference._replace(path=_remove_tag_dot_segments(reference.path))
    elif reference.netloc or target.startswith("//"):
        resolved = reference._replace(
            scheme=base.scheme,
            path=_remove_tag_dot_segments(reference.path),
        )
    else:
        if not reference.path:
            path = base.path
            query = reference.query if "?" in target.split("#", 1)[0] else base.query
        else:
            path = reference.path
            if not path.startswith("/"):
                path = base.path.rpartition("/")[0] + "/" + path
            path = _remove_tag_dot_segments(path)
            query = reference.query
        resolved = reference._replace(
            scheme=base.scheme, netloc=base.netloc, path=path, query=query,
        )
    return urlunsplit(resolved._replace(fragment=""))


def _remove_tag_dot_segments(path: str) -> str:
    """Apply RFC 3986 section 5.2.4 to a path, retaining ordinary empty segments."""
    output = ""
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./"):
            path = path[2:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = path[3:]
            output = output.rpartition("/")[0]
        elif path == "/..":
            path = "/"
            output = output.rpartition("/")[0]
        elif path in (".", ".."):
            path = ""
        else:
            boundary = path.find("/", 1 if path.startswith("/") else 0)
            if boundary == -1:
                output += path
                path = ""
            else:
                output += path[:boundary]
                path = path[boundary:]
    return output


def _tag_page_parameters(
    url: str,
) -> tuple[list[tuple[bytes, str]], Optional[int], Optional[str]]:
    """Return raw query fields, the effective page size, and the tag cursor.

    Decode field names as bytes only to identify ``n`` and ``last``; leave
    opaque values and their encoding untouched. Decode pagination values
    strictly, rejecting invalid encodings, duplicate parameters, and
    non-positive sizes rather than corrupting or truncating a traversal.
    """
    raw_query = urlsplit(url).query
    fields = raw_query.split("&") if raw_query else []
    query = [
        (unquote_to_bytes(field.partition("=")[0].replace("+", " ")), field)
        for field in fields
    ]
    try:
        sizes = [
            unquote_plus(field.partition("=")[2], errors="strict")
            for name, field in query if name == b"n"
        ]
        cursors = [
            unquote_plus(field.partition("=")[2], errors="strict")
            for name, field in query if name == b"last"
        ]
    except UnicodeError as exc:
        raise TagError("Invalid tag pagination parameter encoding") from exc
    if len(sizes) > 1 or len(cursors) > 1:
        raise TagError("Ambiguous tag pagination parameters")
    size = None
    if sizes:
        if not sizes[0].isascii() or not sizes[0].isdecimal():
            raise TagError("Tag pagination page size must be a positive integer")
        try:
            size = int(sizes[0])
        except ValueError as exc:
            raise TagError("Tag pagination page size must be a positive integer") from exc
        if size <= 0:
            raise TagError("Tag pagination page size must be a positive integer")
    if cursors and not cursors[0]:
        raise TagError("Tag pagination cursor must be a non-empty tag")
    return query, size, cursors[0] if cursors else None


def _tag_page_key(
    url: str,
) -> tuple[str, Optional[int], Optional[str], tuple[str, ...]]:
    """Identify a page without conflating distinct opaque continuation bytes.

    Normalize only the understood pagination parameters. Opaque fields retain
    their original order and encoding, including repeated keys and bare flags.
    Invalid pagination parameters propagate as :class:`TagError`.
    """
    query, size, cursor = _tag_page_parameters(url)
    opaque_fields = tuple(field for name, field in query if name not in (b"n", b"last"))
    return urlsplit(url).path, size, cursor, opaque_fields


def _next_tag_link(header: str) -> Optional[str]:
    """Select an RFC 5988 next link without splitting quoted values or URI commas.

    Unknown extension parameters are ignored. Honor only the first ``rel``
    parameter of each link, as required by the RFC. Malformed headers,
    conflicting next links, and unsupported anchored next links raise
    :class:`TagError` rather than risking incomplete or misdirected traversal.
    """
    position = 0
    next_url = None
    while position < len(header):
        while position < len(header) and header[position] in " \t,":
            position += 1
        if position == len(header):
            break
        target = _LINK_TARGET_RE.match(header, position)
        if target is None:
            raise TagError("Malformed tag pagination Link header")
        position = target.end()
        relations = None
        anchored = False
        while True:
            parameter = _LINK_PARAMETER_RE.match(header, position)
            if parameter is None:
                break
            name, value = parameter.groups()
            name = name.lower()
            if value is not None and value.startswith('"'):
                value = re.sub(r"\\(.)", r"\1", value[1:-1])
            if name == "rel" and relations is None:
                if value is None or not value.strip():
                    raise TagError("Tag pagination Link relation is missing its value")
                relations = value
            anchored = anchored or name == "anchor"
            position = parameter.end()
        while position < len(header) and header[position].isspace():
            position += 1
        if position < len(header) and header[position] != ",":
            raise TagError("Malformed tag pagination Link parameters")
        if relations is not None and "next" in relations.lower().split():
            if anchored:
                raise TagError("Anchored tag pagination links are not supported")
            if next_url is not None and next_url != target.group(1):
                raise TagError("Ambiguous next links in tag pagination response")
            next_url = target.group(1)
        position += 1
    return next_url


def _raise_for_list_error(
    response: requests.Response,
    registry: str,
    repo: str,
) -> None:
    """Raise on non-2xx responses for a tag-list operation.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.TagError` "Repository not found"
    * other → :class:`~regshape.libs.errors.TagError` generic

    :raises AuthError: On 401.
    :raises TagError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise TagError(
            f"Repository not found: {registry}/{repo}",
            detail or "HTTP 404",
        )
    raise TagError(
        f"Registry error for {registry}/{repo}",
        detail or f"HTTP {response.status_code}",
    )


def _raise_for_delete_error(
    response: requests.Response,
    registry: str,
    repo: str,
    tag: str,
) -> None:
    """Raise on non-2xx responses for a tag-delete operation.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.TagError` "Tag not found"
    * 400 / 405 → :class:`~regshape.libs.errors.TagError` "not supported"
    * other → :class:`~regshape.libs.errors.TagError` generic

    :raises AuthError: On 401.
    :raises TagError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )
    ref_str = format_ref(registry, repo, tag)

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise TagError(
            f"Tag not found: {ref_str}",
            detail or "HTTP 404",
        )
    if response.status_code in (400, 405):
        raise TagError(
            "Tag deletion is not supported by this registry",
            detail or f"HTTP {response.status_code}",
        )
    raise TagError(
        f"Registry error for {ref_str}",
        detail or f"HTTP {response.status_code}",
    )

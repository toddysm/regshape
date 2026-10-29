# Operations: Tags

## Overview

This spec documents the domain operations layer for OCI tag interactions
in `src/regshape/libs/tags/operations.py`.

The tags operations layer sits between the CLI and the foundation layer.
It never calls `requests` directly — all HTTP traffic goes through a
`RegistryClient` instance provided by the caller.

### Endpoints

| Operation | Method | Endpoint |
|---|---|---|
| List tags | `GET` | `/v2/{repo}/tags/list[?n=<n>&last=<last>]` |
| List all remaining tags | `GET` | Same endpoint, following links or tag cursors |
| Delete tag | `DELETE` | `/v2/{repo}/manifests/{tag}` |

---

## Module Structure

```
src/regshape/libs/tags/
├── __init__.py        # Package init; re-exports public symbols
└── operations.py      # Public domain operations + private error helpers
```

---

## Public Operations

### `list_tags`

```python
@track_time
def list_tags(
    client: RegistryClient,
    repo: str,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> TagList:
```

Fetches the tag list for a repository (single page).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name (e.g. `"myrepo/myimage"`) |
| `page_size` | `Optional[int]` | `None` | OCI `n` query parameter; omitted when `None` |
| `last` | `Optional[str]` | `None` | Lexicographic cursor for pagination; omitted when `None` |

**Returns:** `TagList`

**Behaviour:**

1. Build path `"/v2/{repo}/tags/list"`.
2. Append `n=<page_size>` and/or `last=<last>` when provided.
3. Call `client.get(path, params=params)`.
4. Pass response to `_raise_for_list_error`.
5. Deserialize and return `TagList.from_json(response.text)`.
   Re-raises `TagError` directly; wraps field validation errors in `TagError`.

**Decorator:** `@track_time`

---

### `list_tags_all`

```python
@track_scenario("tag list all")
def list_tags_all(
    client: RegistryClient,
    repo: str,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> TagList:
```

Returns one `TagList` containing all remaining pages, in server order and with
tag spelling unchanged. `page_size=None` uses 100 per request. An explicit size
must be a positive integer; invalid sizes fail before network access. `last`
is an optional exclusive starting tag value, not an offset.

The protocol is defined by [OCI Distribution Specification v1.1.1, Listing
Tags](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#listing-tags).
The helper and its default size are RegShape conveniences, not OCI requirements.

**Traversal:**

1. Request the first page with `n` and, when provided, `last`.
2. Read pagination metadata from that page's returned response, not a separate
   mutable client response slot.
3. Prefer its `Link` with relation `next`, including on a short or empty page.
   Parse [RFC 5988](https://www.rfc-editor.org/rfc/rfc5988.html) link parameters
   without splitting commas inside URI references or quoted values. Resolve
   relative targets against the actual response URL and preserve linked query
   values and path. Follow [RFC 3986 reference resolution](https://www.rfc-editor.org/rfc/rfc3986.html#section-5.2)
   for dot segments and query inheritance without collapsing empty path
   segments: `pages/token//part` and `pages/token/part` are distinct paths.
4. If there is no next link and the current request has a positive `n`, continue
   a full-sized page using its last tag as the next exclusive cursor. Preserve
   other query fields byte-for-byte, including opaque percent-encoded values
   and bare flags; replace only `last`. Stop on a short/empty unlinked page;
   exact multiples may need a final empty request.
5. Use the effective `n` of each request, not the initial size. A linked request
   may change `n` or omit it: with no `n` and no next link, its response is the
   remaining unbounded list.
6. Aggregate pages only on success. Propagate later-page failures without
   returning a successful partial result.

`Link` is optional in the OCI specification. Consequently, absence of a link
alone does not terminate a full-sized cursor-paginated page. Conversely, page
length alone cannot terminate a page with a next link. OCI permits `n=0` for
single-page listing (empty result, no link); `list_tags()` retains that behavior,
while all-pages mode rejects it as a client input policy.

**Client safety policies:**

- Continuations must remain on the configured scheme, hostname, and effective
  port. Reject cross-origin links, scheme changes, embedded user credentials,
  malformed ports, raw whitespace/control characters, and backslashes before
  dispatch. Explicitly configured HTTP registries remain supported.
  Authorities must be ASCII: use punycode for international domain names.
  Reject Unicode authorities rather than allowing differences between
  validator and HTTP-client domain-name mappings to bypass origin checks.
- Disable automatic redirects on pagination requests, including authentication
  probes and retries in both transport modes. Validate each `Location` before
  following a 301/302/303/307/308 redirect so a foreign authentication challenge
  cannot receive stored credentials. Follow at most 30 redirects per page;
  reject missing locations and redirect cycles. Ordinary transport requests
  retain their existing automatic-redirect default; cached responses are
  separated by redirect policy so an auto-follow result cannot bypass an
  explicit opt-out. Track redirect hops separately from completed tag pages,
  allowing a same-origin redirect to canonicalize query ordering without being
  mistaken for a repeated page.
- If challenge discovery at `/v2/` redirects, both transport modes validate
  and follow same-origin probe redirects explicitly before accepting a
  challenge. Probe chains have the same cycle checks and 30-hop bound; unsafe
  targets fail before any credential exchange.
- Sanitize URL-parser failures and suppress their raw exception context:
  malformed URI authorities may contain credentials even when parsing fails
  before the userinfo check. Requests may prepare `Response.next` even when
  automatic redirects are disabled, so the shared request wrapper converts
  pre-return parsing failures into sanitized transport errors before logging.
- Remove fragments for request/cycle identity. Detect repeated URLs/cursors and
  nonempty pages containing no new tags; raise `TagError` rather than looping.
  Empty pages with distinct valid continuation links may still be followed.
  Normalize only known pagination parameters in page identity; opaque values
  such as `%FF` and `%FE` must remain distinct.
- Fail with `TagError` on the **tenth consecutive empty page with a next link**,
  before requesting its continuation, even if opaque links keep changing.
  Receiving tags resets the counter; an empty page without a next link completes
  normally, including after nine empty linked pages. Redirect responses neither
  increment nor reset this counter. The limit is a RegShape safety policy, not
  an OCI requirement or a cap on total pages; failure returns no partial result.
- Reject malformed or ambiguous pagination headers/parameters, conflicting next
  links, and anchored next links whose alternate context is not supported.
- Reject invalid tag values and pages belonging to another repository.
- Reject repeated tags within a page or across partially overlapping pages.
  This is an all-pages consistency policy, not silent deduplication.
- Reject any returned tag equal to an initial or linked exclusive cursor,
  even on a short page. Retain requested cursor boundaries across redirects.

There is no snapshot guarantee if tags change while listing. Aggregation does
not re-sort, lower-case, or silently deduplicate returned tags. Concurrent
changes that cause overlapping pages fail explicitly; retry against a stable
repository rather than treating a duplicated list as a complete result.

**Returns:** `TagList`

**Errors:** `TagError` for invalid input, unsafe/non-progressing pagination,
malformed responses, or registry errors; `AuthError` for authentication failures;
transport exceptions propagate unchanged.

---

### `delete_tag`

```python
@track_time
def delete_tag(
    client: RegistryClient,
    repo: str,
    tag: str,
) -> None:
```

Deletes a tag from a repository. Per the OCI Distribution Spec, tag deletion
is routed through the manifests endpoint.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `tag` | `str` | required | Tag name to delete (must not be a digest) |

**Returns:** `None`

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{tag}"`.
2. Call `client.delete(path)`.
3. Pass response to `_raise_for_delete_error`.

**Decorator:** `@track_time`

---

## Private Helpers

The single-page and all-pages operations share `_parse_tag_page()` for
response parsing/error handling. `_next_tag_link()`, `_tag_page_parameters()`,
`_tag_page_key()`, and `_safe_tag_page_url()` implement the tag-specific
continuation rules above. The shared
[`resolve_registry_url()`](../../src/regshape/libs/transport/urls.py) utility
preserves URI semantics and enforces the same origin policy for pagination
and authentication probes.
Requests are dispatched as registry-relative paths so both middleware and legacy
transport modes work.

### `_raise_for_list_error`

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `TagError` | `"Repository not found: {registry}/{repo}"` |
| other non-2xx | `TagError` | `"Registry error for {registry}/{repo}: HTTP {status}"` |

### `_raise_for_delete_error`

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `TagError` | `"Tag not found: {ref}"` |
| `400`, `405` | `TagError` | `"Tag deletion is not supported by this registry"` |
| other non-2xx | `TagError` | `"Registry error for {ref}: HTTP {status}"` |

Both helpers populate the `detail` field from
`OciErrorResponse.from_response(response).first_detail()`, falling back to
the first 200 characters of `response.text`.

---

## Telemetry

| Decorator | Applied to |
|---|---|
| `@track_time` | `list_tags`, `delete_tag` |
| `@track_scenario` | `list_tags_all` |

All page requests still use the shared transport's request tracing and metrics.

---

## Error Handling Summary

| Condition | Exception | Raised by |
|---|---|---|
| `401` on any call | `AuthError` | `_raise_for_list_error` / `_raise_for_delete_error` |
| `404` on list | `TagError` "Repository not found" | `_raise_for_list_error` |
| `404` on delete | `TagError` "Tag not found" | `_raise_for_delete_error` |
| `400` / `405` on delete | `TagError` "not supported" | `_raise_for_delete_error` |
| Malformed JSON body | `TagError` | `TagList.from_json` (re-raised) |
| Transport / connection failure | `requests.exceptions.RequestException` | propagated as-is |
| Non-positive all-pages size, unsafe link, cycle, or invalid page | `TagError` | `list_tags_all` / pagination helpers |

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `AuthError`, `TagError`
- `regshape.libs.models.tags` — `TagList`
- `regshape.libs.models.error` — `OciErrorResponse`
- `regshape.libs.refs` — `format_ref`
- `regshape.libs.transport` — `RegistryClient`
- `regshape.libs.decorators.timing` — `track_time`
- `regshape.libs.decorators.scenario` — `track_scenario`

**External:**
- `requests` — `requests.Response` type annotation only

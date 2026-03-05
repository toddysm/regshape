# Operations: Referrers

## Overview

This spec documents the domain operations layer for OCI referrers interactions
in `src/regshape/libs/referrers/operations.py`.

The OCI Referrers API (`GET /v2/<name>/referrers/<digest>`) returns an Image
Index whose `manifests` array lists all manifests that have a `subject` field
pointing to the given digest. This is the primary mechanism for discovering
supply-chain artifacts (SBOMs, signatures, attestations, etc.) attached to an
image.

The referrers operations layer sits between the CLI and the transport layer.
It never calls `requests` directly — all HTTP traffic goes through a
`RegistryClient` instance provided by the caller.

### Endpoints

| Operation | Method | Endpoint |
|---|---|---|
| List referrers (single page) | `GET` | `/v2/{repo}/referrers/{digest}[?artifactType=<type>]` |
| List referrers (all pages) | `GET` | `/v2/{repo}/referrers/{digest}[?artifactType=<type>]` (follows `Link` headers) |

---

## Module Structure

```
src/regshape/libs/referrers/
├── __init__.py        # Package init; re-exports public symbols
└── operations.py      # Public domain operations + private error helpers
```

---

## Public Operations

### `list_referrers`

```python
@track_time
def list_referrers(
    client: RegistryClient,
    repo: str,
    digest: str,
    artifact_type: Optional[str] = None,
) -> ReferrerList:
```

Fetches the list of referrers for the manifest identified by *digest* in
*repo*.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name (e.g. `"myrepo/myimage"`) |
| `digest` | `str` | required | Manifest digest in `algorithm:hex` form (e.g. `sha256:abc123...`) |
| `artifact_type` | `Optional[str]` | `None` | Filter results to this artifact type; omitted when `None` |

**Returns:** `ReferrerList`

**Behaviour:**

1. Build path `"/v2/{repo}/referrers/{digest}"`.
2. Append `?artifactType=<artifact_type>` when provided.
3. Call `client.get(path, params=params)`.
4. Pass response to `_raise_for_list_error`.
5. Check the `OCI-Filters-Applied` response header. If `artifactType` was
   requested but the header is absent, perform client-side filtering on the
   deserialized `ReferrerList.manifests` array (keep only entries where
   `Descriptor.artifact_type` matches the requested value).
6. Deserialize and return `ReferrerList.from_json(response.text)`.
   Re-raises `ReferrerError` directly; wraps all other exceptions in
   `ReferrerError`.

**Decorator:** `@track_time`

---

### `list_referrers_all`

```python
@track_scenario("referrer list all")
def list_referrers_all(
    client: RegistryClient,
    repo: str,
    digest: str,
    artifact_type: Optional[str] = None,
) -> ReferrerList:
```

Fetches all pages of the referrer list and returns them merged into a single
`ReferrerList`.

Follows `Link: rel="next"` response headers until all pages have been
retrieved, then returns a single `ReferrerList` whose `manifests` list is
the ordered concatenation of every page.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name (e.g. `"myrepo/myimage"`) |
| `digest` | `str` | required | Manifest digest in `algorithm:hex` form |
| `artifact_type` | `Optional[str]` | `None` | Filter results to this artifact type; omitted when `None` |

**Returns:** `ReferrerList` — merged result containing all referrers across
all pages.

**Behaviour:**

1. Call `list_referrers(client, repo, digest, artifact_type)` to fetch the
   first page.
2. Accumulate `page.manifests` into a running list.
3. Inspect the `Link` header from `client.last_response` using
   `_parse_next_cursor()`.
4. If a next-page cursor is found, issue `client.get()` with the extracted
   URL and repeat from step 2.
5. When no `Link: rel="next"` header is present, merge all accumulated
   descriptors into a single `ReferrerList` and return it.
6. Client-side `artifactType` filtering (see below) is applied once on
   the final merged result, not per-page, to avoid redundant work.

**Decorator:** `@track_scenario("referrer list all")`

**Note:** The first page is fetched via `list_referrers()` (which has
`@track_time`). Subsequent pages are fetched with direct `client.get()`
calls to avoid double-timing. The overall multi-page operation is timed
by the `@track_scenario` decorator.

---

### Client-Side Filtering

The OCI Distribution Spec defines the `artifactType` query parameter and the
`OCI-Filters-Applied` response header. Registries that support server-side
filtering include `OCI-Filters-Applied: artifactType` in their response.
Registries that do not support server-side filtering return the full unfiltered
list without the header.

The operations layer handles both cases transparently:

1. Always pass `artifactType` as a query parameter when the caller provides it.
2. On response, inspect the `OCI-Filters-Applied` header:
   - **Present and contains `artifactType`**: Trust the response — server
     already filtered.
   - **Absent or does not contain `artifactType`**: Apply client-side
     filtering — iterate `ReferrerList.manifests` and retain only descriptors
     where `descriptor.artifact_type == artifact_type`.

This ensures correct behaviour across all registry implementations.

---

## Private Helpers

### `_parse_next_cursor`

Parses the OCI `Link` response header and returns the URL for the next page.

The OCI pagination `Link` header format is:

```
Link: </v2/<name>/referrers/<digest>?n=100&last=sha256:abc...>; rel="next"
```

Extracts the URL inside `<...>`, checks for `rel="next"`, and returns the
full relative URL path (including query string) for the next page.

| Input | Output |
|---|---|
| Header present with `rel="next"` | Relative URL string for next page |
| Header absent or no `rel="next"` | `None` |

This follows the same pattern as `catalog.operations._parse_next_url`.

---

### `_raise_for_list_error`

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `ReferrerError` | `"Manifest not found: {registry}/{repo}@{digest}"` |
| other non-2xx | `ReferrerError` | `"Registry error for {registry}/{repo}@{digest}: HTTP {status}"` |

The helper populates the `detail` field from
`OciErrorResponse.from_response(response).first_detail()`, falling back to
the first 200 characters of `response.text`.

---

## Telemetry

| Decorator | Applied to |
|---|---|
| `@track_time` | `list_referrers` |
| `@track_scenario("referrer list all")` | `list_referrers_all` |

---

## Error Handling Summary

| Condition | Exception | Raised by |
|---|---|---|
| `401` on list | `AuthError` | `_raise_for_list_error` |
| `404` on list | `ReferrerError` "Manifest not found" | `_raise_for_list_error` |
| Other non-2xx | `ReferrerError` generic | `_raise_for_list_error` |
| Malformed JSON body | `ReferrerError` | `ReferrerList.from_json` (re-raised) |
| Transport / connection failure | `requests.exceptions.RequestException` | propagated as-is |

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `AuthError`, `ReferrerError` (new)
- `regshape.libs.models.referrer` — `ReferrerList` (new)
- `regshape.libs.models.error` — `OciErrorResponse`
- `regshape.libs.transport` — `RegistryClient`
- `regshape.libs.decorators.timing` — `track_time`
- `regshape.libs.decorators.scenario` — `track_scenario`

**External:**
- `requests` — `requests.Response` type annotation only
- `re` — regex for `Link` header parsing
- `urllib.parse` — `urlparse`, `parse_qs` for cursor extraction

# Operations: Catalog

## Overview

This spec defines the domain operations layer for OCI repository-catalog
interactions in `src/regshape/libs/catalog/operations.py`.

The catalog operations layer sits between the CLI and the foundation layer.
It never calls `requests` directly — all HTTP traffic goes through a
`RegistryClient` instance provided by the caller, matching the pattern
established by `libs/tags/operations.py` and `libs/blobs/operations.py`.

### Endpoint

```
GET /v2/_catalog[?n=<count>&last=<last-repo>]
```

The endpoint is non-standard (not part of the OCI Distribution Spec v1.1
normative table) but widely implemented. See `specs/models/catalog.md` for
the data model.

---

## Module Structure

```
src/regshape/libs/catalog/
├── __init__.py        # Package init; re-exports public symbols
└── operations.py      # Public domain operations + private error helpers
```

No additional sub-modules are needed at this stage.

---

## Public Operations

### `list_catalog`

```python
@track_time
def list_catalog(
    client: RegistryClient,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> RepositoryCatalog:
```

Fetches a single page of the repository catalog.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client for the target registry |
| `page_size` | `Optional[int]` | `None` | OCI `n` query parameter; omitted when `None` |
| `last` | `Optional[str]` | `None` | OCI `last` query parameter (pagination cursor); omitted when `None` |

**Returns:** `RepositoryCatalog` for the page.

**Behaviour:**

1. Build GET path `"/v2/_catalog"`.
2. Append `n=<page_size>` and / or `last=<last>` query parameters when
   provided.
3. Call `client.get(path, params=params)`.
4. Pass the response to `_raise_for_catalog_error(response, registry)`.
5. Deserialize and return `RepositoryCatalog.from_json(response.text)`.

**Decorator:** `@track_time` (records per-call wall-clock time for
`--time-methods` telemetry output).

---

### `list_catalog_all`

```python
@track_scenario
def list_catalog_all(
    client: RegistryClient,
    page_size: Optional[int] = None,
) -> RepositoryCatalog:
```

Fetches all pages of the repository catalog and returns them merged into a
single `RepositoryCatalog`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `page_size` | `Optional[int]` | `None` | `n` parameter passed to each `list_catalog` call; controls page granularity, not the total result size |

**Returns:** A single `RepositoryCatalog` whose `repositories` list is the
ordered concatenation of all pages.

**Behaviour:**

1. Call `list_catalog(client, page_size=page_size)` to get the first page.
2. Inspect the response `Link` header (exposed via
   `client.last_response.headers`) for a `rel="next"` cursor.
3. If a next cursor exists, call `list_catalog(client, page_size=page_size,
   last=<cursor>)` and append results.
4. Repeat until no `Link: rel="next"` header is present.
5. Return a single `RepositoryCatalog(repositories=accumulated_list)`.

**Decorator:** `@track_scenario` (records total wall-clock time across all
pages for `--time-scenarios` telemetry output).

**Note on `Link` header parsing**: The cursor value is extracted from the
`Link` header using the same utility used by tag and blob pagination. Delegate
to `_parse_next_cursor(headers)` (private helper, see below).

---

## Private Helpers

### `_raise_for_catalog_error`

```python
def _raise_for_catalog_error(
    response: requests.Response,
    registry: str,
) -> None:
```

Inspects HTTP status and raises the appropriate typed error. Does nothing for
2xx responses.

| HTTP status | Exception raised | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `403` | `AuthError` | `"Authorisation denied for {registry}"` |
| `404` | `CatalogNotSupportedError` | `"Registry does not support the catalog API: {registry}"` |
| `405` | `CatalogNotSupportedError` | `"Registry does not support the catalog API: {registry}"` |
| other non-2xx | `CatalogError` | `"Registry error for {registry}: HTTP {status}"` |

The `detail` field of each error is populated from
`OciErrorResponse.from_response(response).first_detail()`, falling back to
the first 200 characters of `response.text`.

**Rationale for `401` on `_catalog`:** A `401` returned specifically on
`GET /v2/_catalog` after a successful `GET /v2/` challenge is treated as
"endpoint not available" rather than an auth failure, because some registries
use `401` to obscure endpoint existence. However, since the operations layer
cannot distinguish this case without a prior successful auth round-trip, `401`
on catalog is mapped to `AuthError` — the operations layer lets the auth
cycle in `RegistryClient` handle the retry. If the retry also yields `401`,
the `AuthError` propagates normally to the CLI. The CLI spec must document
that `401` on catalog MAY mean the endpoint is unsupported (see Open
Questions).

---

### `_parse_next_cursor`

```python
def _parse_next_cursor(headers: dict) -> Optional[str]:
```

Parses the OCI `Link` response header and returns the `last` cursor value for
the next page, or `None` if there is no next page.

The `Link` header format is:

```
Link: </v2/_catalog?last=myrepo/myimage&n=100>; rel="next"
```

The function extracts the URL inside `<...>`, parses its query string, and
returns the value of the `last` parameter.

Returns `None` if:
- The `Link` header is absent.
- No `rel="next"` link relation is present.
- The `last` parameter is absent from the next URL.

---

## Telemetry

| Decorator | Applied to | Controls |
|---|---|---|
| `@track_time` | `list_catalog` | Per-call timing; emitted with `--time-methods` |
| `@track_scenario` | `list_catalog_all` | Total scenario timing; emitted with `--time-scenarios` |

Imports:

```python
from regshape.libs.decorators.timing import track_time
from regshape.libs.decorators.scenario import track_scenario
```

---

## Error Handling Summary

| Condition | Exception | Layer |
|---|---|---|
| `401` on `GET /v2/_catalog` | `AuthError` | `_raise_for_catalog_error` |
| `403` on `GET /v2/_catalog` | `AuthError` | `_raise_for_catalog_error` |
| `404` or `405` | `CatalogNotSupportedError` | `_raise_for_catalog_error` |
| Other non-2xx | `CatalogError` | `_raise_for_catalog_error` |
| Malformed JSON body | `CatalogError` | `RepositoryCatalog.from_json` |
| Transport / connection failure | `requests.exceptions.RequestException` | propagated as-is |

`CatalogNotSupportedError` is a subclass of `CatalogError`, so callers that
only need to distinguish "something went wrong" from "success" can catch the
broader `CatalogError`. Callers that want to surface a specific "not
supported" message to users should catch `CatalogNotSupportedError`
separately.

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `AuthError`, `CatalogError`, `CatalogNotSupportedError`
- `regshape.libs.models.catalog` — `RepositoryCatalog`
- `regshape.libs.models.error` — `OciErrorResponse`
- `regshape.libs.transport` — `RegistryClient`
- `regshape.libs.decorators.timing` — `track_time`
- `regshape.libs.decorators.scenario` — `track_scenario`

**External:**
- `requests` — `requests.Response` type annotation only; no direct HTTP calls

---

## Open Questions

- [ ] Should `list_catalog_all` have a `max_repos` safety cap to prevent
  unbounded accumulation on very large registries? Current proposal: no cap
  by default; the CLI may expose `--max` as a hard limit before calling
  `list_catalog_all`.
- [ ] The `401` ambiguity: should the CLI or operations layer make a
  second attempt with anonymous access before surfacing a
  `CatalogNotSupportedError`? Current proposal: leave it to the CLI
  spec — the operations layer always raises `AuthError` on `401`.
- [ ] Should `list_catalog_all` be removed and pagination left entirely to
  the CLI (which calls `list_catalog` in a loop)? Current proposal: keep
  `list_catalog_all` — it mirrors the pattern available for other endpoint
  groups and encapsulates the `Link` header parsing.

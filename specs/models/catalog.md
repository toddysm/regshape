# Data Model: Catalog

## Overview

This spec defines the data model for OCI repository-catalog responses in
`src/regshape/libs/models/catalog.py`.

The registry exposes a single endpoint for listing all repositories hosted on
a registry:

```
GET /v2/_catalog[?n=<count>&last=<last-repo>]
```

This endpoint is non-standard in the OCI Distribution Spec (it is absent from
the normative "end-N" table) but is widely implemented, including by Docker
Hub, Amazon ECR, Azure Container Registry, and most self-hosted registries.

The response body carries an array of repository name strings. The data model
consists of a single type, `RepositoryCatalog`, that wraps the wire response
and normalises edge cases (a `null` or missing `repositories` array is treated
as an empty list, mirroring the same normalisation applied to `TagList.tags`).

---

## Module Structure

```
src/regshape/libs/models/
├── __init__.py      # Updated: exports RepositoryCatalog
├── blob.py
├── catalog.py       # NEW: RepositoryCatalog
├── descriptor.py
├── error.py
├── manifest.py
├── mediatype.py
└── tags.py
```

`CatalogError` is added to `src/regshape/libs/errors.py`, parallel to
`TagError` and `BlobError`.

---

## Data Models

### `RepositoryCatalog`

Represents the response body of `GET /v2/_catalog`.

```python
@dataclass
class RepositoryCatalog:
    repositories: list[str]  # Wire key: "repositories"
```

#### Field notes

- **`repositories`** is an ordered list of repository name strings (e.g.
  `["library/ubuntu", "myrepo/myimage"]`). The order matches the order
  returned by the registry; no sorting is applied.
- A registry MAY return `"repositories": null` on the last pagination page
  (same convention as tag listing). `from_dict` and `from_json` normalise
  this to `[]` before constructing the dataclass so callers never observe
  `None`.
- A missing `"repositories"` key is treated the same as `null` — normalised
  to `[]`.
- Unlike `TagList`, there is no `namespace`/`name` field: the catalog response
  is registry-scoped, not repository-scoped.

#### Validation rules (`__post_init__`)

| Field | Rule | Error |
|-------|------|-------|
| `repositories` | Must be a `list` | `ValueError` |

There is no non-empty constraint: a registry with no repositories returns a
valid empty catalog.

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> dict` | Serializes to OCI wire dict; always emits `"repositories": []` not `null` |
| `to_json` | `() -> str` | Canonical JSON (`sort_keys=True`, compact separators) |
| `from_dict` | `(cls, data: dict) -> RepositoryCatalog` | Deserializes from wire dict; normalises `null`/missing repositories |
| `from_json` | `(cls, data: str) -> RepositoryCatalog` | Deserializes from raw JSON string |

---

## JSON Wire Format

### Normal response

```json
{
  "repositories": ["library/ubuntu", "myrepo/myimage", "myrepo/other"]
}
```

### Empty registry

```json
{
  "repositories": []
}
```

### Last pagination page (null repositories)

```json
{
  "repositories": null
}
```

`from_dict` normalises `null` → `[]`, so `RepositoryCatalog.repositories == []`.

---

## Field-to-Wire-Key Mapping

| Python field | JSON wire key | Notes |
|---|---|---|
| `repositories` | `"repositories"` | `None` or missing in wire → `[]` in model; `[]` is always emitted on serialization |

---

## Pagination

The catalog endpoint supports the same link-header pagination scheme as tag
listing:

```
GET /v2/_catalog?n=100
```

Response header when more pages exist:

```
Link: </v2/_catalog?last=myrepo/myimage&n=100>; rel="next"
```

The `RepositoryCatalog` model represents a **single page**. Pagination cursor
logic (following `Link` headers, assembling pages) is the responsibility of the
domain operations layer (`libs/catalog/`), not the model.

---

## Error Handling

| Condition | Error Type | Behaviour |
|---|---|---|
| Malformed JSON in `from_json` | `CatalogError` | Wraps `json.JSONDecodeError` |
| `data` is not a dict in `from_dict` | `CatalogError` | Raised with type name |
| `"repositories"` value is not a list after null-normalisation | `ValueError` | Raised in `__post_init__` |
| Registry returns `404` or `405` on `GET /v2/_catalog` | `CatalogNotSupportedError` | Raised by the operations layer; never instantiates the model |
| Registry returns `401` on `GET /v2/_catalog` | `AuthError` | Raised by the operations layer; signals authentication/authorisation failure for the catalog endpoint |

`CatalogError` is a subclass of `RegShapeError` added to `libs/errors.py`.
`CatalogNotSupportedError` is a subclass of `CatalogError`, also added to
`libs/errors.py`, raised exclusively when the registry returns an HTTP status
that signals the catalog endpoint is not implemented (`404`, `405`). This
lets callers distinguish "endpoint not available" from "response was malformed".

```
RegShapeError
└── CatalogError
    └── CatalogNotSupportedError
```

---

## OCI Spec References

- **Listing repositories**: `GET /v2/_catalog` — non-normative, widely
  implemented. Not assigned an end-N number in the OCI Distribution Spec v1.1.
  Supports `?n=<count>&last=<last-repo>` pagination with `Link` response
  header, mirroring the tag-list pagination scheme.
- **Unsupported endpoint**: Registries that do not implement the catalog
  endpoint typically return `404 Not Found` or `405 Method Not Allowed`; some
  return `401 Unauthorized` to obscure endpoint existence. These are HTTP-level
  errors and are out of scope for the `RepositoryCatalog` model. The operations
  layer (`libs/catalog/`) is responsible for detecting these responses and
  raising a `CatalogNotSupportedError` (a `CatalogError` subclass) with a
  clear message (e.g. `"Registry does not support the catalog API"`). Using a
  dedicated subclass lets callers catch unsupported-endpoint failures
  separately from malformed-response failures without inspecting message
  strings. The model itself is never instantiated in these cases.
  **Operations layer design note**: the catalog operations spec must document
  which HTTP status codes map to `CatalogNotSupportedError` vs a generic
  `CatalogError` (e.g. a `401` on `GET /v2/_catalog` after a successful
  `GET /v2/` auth challenge should be treated as "not supported", not as an
  auth failure).

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `RegShapeError` base for `CatalogError`

**External (stdlib only):**
- `json` — serialization/deserialization
- `dataclasses` — `@dataclass`

No third-party dependencies.

---

## Open Questions

- [ ] Should `to_json()` / `to_dict()` emit `"repositories": []` or omit the
  key when the list is empty? Current proposal: always emit `"repositories": []`
  for consistency with `TagList` and the wire format.
- [ ] Should `RepositoryCatalog` expose `__len__` and `__iter__` convenience
  wrappers over `repositories`? Current proposal: no — keep it a plain
  dataclass; callers iterate `catalog.repositories` directly.
- [ ] Some registries return a `"next"` key or cursor alongside `"repositories"`
  in the response body rather than solely using the `Link` header. Should the
  model capture that? Current proposal: no — only the standard `Link`-header
  pagination is modelled; non-standard cursors are ignored.
- [x] Should there be a dedicated `CatalogNotSupportedError` subclass of
  `CatalogError` so callers can distinguish "registry does not implement the
  endpoint" from "response was malformed"? **Decision: yes.** `CatalogNotSupportedError`
  is defined alongside `CatalogError` in `libs/errors.py`. The exact mapping
  of HTTP status codes to this error is left to the operations layer spec.

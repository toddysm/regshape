# Data Model: Tags

## Overview

This spec defines the data model for OCI tag-list responses in
`src/regshape/libs/models/tags.py`.

The registry exposes a single endpoint for tag listing:

```
GET /v2/<name>/tags/list[?n=<count>&last=<last-tag>]
```

Tag deletion is performed via `DELETE /v2/<name>/manifests/<tag>`, which shares
the manifest delete endpoint and returns `202 Accepted` with no body. No data
model is needed for that operation.

Tag creation and tag movement are implicit side-effects of pushing a manifest
with a tag reference (`PUT /v2/<name>/manifests/<tag>`), which is covered by
the manifest domain.

The list response body carries the repository namespace and an ordered array of
tag strings. The data model therefore consists of a single type, `TagList`,
that wraps the wire response and normalises edge cases (a `null` `tags` array
is permitted by the spec when signalling the last pagination page).

---

## Module Structure

```
src/regshape/libs/models/
├── __init__.py      # Updated: exports TagList
├── descriptor.py
├── manifest.py
├── mediatype.py
└── tags.py          # NEW: TagList
```

`TagError` is added to `src/regshape/libs/errors.py`, parallel to
`ManifestError`.

---

## Data Models

### `TagList`

Represents the response body of `GET /v2/<name>/tags/list`.

```python
@dataclass
class TagList:
    namespace: str   # Wire key: "name"
    tags: list[str]  # Wire key: "tags"
```

#### Field notes

- **`namespace`** maps to the wire key `"name"`. The OCI spec uses `<name>`,
  *namespace*, *repository*, and *repo* interchangeably to refer to this value;
  `namespace` is used here because it matches the spec language most precisely
  ("the namespace of the repository").
- **`tags`** is an ordered list of tag strings. The OCI Distribution Spec
  requires tags to be in lexical ("ASCIIbetical") order, but `TagList` preserves
  the order exactly as received without enforcing it.
- A registry MAY return `"tags": null` on the last pagination page. `from_dict`
  and `from_json` normalise this to `[]` before constructing the dataclass so
  callers never observe `None`.
- A missing `"tags"` key is treated the same as `null` — normalised to `[]`.

#### Validation rules (`__post_init__`)

| Field | Rule | Error |
|-------|------|-------|
| `namespace` | Must be a non-empty string | `ValueError` |
| `tags` | Must be a `list` | `ValueError` |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> dict` | Serializes to OCI wire dict; emits `"tags": []` not `null` |
| `to_json` | `() -> str` | Canonical JSON (`sort_keys=True`, compact separators) |
| `from_dict` | `(cls, data: dict) -> TagList` | Deserializes from wire dict; normalises `null`/missing tags |
| `from_json` | `(cls, data: str) -> TagList` | Deserializes from raw JSON string |

---

## JSON Wire Format

### Normal response

```json
{
  "name": "myrepo/myimage",
  "tags": ["latest", "v1.0", "v2.0"]
}
```

### Last pagination page (null tags)

```json
{
  "name": "myrepo/myimage",
  "tags": null
}
```

`from_dict` normalises `null` → `[]`, so `TagList.tags == []`.

### Empty repository (no tags)

```json
{
  "name": "myrepo/myimage",
  "tags": []
}
```

---

## Field-to-Wire-Key Mapping

| Python field | JSON wire key | Notes |
|---|---|---|
| `namespace` | `"name"` | "name", "namespace", "repo", and "repository" are used interchangeably in the OCI spec |
| `tags` | `"tags"` | `None` or missing in wire → `[]` in model; `[]` is always emitted on serialization |

---

## Error Handling

| Condition | Error Type | Behaviour |
|---|---|---|
| Malformed JSON in `from_json` | `TagError` | Wraps `json.JSONDecodeError` |
| `data` is not a dict in `from_dict` | `TagError` | Raised with type name |
| `"name"` key missing in `from_dict` | `TagError` | Raised with field name |
| `namespace` is empty string | `ValueError` | Raised in `__post_init__` |
| `tags` is not a list (after null-normalisation) | `ValueError` | Raised in `__post_init__` |

`TagError` is a subclass of `RegShapeError` added to `libs/errors.py`.

---

## OCI Spec References

- **Listing Tags**: `GET /v2/<name>/tags/list` — end-8a / end-8b in the OCI
  Distribution Spec. Response MUST be `200 OK`; `"name"` and `"tags"` keys MUST
  be present. Tags MUST be in lexical ("ASCIIbetical") order when not empty.
- **Deleting tags**: `DELETE /v2/<name>/manifests/<tag>` — end-9. Returns
  `202 Accepted` with no body; no model needed.
- **Creating / moving tags**: `PUT /v2/<name>/manifests/<tag>` — end-7. Covered
  by the manifest domain.

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `RegShapeError` base for `TagError`

**External (stdlib only):**
- `json` — serialization/deserialization
- `dataclasses` — `@dataclass`

No third-party dependencies.

---

## Open Questions

- [ ] Should `to_json()` / `to_dict()` emit `"tags": []` or omit the key when
  the list is empty? Current proposal: always emit `"tags": []` (matches the
  wire format for an empty repository).
- [ ] Should `TagList` expose `__len__` and `__iter__` convenience wrappers over
  `tags`? Current proposal: no — keep it a plain dataclass; callers iterate
  `tag_list.tags` directly.

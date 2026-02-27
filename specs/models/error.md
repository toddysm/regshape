# Data Model: OCI Error Response

## Overview

This spec defines the data models for OCI error response bodies in
`src/regshape/libs/models/error.py`.

The OCI Distribution Spec mandates a structured error envelope for all
non-2xx responses:

```
{
  "errors": [
    {
      "code":    "<ERROR_CODE>",
      "message": "<human-readable message>",
      "detail":  <opaque, optional>
    }
  ]
}
```

Currently, `manifest.py` and `tag.py` each contain an inline `try/except`
block that duplicates this parsing logic. The `error.py` module centralises it
into two typed dataclasses — `OciErrorDetail` and `OciErrorResponse` — and
provides a `from_response()` class method that absorbs all parse failures.

The retrofit replaces every inline `err_body = response.json() …` block in the
CLI modules with a single call to
`OciErrorResponse.from_response(response).first_detail()`.

---

## Module Structure

```
src/regshape/libs/models/
├── __init__.py      # Updated: exports OciErrorDetail, OciErrorResponse
├── descriptor.py
├── error.py         # NEW: OciErrorDetail, OciErrorResponse
├── manifest.py
├── mediatype.py
└── tags.py
```

No new entry in `libs/errors.py` is needed — these are data models for the
wire format, not Python exception types.

---

## Data Models

### `OciErrorDetail`

Represents one entry in the `"errors"` array.

```python
@dataclass
class OciErrorDetail:
    code: str
    message: str
    detail: Any | None = None    # Wire key: "detail" — opaque per OCI spec
```

#### Field notes

- **`code`** is an OCI-defined error token such as `MANIFEST_UNKNOWN`,
  `NAME_UNKNOWN`, `BLOB_UNKNOWN`, `UNAUTHORIZED`, etc. The spec does not
  mandate a closed vocabulary; registries may use vendor-specific codes.
- **`message`** is a human-readable explanation for the error.
- **`detail`** is explicitly untyped in the OCI spec. It may be a `dict`,
  `list`, `str`, or `None`. The model stores it as-is; it is never interpreted
  or validated.

#### Validation rules (`__post_init__`)

| Field | Rule | Error |
|-------|------|-------|
| `code` | Must be a string (may be empty) | `ValueError` |
| `message` | Must be a string (may be empty) | `ValueError` |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `format` | `() -> str` | `"CODE: message"` when both are non-empty; falls back to whichever is non-empty, or `""` when both are empty |
| `to_dict` | `() -> dict` | Wire dict; omits `"detail"` key when `detail is None` |
| `from_dict` | `(cls, data: object) -> OciErrorDetail` | Deserializes from a wire dict |

---

### `OciErrorResponse`

Represents the full OCI error envelope.

```python
@dataclass
class OciErrorResponse:
    errors: list[OciErrorDetail]
```

#### Field notes

- **`errors`** is always a list. `from_dict` normalises a missing or `null`
  `"errors"` key to `[]`. An empty list here means the response body parsed as
  valid JSON but contained no errors — callers treat this the same as a parse
  failure (i.e. fall back to the raw response text).

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `first_detail` | `() -> str` | `errors[0].format()` when `errors` is non-empty; `""` otherwise. This is the single extraction point used by all CLI raise-helpers. |
| `to_dict` | `() -> dict` | Wire dict |
| `to_json` | `() -> str` | Canonical JSON (`sort_keys=True`, compact separators) |
| `from_dict` | `(cls, data: object) -> OciErrorResponse` | Deserializes from a wire dict |
| `from_json` | `(cls, data: str) -> OciErrorResponse` | Deserializes from raw JSON string |
| `from_response` | `(cls, response: requests.Response) -> OciErrorResponse` | Parse the response body; **never raises** — returns `OciErrorResponse(errors=[])` on any parse failure |

#### `from_response` contract

`from_response` is the primary integration point for CLI code. It absorbs all
failure modes silently so that error-raise helpers remain one-liners:

```python
detail = OciErrorResponse.from_response(response).first_detail()
```

Returns `OciErrorResponse(errors=[])` (which yields `first_detail() == ""`) in
any of these cases:

- Response body is empty.
- Response body is not valid JSON.
- Top-level JSON is not a dict.
- `"errors"` key is absent or `null`.
- Any individual entry is malformed (only well-formed entries are included;
  malformed ones are skipped rather than aborting the whole parse).

---

## JSON Wire Format

### Normal error response

```json
{
  "errors": [
    {
      "code": "MANIFEST_UNKNOWN",
      "message": "manifest unknown",
      "detail": { "Tag": "v1.0" }
    }
  ]
}
```

### Multiple errors

```json
{
  "errors": [
    { "code": "UNAUTHORIZED",     "message": "authentication required" },
    { "code": "TOOMANYREQUESTS",  "message": "rate limit exceeded",     "detail": null }
  ]
}
```

### Body with no errors array (non-OCI registry)

```json
{ "message": "not found" }
```

`from_response` returns `OciErrorResponse(errors=[])`;
`first_detail()` returns `""`.

---

## Field-to-Wire-Key Mapping

### `OciErrorDetail`

| Python field | JSON wire key | Notes |
|---|---|---|
| `code` | `"code"` | May be absent in non-conformant registries; defaults to `""` |
| `message` | `"message"` | May be absent; defaults to `""` |
| `detail` | `"detail"` | Opaque; stored as-is; key omitted when `None` in `to_dict` |

### `OciErrorResponse`

| Python field | JSON wire key | Notes |
|---|---|---|
| `errors` | `"errors"` | `null` or missing → `[]` |

---

## Error Handling

### `OciErrorDetail.from_dict`

| Condition | Behaviour |
|---|---|
| `data` is not a `dict` | Raises `ValueError` with type name |
| `"code"` absent or `None` | Defaults to `""` (non-conformant but tolerated) |
| `"message"` absent or `None` | Defaults to `""` (non-conformant but tolerated) |
| `"detail"` absent | `detail` field is `None` |

### `OciErrorResponse.from_dict`

| Condition | Behaviour |
|---|---|
| `data` is not a `dict` | Raises `ValueError` with type name |
| `"errors"` absent or `None` | `errors` is `[]` |
| An entry in `"errors"` is malformed | Entry is skipped; others are still parsed |

### `OciErrorResponse.from_json`

| Condition | Behaviour |
|---|---|
| `data` is not valid JSON | Raises `ValueError` (wraps `json.JSONDecodeError`) |

### `OciErrorResponse.from_response`

| Condition | Behaviour |
|---|---|
| Response body is empty | Returns `OciErrorResponse(errors=[])` |
| Body is not valid JSON | Returns `OciErrorResponse(errors=[])` |
| Top-level JSON is not a dict | Returns `OciErrorResponse(errors=[])` |
| Any other exception | Returns `OciErrorResponse(errors=[])` |

`from_response` **never raises**.

---

## Retrofit: CLI integration points

The following locations in the CLI modules contain inline OCI error parsing
that will be replaced during retrofit.

### `manifest.py` — `_raise_for_manifest_error`

**Before:**
```python
detail = ""
try:
    err_body = response.json()
    errors = err_body.get("errors", [])
    if errors:
        first = errors[0]
        code = first.get("code", "")
        msg = first.get("message", "")
        detail = f"{code}: {msg}"
except Exception:
    detail = response.text[:200]
```

**After:**
```python
detail = OciErrorResponse.from_response(response).first_detail() or response.text[:200]
```

### `tag.py` — `_parse_oci_error_detail`

The helper function `_parse_oci_error_detail(response)` can be deleted
entirely. Both `_raise_for_list_error` and `_raise_for_delete_error` replace
their call to that helper with:

```python
detail = OciErrorResponse.from_response(response).first_detail() or response.text[:200]
```

---

## OCI Spec References

- **Error Codes**: OCI Distribution Spec §6 "Error Codes". Defines the
  mandatory error envelope and a set of standard codes (`BLOB_UNKNOWN`,
  `BLOB_UPLOAD_INVALID`, `BLOB_UPLOAD_UNKNOWN`, `DIGEST_INVALID`,
  `MANIFEST_BLOB_UNKNOWN`, `MANIFEST_INVALID`, `MANIFEST_UNKNOWN`,
  `NAME_INVALID`, `NAME_UNKNOWN`, `SIZE_INVALID`, `UNAUTHORIZED`,
  `DENIED`, `UNSUPPORTED`).
- **Error format**: Wire format used by conformant registries for all non-2xx
  responses.

---

## Dependencies

**Internal:**
- None — `error.py` has no dependencies on other `regshape` modules.

**External:**
- `json` — serialization/deserialization
- `dataclasses` — `@dataclass`
- `typing` — `Any`
- `requests` — `requests.Response` type hint in `from_response` only

---

## Open Questions

- [ ] Should `from_response` fall back to the raw response text (truncated to
  200 chars) when `errors` is empty, rather than returning `""`? Current
  proposal: no — `from_response` returns the model only; the fallback
  `or response.text[:200]` is a one-liner at each call site, keeping the model
  pure and the fallback behaviour visible.
- [ ] Should `OciErrorResponse` expose `__iter__` / `__len__` over `errors`?
  Current proposal: no — callers use `first_detail()` for the common case and
  access `.errors` directly for full enumeration.

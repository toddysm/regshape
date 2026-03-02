# Operations: Manifests

## Overview

This spec documents the domain operations layer for OCI manifest interactions
in `src/regshape/libs/manifests/operations.py`.

The manifests operations layer sits between the CLI and the foundation layer.
It never calls `requests` directly — all HTTP traffic goes through a
`RegistryClient` instance provided by the caller.

### Endpoints

| Operation | Method | Endpoint |
|---|---|---|
| Get manifest | `GET` | `/v2/{repo}/manifests/{reference}` |
| Head manifest | `HEAD` | `/v2/{repo}/manifests/{reference}` |
| Push manifest | `PUT` | `/v2/{repo}/manifests/{reference}` |
| Delete manifest | `DELETE` | `/v2/{repo}/manifests/{digest}` |

---

## Module Structure

```
src/regshape/libs/manifests/
├── __init__.py        # Package init; re-exports public symbols
└── operations.py      # Public domain operations + private error helper
```

---

## Public Operations

### `get_manifest`

```python
@track_time
def get_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
) -> tuple[str, str, str]:
```

Fetches the manifest body for a repository reference.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name (e.g. `"myrepo/myimage"`) |
| `reference` | `str` | required | Tag or digest (e.g. `"latest"` or `"sha256:abc..."`) |
| `accept` | `str` | required | Value for the `Accept` header |

**Returns:** `(body_str, content_type, digest)` — the raw manifest JSON
string, the `Content-Type` response header value, and the
`Docker-Content-Digest` response header value.

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{reference}"`.
2. Call `client.get(path, headers={"Accept": accept})`.
3. Pass response to `_raise_for_manifest_error`.
4. Return `(response.text, Content-Type header, Docker-Content-Digest header)`.

**Decorator:** `@track_time`

---

### `head_manifest`

```python
@track_time
def head_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
) -> tuple[str, str, int]:
```

Returns metadata for a manifest without downloading its body.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `reference` | `str` | required | Tag or digest |
| `accept` | `str` | required | Value for the `Accept` header |

**Returns:** `(digest, media_type, size)` — `Docker-Content-Digest` header,
`Content-Type` header, and `Content-Length` header (parsed as `int`, defaulting
to `0` on parse failure or absence).

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{reference}"`.
2. Call `client.head(path, headers={"Accept": accept})`.
3. Pass response to `_raise_for_manifest_error`.
4. Return `(digest, media_type, size)` from response headers.

**Decorator:** `@track_time`

---

### `push_manifest`

```python
@track_time
def push_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    body: bytes,
    content_type: str,
) -> str:
```

Pushes a manifest to a repository, creating or overwriting the reference.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `reference` | `str` | required | Tag or digest to create/overwrite |
| `body` | `bytes` | required | Raw manifest JSON bytes |
| `content_type` | `str` | required | Value for the `Content-Type` header |

**Returns:** `str` — the `Docker-Content-Digest` response header value.

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{reference}"`.
2. Call `client.put(path, headers={"Content-Type": content_type}, data=body)`.
3. Pass response to `_raise_for_manifest_error`.
4. Return `Docker-Content-Digest` header value.

**Decorator:** `@track_time`

---

### `delete_manifest`

```python
@track_time
def delete_manifest(
    client: RegistryClient,
    repo: str,
    digest: str,
) -> None:
```

Deletes the manifest identified by digest. Per the OCI Distribution Spec,
deletion must be by digest, not by tag.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `digest` | `str` | required | Manifest digest (e.g. `"sha256:abc..."`) |

**Returns:** `None`

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{digest}"`.
2. Call `client.delete(path)`.
3. Pass response to `_raise_for_manifest_error`.

**Decorator:** `@track_time`

---

## Private Helpers

### `_raise_for_manifest_error`

Single error helper shared by all four operations.

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `ManifestError` | `"Manifest not found: {ref}"` |
| other non-2xx | `ManifestError` | `"Registry error for {ref}: HTTP {status}"` |

`detail` is populated from
`OciErrorResponse.from_response(response).first_detail()`, falling back to
the first 200 characters of `response.text`.

---

## Telemetry

| Decorator | Applied to |
|---|---|
| `@track_time` | `get_manifest`, `head_manifest`, `push_manifest`, `delete_manifest` |

No `@track_scenario` is needed — all four operations are single-step HTTP
calls.

---

## Error Handling Summary

| Condition | Exception | Raised by |
|---|---|---|
| `401` on any call | `AuthError` | `_raise_for_manifest_error` |
| `404` on any operation | `ManifestError` "Manifest not found" | `_raise_for_manifest_error` |
| Other non-2xx | `ManifestError` "Registry error" | `_raise_for_manifest_error` |
| Transport / connection failure | `requests.exceptions.RequestException` | propagated as-is |

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `AuthError`, `ManifestError`
- `regshape.libs.models.error` — `OciErrorResponse`
- `regshape.libs.refs` — `format_ref`
- `regshape.libs.transport` — `RegistryClient`
- `regshape.libs.decorators.timing` — `track_time`

**External:**
- `requests` — `requests.Response` type annotation only

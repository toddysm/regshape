# Operations: Blobs

## Overview

This spec documents the domain operations layer for OCI blob interactions
in `src/regshape/libs/blobs/operations.py`.

The blobs operations layer sits between the CLI and the foundation layer.
It never calls `requests` directly — all HTTP traffic goes through a
`RegistryClient` instance provided by the caller.

### Endpoints

| Operation | Method | Endpoint |
|---|---|---|
| Head blob | `HEAD` | `/v2/{repo}/blobs/{digest}` |
| Get blob | `GET` | `/v2/{repo}/blobs/{digest}` |
| Delete blob | `DELETE` | `/v2/{repo}/blobs/{digest}` |
| Upload (monolithic) | `POST` + `PUT` | `/v2/{repo}/blobs/uploads/` → `<location>?digest=...` |
| Upload (chunked) | `POST` + N×`PATCH` + `PUT` | `/v2/{repo}/blobs/uploads/` → `<location>` → `<location>?digest=...` |
| Mount blob | `POST` | `/v2/{repo}/blobs/uploads/?from={from_repo}&mount={digest}` |

---

## Module Structure

```
src/regshape/libs/blobs/
├── __init__.py        # Package init; re-exports public symbols
└── operations.py      # Public domain operations + private helpers
```

### Module-level constants

| Constant | Value | Description |
|---|---|---|
| `_DEFAULT_CHUNK_SIZE` | `65536` | Default streaming/upload chunk size in bytes |
| `_DEFAULT_CONTENT_TYPE` | `"application/octet-stream"` | Default `Content-Type` for blob uploads |
| `_SUPPORTED_ALGORITHMS` | `{"sha256", "sha512"}` | Accepted digest algorithm prefixes |

---

## Public Operations

### `head_blob`

```python
@track_time
def head_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
) -> BlobInfo:
```

Checks existence and retrieves metadata for a blob without downloading its
content.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `digest` | `str` | required | Blob digest (e.g. `"sha256:abc..."`) |

**Returns:** `BlobInfo` constructed from `Docker-Content-Digest`,
`Content-Type`, and `Content-Length` response headers.

**Behaviour:**

1. `GET` path `"/v2/{repo}/blobs/{digest}"`.
2. Call `client.head(path)`.
3. Pass response to `_raise_for_blob_error`.
4. Return `_blob_info_from_response(response, digest)`.

**Decorator:** `@track_time`

---

### `get_blob`

```python
@track_time
def get_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
    output_path: Optional[str] = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> BlobInfo:
```

Downloads a blob and verifies its digest.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `digest` | `str` | required | Expected digest (e.g. `"sha256:..."` or `"sha512:..."`) |
| `output_path` | `Optional[str]` | `None` | File path to write the blob to; when `None` body is consumed for digest verification only |
| `chunk_size` | `int` | `65536` | Streaming chunk size in bytes |

**Returns:** `BlobInfo` built from response headers after successful digest
verification.

**Behaviour:**

1. Derive algorithm from `digest.partition(":")`. Raise `BlobError` immediately
   if the algorithm is unsupported (before any network I/O).
2. Call `client.get(path, stream=True)`.
3. Pass response to `_raise_for_blob_error`.
4. Stream response body: if `output_path` is set, write to file via
   `_stream_to_file`; otherwise consume chunks only to update the hasher.
5. On `OSError` during file write, raise `BlobError` "Cannot write to output
   path".
6. After streaming, compare `"{algorithm}:{hexdigest}"` against `digest`. On
   mismatch: delete the output file (if written) and raise `BlobError`
   "Digest mismatch".
7. Return `_blob_info_from_response(response, digest)`.

**Decorator:** `@track_time`

---

### `delete_blob`

```python
@track_time
def delete_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
) -> None:
```

Deletes a blob from the registry.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `digest` | `str` | required | Blob digest to delete |

**Returns:** `None`. Expects `202 Accepted`.

**Behaviour:**

1. Build path `"/v2/{repo}/blobs/{digest}"`.
2. Call `client.delete(path)`.
3. Pass response to `_raise_for_blob_error`.

**Decorator:** `@track_time`

---

### `upload_blob`

```python
@track_scenario("blob upload")
def upload_blob(
    client: RegistryClient,
    repo: str,
    data: bytes,
    digest: str,
    content_type: str = _DEFAULT_CONTENT_TYPE,
) -> str:
```

Uploads a blob using the monolithic POST + PUT protocol.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `data` | `bytes` | required | Raw blob bytes |
| `digest` | `str` | required | Expected content digest; sent as `digest` query parameter on the completing PUT |
| `content_type` | `str` | `"application/octet-stream"` | `Content-Type` header on the PUT |

**Returns:** `str` — confirmed digest from `Docker-Content-Digest` header
(falls back to the supplied `digest` if the header is absent).

**Behaviour (two HTTP calls):**

1. **POST** `"/v2/{repo}/blobs/uploads/"` to initiate the upload session.
   Pass response to `_raise_for_upload_error(response, registry, session_id=None)`.
2. Extract `Location` header → `BlobUploadSession.from_location(location)`.
3. Split `session.upload_path` via `_split_upload_path`; append
   `("digest", digest)` to the query params list.
4. **PUT** `<bare_path>` with `params=<params_list>`,
   `Content-Type: {content_type}`, `Content-Length: {len(data)}`.
   Pass response to `_raise_for_upload_error`.
5. Compare `Docker-Content-Digest` header against `digest`; raise `BlobError`
   on mismatch.
6. Return confirmed digest (or `digest` if header absent).

**Decorator:** `@track_scenario("blob upload")`

---

### `upload_blob_chunked`

```python
@track_scenario("blob upload chunked")
def upload_blob_chunked(
    client: RegistryClient,
    repo: str,
    source: BinaryIO,
    digest: str,
    content_type: str = _DEFAULT_CONTENT_TYPE,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> str:
```

Uploads a blob using the chunked POST + N×PATCH + PUT protocol.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `source` | `BinaryIO` | required | Open binary file-like object to read from |
| `digest` | `str` | required | Expected content digest |
| `content_type` | `str` | `"application/octet-stream"` | `Content-Type` on the completing PUT |
| `chunk_size` | `int` | `65536` | Chunk size in bytes |

**Returns:** `str` — confirmed digest (same logic as `upload_blob`).

**Behaviour (POST + N×PATCH + PUT):**

1. **POST** `"/v2/{repo}/blobs/uploads/"` to initiate session.
   Pass to `_raise_for_upload_error(response, registry, session_id=None)`.
2. Extract `Location` header → `BlobUploadSession.from_location(location)`.
3. **PATCH loop** — while `source.read(chunk_size)` yields non-empty bytes:
   - Send `PATCH session.upload_path` with `Content-Range: {start}-{end}/*`,
     `Content-Length`, `Content-Type: application/octet-stream`.
   - Pass to `_raise_for_upload_error`.
   - Advance `session.offset += len(chunk)`.
   - If `Location` header is present in the PATCH response, attempt to update
     `session.upload_path` and `session.session_id` via `BlobUploadSession.from_location`;
     silently ignore unparseable new locations (keep existing path).
4. **PUT** (completing) — split `session.upload_path`, append `("digest", digest)`,
   send PUT with empty body and `Content-Length: 0`.
   Pass to `_raise_for_upload_error`.
5. Compare and return confirmed digest (same as `upload_blob`).

**Decorator:** `@track_scenario("blob upload chunked")`

---

### `mount_blob`

```python
@track_time
def mount_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
    from_repo: str,
) -> str:
```

Attempts a cross-repository blob mount without a data transfer.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Destination repository name |
| `digest` | `str` | required | Blob digest to mount |
| `from_repo` | `str` | required | Source repository name (without registry prefix) |

**Returns:** `str` — confirmed digest from `Docker-Content-Digest` header.

**Behaviour:**

1. POST `"/v2/{repo}/blobs/uploads/?from={from_repo}&mount={digest}"`.
2. If the response is `202 Accepted`, raise `BlobError` directing the caller
   to fall back to `upload_blob` or `upload_blob_chunked`.
3. Otherwise pass to `_raise_for_blob_error`.
4. Return `Docker-Content-Digest` header (falls back to supplied `digest`).

**Decorator:** `@track_time`

---

## Private Helpers

### `_split_upload_path`

```python
def _split_upload_path(upload_path: str) -> tuple[str, list[tuple[str, str]]]:
```

Splits an upload path (which may contain session tokens in its query string)
into a bare path and an ordered list of `(key, value)` query pairs. Callers
append `("digest", digest)` to the list and pass both to `client.put(...,
params=params)`, letting `requests` handle percent-encoding.

---

### `_blob_info_from_response`

```python
def _blob_info_from_response(response, fallback_digest: str) -> BlobInfo:
```

Constructs a `BlobInfo` from `Docker-Content-Digest`, `Content-Type`, and
`Content-Length` headers. Falls back to `fallback_digest` / `"application/octet-stream"` /
`0` when headers are absent or unparseable.

---

### `_stream_to_file`

```python
def _stream_to_file(response, output_path: str, chunk_size: int, hasher) -> None:
```

Streams a response body to a file path in `chunk_size`-byte increments,
calling `hasher.update(chunk)` for each chunk.

---

### `_raise_for_blob_error`

Used by `head_blob`, `get_blob`, `delete_blob`, `mount_blob`.

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `BlobError` | `"Blob not found: {registry}/{repo}@{digest}"` |
| `405` | `BlobError` | `"Operation not supported by this registry"` |
| other non-2xx | `BlobError` | `"Registry error for {registry}/{repo}: HTTP {status}"` |

---

### `_raise_for_upload_error`

Used by `upload_blob` and `upload_blob_chunked` for POST, PATCH, and PUT
stages.

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `400` | `BlobError` | `"Invalid upload: {detail}"` |
| `404` | `BlobError` | `"Upload session not found: {session_id}"` |
| `416` | `BlobError` | `"Offset mismatch during chunked upload"` |
| other non-2xx | `BlobError` | `"Registry error during blob upload: HTTP {status}"` |

---

## Telemetry

| Decorator | Applied to | Controls |
|---|---|---|
| `@track_time` | `head_blob`, `get_blob`, `delete_blob`, `mount_blob` | Per-call timing (`--time-methods`) |
| `@track_scenario("blob upload")` | `upload_blob` | Multi-step scenario timing (`--time-scenarios`) |
| `@track_scenario("blob upload chunked")` | `upload_blob_chunked` | Multi-step scenario timing (`--time-scenarios`) |

---

## Error Handling Summary

| Condition | Exception | Raised by |
|---|---|---|
| `401` on any call | `AuthError` | `_raise_for_blob_error` / `_raise_for_upload_error` |
| `404` on content ops | `BlobError` "Blob not found" | `_raise_for_blob_error` |
| `405` on content ops | `BlobError` "Operation not supported" | `_raise_for_blob_error` |
| `400` during upload | `BlobError` "Invalid upload" | `_raise_for_upload_error` |
| `404` during upload | `BlobError` "Upload session not found" | `_raise_for_upload_error` |
| `416` during chunked upload | `BlobError` "Offset mismatch" | `_raise_for_upload_error` |
| `202` on mount | `BlobError` "Blob mount not accepted" | `mount_blob` inline |
| Unsupported digest algorithm | `BlobError` | `get_blob` inline (before any I/O) |
| Digest mismatch after download | `BlobError` "Digest mismatch" | `get_blob` inline (output file deleted) |
| Digest mismatch confirmed by registry | `BlobError` "Digest mismatch" | `upload_blob` / `upload_blob_chunked` inline |
| `OSError` writing output file | `BlobError` "Cannot write to output path" | `get_blob` inline |
| Transport / connection failure | `requests.exceptions.RequestException` | propagated as-is |

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `AuthError`, `BlobError`
- `regshape.libs.models.blob` — `BlobInfo`, `BlobUploadSession`
- `regshape.libs.models.error` — `OciErrorResponse`
- `regshape.libs.transport` — `RegistryClient`
- `regshape.libs.decorators.timing` — `track_time`
- `regshape.libs.decorators.scenario` — `track_scenario`

**External (stdlib):**
- `hashlib` — digest computation in `get_blob`
- `urllib.parse` — `urlparse`, `parse_qsl` in `_split_upload_path`

**External (third-party):**
- `requests` — `requests.Response` type annotation only

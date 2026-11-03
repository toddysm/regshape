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
    *,
    progress_callback: ProgressCallback | None = None,
    total_size: int | None = None,
) -> BlobInfo:
```

Downloads a blob and verifies its digest.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `digest` | `str` | required | Expected digest (e.g. `"sha256:..."` or `"sha512:..."`) |
| `output_path` | `Optional[str]` | `None` | File path to write the blob to; when `None` body is consumed for digest verification only |
| `chunk_size` | `int` | `65536` | Positive streaming chunk size in bytes |
| `progress_callback` | `Optional[ProgressCallback]` | `None` | Keyword-only observer of immutable transfer events |
| `total_size` | `Optional[int]` | `None` | Keyword-only trusted size of the consumed payload |

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
7. Close the streamed response, then return `_blob_info_from_response(response, digest)`.
   See [transfer progress](#transfer-progress) for opt-in lifecycle and size checks.

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
    *,
    progress_callback: ProgressCallback | None = None,
    total_size: int | None = None,
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
| `chunk_size` | `int` | `65536` | Positive chunk size in bytes |
| `progress_callback` | `Optional[ProgressCallback]` | `None` | Keyword-only observer of accepted-byte/lifecycle events |
| `total_size` | `Optional[int]` | `None` | Keyword-only trusted bytes remaining from the current source offset |

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

### `upload_blob_stream`

```python
upload_blob_stream(
    client, repo, source, digest, content_type="application/octet-stream",
    *, progress_callback=None, total_size=None,
)
```

This is the file/stream-capable monolithic alternative to the existing
bytes-based `upload_blob`. It preserves POST-plus-PUT rather than switching
registry protocols. The source remains caller-owned and is never closed.

For seekable binary sources, remaining length is discovered by seeking to the
end and restoring the original position, without reading the payload into memory.
An explicit `total_size` must match that remaining length. Nonseekable sources
require an explicit size that frames one payload, without an EOF lookahead:
pipe writers need not close before receiving the upload result, and bytes after
the frame remain unread. Replay after consumption fails before a second request.
Short/nonbinary data and size changes in seekable files fail explicitly rather
than padding or completing an empty retry. The caller must provide the correct
nonseekable frame size; the library cannot infer additional data beyond it.

The HTTP client reads the counted body incrementally with Content-Length.
Progress measures bytes read for the client, not server acknowledgements.
The full-byte state is `COMMITTING` while the final response is pending, then
`VERIFYING`; success follows the registry response and applicable digest check.
Source and callback failures propagate without being mistaken for retriable
network failures, even if Requests wraps the original exception.

Authentication retries in both client modes, configured transient retries, and
Requests redirect rewinds reset to the original source offset. The same
transfer ID is retained; `attempt` increases and a `RETRYING` event replaces
unconfirmed bytes with zero instead of accumulating retransmissions. Existing
retry eligibility and redirect policies are not broadened. The wrapper is
closed after use, but the borrowed source is not. Known request length remains
available to existing telemetry without reading the stream for measurement.
If a redirect changes the completing PUT into another method, its response
cannot confirm the upload and raises `BlobError` instead of reporting success.
For a nonempty payload, the final prepared request must also retain the counted
body; a redirect that keeps PUT but discards its body is not confirmation.

### Transfer progress

`ProgressCallback`, `TransferProgress`, `TransferDirection`, and `TransferPhase`
are available from `regshape.libs.progress` and re-exported by
`regshape.libs.blobs`. No command-line renderer or telemetry flags are required.
The callback is synchronous and optional; callers should keep it lightweight.

Each frozen `TransferProgress` contains:

| Field | Meaning |
| --- | --- |
| `transfer_id` | Opaque ID unique to the invocation, stable across its events |
| `digest` | Valid SHA-256/SHA-512 blob digest; arbitrary URLs/strings are rejected when observing |
| `direction` | `TransferDirection.DOWNLOAD` or `UPLOAD` |
| `phase` | A `TransferPhase` lifecycle state |
| `bytes_completed` | Absolute processed/accepted payload bytes, never a delta |
| `total_bytes` | Trusted size, or `None` for unknown; zero means known empty |
| `attempt` | Logical payload attempt; increases on monolithic body replay, not buffered-chunk HTTP retries |

Downloads emit `PREPARING`, `TRANSFERRING` at zero and after each nonempty
consumed chunk, `VERIFYING`, then `COMPLETED`. Both saved and verification-only
downloads are observed. Chunked uploads emit preparation/transfer updates after
successful chunk responses, then `COMMITTING`, `VERIFYING`, and `COMPLETED`.
The final upload response and applicable digest checks precede completion.
Empty transfers still have lifecycle events.

When observing a download without an explicit total, a valid decimal
`Content-Length` is used only without transfer encoding and with identity
content encoding. Otherwise the total stays unknown; compressed wire sizes are
not confused with decoded payload bytes. Negative or unusable metadata becomes
zero in the legacy `BlobInfo.size` result, but never a known-zero progress total.
Upload source length is not probed: supply `total_size` when known, measured from
the current source offset. The library never seeks or closes a caller-owned
upload source.

A trusted total mismatch raises `BlobError`, not a clamped percentage or
successful result. Overruns retain the last valid counter and fail before an
oversized upload chunk is sent; underruns fail before upload commit. Explicit
totals are checked even without a callback. Absent both new arguments, inferred
download-header size checks are not imposed on existing callers. Nonpositive,
boolean, and noninteger chunk sizes are rejected before I/O, preventing empty
commits or unbounded reads.

Buffered chunks retried by authentication or transport middleware contribute
bytes only once after success. This item does not expose individual internal
retry events; `attempt` stays one for that protocol. Monolithic streamed bodies
emit `RETRYING` on rewind. `SKIPPED` is reserved for orchestration; this library
contract does not itself implement terminal rendering.

An operational observer receives one terminal `FAILED` or `CANCELLED` event
when the transfer fails, or `COMPLETED` on success. A callback exception
propagates unchanged and disables further notifications to that callback;
terminal notification is not attempted recursively. If failure notification
itself raises, only the secondary exception type is logged and the original
transfer error remains primary. Events never include credentials, URLs, headers,
payload content, or raw exception messages.
`KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` are classified as
cancelled; other transfer exceptions are classified as failed.

Streamed responses and output-file handles are closed on success/failure.
Existing output-file semantics remain: digest mismatches remove the invalid
file; other failures may leave partial output, so callers must not use it as a
successful download. Cleanup errors are surfaced without masking a primary
transfer failure. No automatic remote-session cancellation or resumable-upload
protocol is introduced.

Example library use, given an existing authenticated `client` and a valid
`digest`:

```python
from regshape.libs.blobs import TransferProgress, get_blob

events: list[TransferProgress] = []
info = get_blob(client, "team/image", digest, progress_callback=events.append)
```

The callback data supports later rendering decisions; it does not print, throttle,
or allocate terminal rows. Existing layout callback event shapes are unchanged.

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
`0` when headers are absent, unparseable, or negative.

---

### `_stream_to_file`

```python
def _stream_to_file(response, output_path, chunk_size, update_hash, progress) -> None:
```

Streams a response body to a file path in `chunk_size`-byte increments,
calling `update_hash(chunk)` and reporting progress after writing each chunk.
File cleanup preserves an existing transfer/observer error.

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

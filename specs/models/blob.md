# Data Model: Blob

## Overview

This spec defines the data models for OCI blob operations in
`src/regshape/libs/models/blob.py`.

Blobs are the raw content objects referenced by manifests — image layers,
config objects, and arbitrary artifact payloads. The OCI Distribution Spec
exposes three families of blob endpoint:

- **Content access** — `HEAD` and `GET /v2/<name>/blobs/<digest>` return
  (or stream) an existing blob; no response body is parsed as JSON.
- **Content deletion** — `DELETE /v2/<name>/blobs/<digest>` returns
  `202 Accepted` with no body; no model is needed.
- **Upload protocol** — `POST /v2/<name>/blobs/uploads/` initiates a session;
  subsequent PATCH (chunked) or PUT (monolithic/completing) calls carry the
  content and commit it. The session state must be tracked across calls.

The model layer therefore consists of exactly two types:

- **`BlobInfo`** — blob metadata derived from response headers (HEAD / GET
  response); never parsed from a JSON body.
- **`BlobUploadSession`** — upload session state derived from the `Location`
  header returned by the initiating POST; tracks the upload path and the
  advancing byte offset for chunked uploads.

`BlobError` is added to `src/regshape/libs/errors.py`, parallel to
`ManifestError` and `TagError`.

---

## Module Structure

```
src/regshape/libs/models/
├── __init__.py      # Updated: exports BlobInfo, BlobUploadSession
├── blob.py          # NEW: BlobInfo, BlobUploadSession
├── descriptor.py
├── manifest.py
├── mediatype.py
└── tags.py
```

`BlobError` is added to `src/regshape/libs/errors.py`.

---

## Data Models

### `BlobInfo`

Metadata for an existing blob, derived from the response headers of a
`HEAD /v2/<name>/blobs/<digest>` or `GET /v2/<name>/blobs/<digest>` call.
There is no JSON body to parse — all fields come from headers.

```python
@dataclass
class BlobInfo:
    digest: str        # From: Docker-Content-Digest header
    content_type: str  # From: Content-Type header
    size: int          # From: Content-Length header (0 if absent)
```

#### Field notes

- **`digest`** is taken from the `Docker-Content-Digest` response header.
  It confirms the server-side identity of the blob and should be used for
  verification, not the digest embedded in the request path (which the
  client supplied and which the server may redirect or rewrite).
- **`content_type`** is taken from the `Content-Type` response header. For
  image layers this is typically `application/vnd.oci.image.layer.v1.tar+gzip`;
  for config objects it is `application/vnd.oci.image.config.v1+json`. The
  model does not validate or constrain this value.
- **`size`** is taken from the `Content-Length` response header. Set to `0`
  when the header is absent or non-numeric; the domain layer does not treat a
  missing `Content-Length` as an error (some registries omit it on `HEAD`
  responses).

#### Validation rules (`__post_init__`)

| Field | Rule | Error |
|-------|------|-------|
| `digest` | Must match `^(sha256\|sha512):[a-f0-9]+$` | `ValueError` |
| `content_type` | Must be non-empty string | `ValueError` |
| `size` | Must be >= 0 | `ValueError` |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> dict` | Serializes to a plain dict; emits all three fields |

`BlobInfo` has no `from_dict` / `from_json` methods — it is always constructed
from header values by the domain layer, never deserialized from a JSON body.

---

### `BlobUploadSession`

State for an in-progress blob upload, derived from the `Location` header
returned by `POST /v2/<name>/blobs/uploads/`. The session is used internally
by `upload_blob` and `upload_blob_chunked` in `libs/blobs/` to track the
upload path and byte offset across the POST → (PATCH*) → PUT call sequence.

```python
@dataclass
class BlobUploadSession:
    upload_path: str  # Path component of the Location URL (/v2/.../uploads/<uuid>)
    session_id: str   # UUID at the end of upload_path
    offset: int = 0   # Current byte offset; advanced after each PATCH
```

#### Field notes

- **`upload_path`** is the path component of the `Location` header value. Some
  registries return an absolute URL (`https://registry/v2/.../uploads/uuid``)
  and others return a relative path (`/v2/.../uploads/uuid``). The class
  always stores only the path component so that `RegistryClient` receives a
  clean `/v2/...` path regardless of what the registry returned.
- **`session_id`** is the final path segment of `upload_path` (the UUID
  assigned by the registry). It is stored separately to allow callers to log
  or reference the session without re-parsing the path.
- **`offset`** starts at 0 and is advanced by the domain layer after each
  successful PATCH. It is sent as the start of the `Content-Range` header on
  the next PATCH and as the final byte count on the completing PUT.

#### Validation rules (`__post_init__`)

| Field | Rule | Error |
|-------|------|-------|
| `upload_path` | Must be non-empty string starting with `/` | `ValueError` |
| `session_id` | Must be non-empty string | `ValueError` |
| `offset` | Must be >= 0 | `ValueError` |

#### Class method: `from_location`

```python
@classmethod
def from_location(cls, location: str) -> "BlobUploadSession":
    """Parse a ``Location`` header value into an upload session.

    Accepts both absolute URLs and relative paths:

    - ``https://registry.example.com/v2/repo/blobs/uploads/abc-123``
    - ``/v2/repo/blobs/uploads/abc-123``

    The path component is extracted with ``urllib.parse.urlparse`` so the
    stored ``upload_path`` is always a clean ``/v2/...`` path.

    :param location: Raw ``Location`` header value from a POST response.
    :returns: A :class:`BlobUploadSession` with ``offset=0``.
    :raises BlobError: If *location* is empty, cannot be parsed, the path
        component does not start with ``/v2/``, or the UUID segment is
        absent.
    """
```

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `from_location` | `(cls, location: str) -> BlobUploadSession` | Parse from a `Location` header value |

There are no serialization methods — `BlobUploadSession` is a transient
in-process object that is never written to disk or sent over the wire.

---

## Error Handling

| Condition | Error Type | Behaviour |
|---|---|---|
| `digest` format invalid | `ValueError` | Raised in `BlobInfo.__post_init__` |
| `content_type` is empty | `ValueError` | Raised in `BlobInfo.__post_init__` |
| `size` is negative | `ValueError` | Raised in `BlobInfo.__post_init__` |
| `upload_path` is empty or lacks leading `/` | `ValueError` | Raised in `BlobUploadSession.__post_init__` |
| `session_id` is empty | `ValueError` | Raised in `BlobUploadSession.__post_init__` |
| `offset` is negative | `ValueError` | Raised in `BlobUploadSession.__post_init__` |
| `location` is empty in `from_location` | `BlobError` | Raised before parsing |
| `location` path does not start with `/v2/` | `BlobError` | Raised after parsing |
| UUID segment absent from `location` path | `BlobError` | Raised after parsing |

`BlobError` is a subclass of `RegShapeError` added to `libs/errors.py`,
parallel to `ManifestError` and `TagError`.

---

## Domain Operations (`libs/blobs/`)

The blob models are consumed by the domain operations module, which follows
the same conventions as `libs/manifests/` and `libs/tags/`.

### Module Structure

```
src/regshape/libs/blobs/
├── __init__.py       # Exports: head_blob, get_blob, delete_blob,
│                     #          upload_blob, upload_blob_chunked, mount_blob
└── operations.py
```

### Function Summary

All functions accept a `RegistryClient` as their first argument. The 401 →
auth → retry cycle is handled transparently by the client; domain functions
never inspect `WWW-Authenticate` headers or call `resolve_credentials`.

| Function | Decorator | OCI endpoint(s) | Returns |
|---|---|---|---|
| `head_blob(client, repo, digest)` | `@track_time` | `HEAD /v2/{repo}/blobs/{digest}` | `BlobInfo` |
| `get_blob(client, repo, digest, output_path, chunk_size)` | `@track_time` | `GET /v2/{repo}/blobs/{digest}` | `BlobInfo` |
| `delete_blob(client, repo, digest)` | `@track_time` | `DELETE /v2/{repo}/blobs/{digest}` | `None` |
| `upload_blob(client, repo, data, digest, content_type)` | `@track_scenario("blob upload")` | `POST` + `PUT` | `str` (confirmed digest) |
| `upload_blob_chunked(client, repo, source, digest, content_type, chunk_size)` | `@track_scenario("blob upload chunked")` | `POST` + N×`PATCH` + `PUT` | `str` (confirmed digest) |
| `mount_blob(client, repo, digest, from_repo)` | `@track_time` | `POST` (with `mount=` + `from=`) | `str` (confirmed digest) |

### `@track_scenario` rationale

`upload_blob` and `upload_blob_chunked` are both decorated with
`@track_scenario` because each is a multi-step protocol workflow across
two or more semantically distinct HTTP calls:

- **`upload_blob`** — POST (initiate session) + PUT (commit content): two calls
  with different HTTP methods, different paths, and different semantic purposes.
- **`upload_blob_chunked`** — POST (initiate) + N×PATCH (stream chunks) +
  PUT (commit): N is unknown until the source is exhausted.

By contrast, `mount_blob` is a single POST regardless of outcome (201 = success,
202 = server cannot mount) and gets `@track_time`.

### Error Helpers (`_raise_for_blob_error`, `_raise_for_upload_error`)

Two private helpers handle non-2xx responses, following the
`_raise_for_manifest_error` / `_raise_for_list_error` / `_raise_for_delete_error`
pattern from the manifest and tag operations.

**`_raise_for_blob_error`** — used by `head_blob`, `get_blob`, `delete_blob`:

| Status | Raises | Message |
|---|---|---|
| 401 | `AuthError` | `Authentication failed for {registry}` |
| 404 | `BlobError` | `Blob not found: {registry}/{repo}@{digest}` |
| 405 | `BlobError` | `Operation not supported by this registry` |
| other | `BlobError` | `Registry error for {registry}/{repo}` with status code |

**`_raise_for_upload_error`** — used by the POST, PATCH, and PUT stages of
both upload functions:

| Status | Context | Raises | Message |
|---|---|---|---|
| 401 | any stage | `AuthError` | `Authentication failed for {registry}` |
| 404 | PATCH or completing PUT | `BlobError` | `Upload session not found: {session_id}` |
| 400 | completing PUT | `BlobError` | `Invalid upload: {detail}` (covers `DIGEST_INVALID`, bad `Content-Range`) |
| 416 | PATCH | `BlobError` | `Offset mismatch: registry expected {registry_offset}, client sent {client_offset}` |
| other | any stage | `BlobError` | `Registry error during blob upload` with status code |

The two helpers exist because the same `404` status means fundamentally
different things in a content-read context ("blob does not exist") versus an
upload context ("upload session has expired or was never created").

### `get_blob` streaming and digest verification

`get_blob` passes `stream=True` to `RegistryClient.request()` when
`output_path` is provided. The response body is read in `chunk_size`-byte
increments and written to the file at `output_path` while simultaneously
feeding a running `hashlib.sha256()` digest. After the last chunk is written:

1. The computed digest is compared against the requested `digest` parameter.
2. If they do not match, the partially-written file is deleted and a `BlobError`
   is raised — the caller never receives a `BlobInfo` for corrupted content.
3. If they match, `BlobInfo` is constructed from the response headers and
   returned.

When `output_path` is `None`, the blob is read fully into memory (for small
blobs such as config objects) and the same digest check applies before
returning.

### `upload_blob_chunked` offset tracking

The domain function maintains a `BlobUploadSession` internally:

1. Issue `POST /v2/{repo}/blobs/uploads/` → `201` response → parse
   `BlobUploadSession.from_location(response.headers["Location"])`.
2. Read `chunk_size` bytes from `source`. If EOF on first read, fall through
   to the completing PUT immediately.
3. For each chunk: issue `PATCH <session.upload_path>` with
   `Content-Range: {session.offset}-{session.offset + len(chunk) - 1}/*` and
   `Content-Length: {len(chunk)}`. On `202`, update `session.offset += len(chunk)`.
4. On EOF: issue `PUT <upload_path>?digest={digest}` with an empty body and
   `Content-Length: 0`. Confirm `201` response and return the
   `Docker-Content-Digest` header value.

The `BlobUploadSession` is never returned to the caller — it is an internal
state machine for the upload protocol.

### `mount_blob` 202 handling

A `202 Accepted` response to the mount POST means the registry either does not
support cross-repo mounting or the source blob is not accessible. This is
treated as a `BlobError` rather than a silent success, with a message that
indicates the fallback action the caller should take:

```
Blob mount not accepted for {registry}/{repo}@{digest}:
  registry returned 202 — retry with upload_blob or upload_blob_chunked
```

This forces an explicit decision at the call site rather than silently
treating a no-op as success.

---

## OCI Spec References

- **Checking blob existence**: `HEAD /v2/<name>/blobs/<digest>` — end-2 in the
  OCI Distribution Spec. Returns `200 OK` with headers; body must be empty.
- **Fetching a blob**: `GET /v2/<name>/blobs/<digest>` — end-2. Returns
  `200 OK` (direct) or `307 Temporary Redirect` (external storage). The
  `requests` library follows redirects by default.
- **Deleting a blob**: `DELETE /v2/<name>/blobs/<digest>` — end-10. Returns
  `202 Accepted` with no body.
- **Initiating an upload**: `POST /v2/<name>/blobs/uploads/` — end-4a/4b.
  Returns `202 Accepted` with a `Location` header for the upload URL.
- **Chunked upload (PATCH)**: `PATCH <upload-url>` — end-6. Requires
  `Content-Range` and `Content-Length` headers. Returns `202 Accepted` with
  an updated `Location` header.
- **Completing an upload (PUT)**: `PUT <upload-url>?digest=<digest>` — end-6.
  Returns `201 Created` with `Docker-Content-Digest` header.
- **Monolithic upload (POST+PUT)**: `POST /v2/<name>/blobs/uploads/?digest=<digest>`
  followed by `PUT` — end-4a variant. Some registries support `POST` +
  single-step `PUT` without intermediate `PATCH`.
- **Cross-repo mount**: `POST /v2/<name>/blobs/uploads/?from=<repo>&mount=<digest>`
  — end-11. Returns `201 Created` (success) or `202 Accepted` (fallback).

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `RegShapeError` base class for `BlobError`
- `regshape.libs.transport` — `RegistryClient` (domain operations only)
- `regshape.libs.decorators.timing` — `track_time` (domain operations only)
- `regshape.libs.decorators.scenario` — `track_scenario` (domain operations only)
- `regshape.libs.models.error` — `OciErrorResponse` (domain operations only)

**External (stdlib only):**
- `hashlib` — SHA-256 digest verification in `get_blob`
- `dataclasses` — `@dataclass`
- `typing` — `Optional`, `BinaryIO`
- `urllib.parse` — `urlparse` in `BlobUploadSession.from_location`

No third-party dependencies in the model layer.

---

## Open Questions

- [ ] Should `get_blob` with `output_path=None` cap the in-memory buffer size
  and raise a `BlobError` if the blob exceeds it? Current proposal: no cap —
  callers that may encounter large blobs are expected to supply `output_path`.
- [ ] Should `upload_blob_chunked` update `session.upload_path` from the
  `Location` header returned by each `202 PATCH` response (some registries
  rotate the upload URL per chunk), or reuse the initial path throughout?
  Current proposal: always use the `Location` from the most recent `202`
  response if present, fall back to the initial path if the header is absent.
- [ ] Should `upload_blob` accept a `BinaryIO` source in addition to `bytes`,
  to unify the caller interface with `upload_blob_chunked`?
  Current proposal: `bytes` only for `upload_blob` (monolithic implies the
  payload is already in memory); `BinaryIO` only for `upload_blob_chunked`.

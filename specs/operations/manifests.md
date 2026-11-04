# Operations: Manifests

<!-- doc-type: reference -->

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
    *,
    progress_callback: ManifestProgressCallback | None = None,
) -> tuple[str, str, str]:
```

Fetches the manifest body for a repository reference.
The optional observations described here target the 0.2 development interface;
older installed libraries may not accept `progress_callback`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name (e.g. `"myrepo/myimage"`) |
| `reference` | `str` | required | Tag or digest (e.g. `"latest"` or `"sha256:abc..."`) |
| `accept` | `str` | required | Value for the `Accept` header |
| `progress_callback` | `ManifestProgressCallback \| None` | `None` | Keyword-only synchronous byte observer; enables streamed response consumption |

**Returns:** `(body_str, content_type, digest)` — the response decoded using
Requests' text semantics, the `Content-Type` response header value, and the
`Docker-Content-Digest` response header value (empty strings for absent headers).
Fetching does not parse the model or verify the reported digest.

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{reference}"`.
2. Call `client.get(path, headers={"Accept": accept})`; with an observer, add
   `stream=True` and observe nonempty chunks requested in 64 KiB reads.
3. Pass response to `_raise_for_manifest_error`.
4. Decode the complete payload with unchanged Requests charset/detection/fallback
   behavior. Even observed calls retain O(payload size) memory.
5. Close the owned response before emitting `RECEIVED` and returning the tuple.

The additive frozen `ManifestProgress` event uses opaque invocation identity,
advertised manifest/index/unknown kind, receive phase, absolute consumed payload
bytes and an optional advisory total. It has no digest or verification claim.
All event types and `ManifestProgressCallback` are exported from the manifest
package and [shared progress module](../../src/regshape/libs/progress.py).
See [manifest callback details](../../docs/library/transfer-progress.md#manifest-receive-observations)
for fields, error/cleanup semantics and examples.

No callback preserves buffered request behavior and emits no observations.
Observed calls bypass the optional response cache; buffered cache reuse is
unchanged. No additional HEAD or probe is performed solely for progress.
Existing authentication/retry behavior is retained, with discarded streamed
responses closed before authentication exchange or retry backoff. Post-response
diagnostic failures close an acquired stream before it can be handed to the
operation, preserving the original exception if cleanup also fails.
Non-2xx bodies are read before tolerant OCI error parsing, so framing/read
failures propagate unchanged rather than being swallowed as malformed error JSON.

**Failures and ownership:** A non-callable observer raises `TypeError` before the
GET. Observer errors propagate unchanged and disable further notifications.
Transport/domain errors and interruption retain their existing exceptions;
secondary cleanup/observer errors do not replace the primary failure. The fetch
owns and closes its response, not the caller's client. `RECEIVED` means the body
was consumed, decoded and closed; parsing, digest validation, storage and output
are not performed by this operation. A `RECEIVED` callback can still raise, in
which case no tuple is returned.

Streamed request debug retains its existing schema: absent body preview and
`body_size: 0` mean no body captured at that layer, not an empty payload.
Request timing ends at headers; operation timing includes consumption.
The library remains terminal-free; [manifest command rendering](../cli/manifest.md#manifest-get)
is a separate caller of this observation contract.
See the [manifest workflow](../../docs/guides/live-transfer-progress.md#download-a-manifest-or-index)
for command examples, progress snapshots and troubleshooting.

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

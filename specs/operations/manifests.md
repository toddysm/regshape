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
├── download.py        # Byte-preserving verified manifest-to-file receive
├── _stream.py         # Bounded entity decoding and pre-redirect rejection
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

### `download_manifest`

The additive 0.2 development API preserves entity bytes in a new file rather than
returning decoded text. It is the raw-receive foundation for
[full artifact download](../cli/artifact-pull.md), not a recursive artifact pull.
The existing `get_manifest` and `manifest get` behavior is unchanged.

```python
download_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
    output_path: str | Path,
    *,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    max_bytes: int = 16777216,
    progress_callback: ManifestProgressCallback | None = None,
) -> ManifestDownload
```

The first four inputs have the same roles as `get_manifest`. The new operation
validates relative repository components, registry tags and strict lowercase
SHA-256/SHA-512 digest syntax before file/network activity; it rejects URL
queries/fragments and traversal. `accept` must be a nonempty ASCII header value
without controls. This does not change validation in existing operations.

| Parameter | Meaning |
| --- | --- |
| `output_path` | Required new filename in an existing, trusted caller-controlled parent directory. Existing files, directories and symlinks are never overwritten; missing parents are not created. |
| `expected_digest` | Optional additional descriptor digest. All requested, expected and reported digests are verified, including different supported algorithms. Contradictory requested/expected digests of the same algorithm fail preflight. |
| `expected_size` | Optional nonnegative integer entity size, including known-empty zero. A mismatch fails; this is not an advisory HTTP length. |
| `max_bytes` | Positive integer cap, default 16 MiB. A declared expected size above it fails preflight. Actual consumption enforces it even without an observer. Booleans are not accepted as sizes/limits. |
| `progress_callback` | Optional synchronous manifest observer; does not control whether requests stream or integrity checks run. |

`ManifestDownload` is a frozen dataclass exported with the function from
`regshape.libs.manifests`:

| Field | Type | Meaning |
| --- | --- | --- |
| `path` | `str` | Selected output filename, caller-owned after return. |
| `digest` | `str` | Verified requested digest, otherwise expected digest, otherwise verified registry digest, otherwise computed SHA-256. |
| `size` | `int` | Entity bytes saved after HTTP content-coding removal. |
| `media_type` | `str` | Content-Type header as received, including parameters; empty if absent. Not a schema-validation claim. |
| `reported_digest` | `str` | Verified Docker-Content-Digest header; empty only when absent. A present empty/malformed/unsupported header fails. |

Successful responses are consumed in requested chunks no larger than 64 KiB,
written and hashed without decoding, JSON parsing, reserialization or a complete
in-memory payload. Here "without decoding" means no charset/text decoding:
HTTP content coding is removed using bounded incremental decoding rather than
urllib3's version-dependent decompression buffering. The request asks for
`Accept-Encoding: identity`; gzip (including concatenated members) and wrapped/raw
deflate are supported if the server still encodes the response. Unsupported or
stacked encodings, incomplete streams, or corrupt/trailing encoded data fail
explicitly. Invalid UTF-8 or non-JSON bytes can be saved if their identity
and size are valid; higher-level graph code is responsible for schema checks.
Deflate first uses its required zlib wrapper and permits raw-deflate compatibility
fallback only before emitting any entity bytes, retaining at most 64 KiB of
encoded prefix. A valid-looking two-byte zlib prefix alone does not choose the
format. If ambiguity cannot be resolved within that bound or after output starts,
the operation fails instead of replaying already emitted bytes.
Error-body decoding is bounded by the smaller of `max_bytes` and 64 KiB and
rejects overlarge errors. Framing/read errors propagate before tolerant OCI
error parsing; bounded error buffering does not change successful payload bytes.
The raw reader explicitly enables urllib3 wire-framing length enforcement even
on versions that default it off; a truncated framed body cannot become success.
Unsolicited `206` or `Content-Range` responses fail before consumption: this
operation neither requests nor assembles partial manifests.

This new primitive rejects redirects in a pre-redirect Requests response hook,
before Session can eagerly consume an intermediate redirect body. The hook is
preserved through client middleware, retries and authentication probes in both
transport modes. This deliberate restriction protects the bounded receive
contract; use a direct manifest endpoint. Existing decoded manifest/blob requests,
including their redirect behavior, are unchanged. The optional transport
`response_hook` keyword is additive and absent from ordinary requests.

Lifecycle is PREPARING, TRANSFERRING, then RECEIVED after digest/size checks,
response close and file flush/close. Kind comes from the advertised type.
Unknown/unusable or inconsistent HTTP sizes follow the shared advisory-total
policy; descriptor-size mismatches still fail. All callbacks share one identity.
No progress-only HEAD/refetch or partial-body resume is performed. Streamed
cache bypass/authentication/retry cleanup applies even when no observer is supplied.

The operation borrows the client and owns only its response/new output handle.
The caller must exclude concurrent mutation of the trusted parent/output;
this low-level file primitive is **not** the hostile-path-safe graph publication
boundary planned in #91. Identity checks reject a replaced output entry before
return and refuse to delete its replacement.

Invalid inputs raise `ValueError` (`TypeError` for a non-callable observer);
registry/integrity/cap failures raise `ManifestError`, with existing `AuthError`
and Requests transport failures retained. File errors are `OSError` subclasses.
Observers and cancellation propagate unchanged and disable recursive observer
notifications. Failures remove the owned output where safe; secondary close/
cleanup failures are logged without replacing the primary error. A failing
RECEIVED observer prevents a return and removes the file. A later independent
caller or diagnostic failure after successful return is not transactional with
that complete file.

Example (authenticate separately; replace the synthetic source and use a new file):

```python
from regshape.libs.manifests import download_manifest
from regshape.libs.transport import RegistryClient, TransportConfig

client = RegistryClient(TransportConfig(registry="registry.example.com"))
received = download_manifest(
    client, "team/example", "v1",
    "application/vnd.oci.image.manifest.v1+json",
    "manifest.raw",
)
print(received.digest, received.size)
```

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
    *,
    expected_digest: str | None = None,
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
| `expected_digest` | `str \| None` | `None` | Keyword-only strict SHA-256/SHA-512 payload identity |

**Returns:** `str` — the `Docker-Content-Digest` response header value for legacy
callers, or the verified `expected_digest` when supplied.

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{reference}"`.
2. Call `client.put(path, headers={"Content-Type": content_type}, data=body)`.
3. Pass response to `_raise_for_manifest_error`.
4. With `expected_digest`, verify the submitted bytes and any digest reference
   locally before PUT, require `201 Created`, and validate every present
   `Docker-Content-Digest` against those exact bytes. A valid digest using the
   other supported algorithm is accepted; empty/malformed/mismatched headers
   are rejected. An absent header is allowed with the verified submitted payload.
5. Close the owned response on success/failure, preserving an active error if
   cleanup also fails. The client remains caller-owned. Omitting the argument
   preserves the existing any-2xx/header return behavior.

Strict acknowledgement failures raise `ManifestError`; malformed strict inputs
raise `ValueError` before network work. Transport exceptions propagate. Receipt
of an error or loss of the response does not imply a remote rollback.

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

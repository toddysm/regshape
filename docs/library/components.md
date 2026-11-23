# Config and layer download SDK

<!-- doc-type: reference -->

`regshape.libs.components` provides artifact-neutral component receipt without
Click or `regshape.cli`. These are **unreleased 0.2 development APIs**.
See the [user workflow](../guides/artifact-components.md), the
[command reference](../../specs/cli/components.md), and
[implementation](../../src/regshape/libs/components.py).

## Entry points

```python
from collections.abc import Callable
from pathlib import Path

from regshape.libs.artifacts import ArtifactLimits, ArtifactPhaseCallback
from regshape.libs.blobs import DownloadOptions, DownloadStatus
from regshape.libs.components import Component, ComponentDownloadResult
from regshape.libs.progress import ManifestProgressCallback, ProgressCallback
from regshape.libs.transport import RegistryClient

def download_config(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    limits: ArtifactLimits | None = None, chunk_size: int = 65536,
    progress_callback: ProgressCallback | None = None,
    manifest_progress_callback: ManifestProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ComponentDownloadResult: ...

def download_layer(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    layer_index: int, limits: ArtifactLimits | None = None, chunk_size: int = 65536,
    progress_callback: ProgressCallback | None = None,
    manifest_progress_callback: ManifestProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ComponentDownloadResult: ...

def preflight_component_download(
    registry: str, repo: str, reference: str, output_path: str | Path, *,
    component: Component, layer_index: int | None = None,
    limits: ArtifactLimits | None = None,
) -> None: ...

def validate_component_source(registry: str, repo: str, reference: str) -> None: ...
```

`Component` is `Literal["config", "layer"]`. Both downloads use the registry
from `client.config.registry`; pass a strict relative OCI repository and a tag
or lowercase SHA-256/SHA-512 manifest digest as separate arguments. The helper
validates registry authority (optional numeric port, no scheme), repository
and reference without credentials, storage probes or telemetry.

`output_path` is a required absent file, not a stream or directory; empty
strings and `"-"` are refused. No default filename, overwrite, append, force,
resume or verify-only mode exists. Files, directories and dangling links already
at the output are errors. Native Linux/macOS storage and
[trusted-parent prerequisites](../guides/artifact-components.md#before-you-start-output-and-trust)
apply to direct SDK callers too; Windows fails closed.

`layer_index` must have `type(layer_index) is int` and be nonnegative: booleans,
strings, floats and `None` are not coerced. Single-layer selection is 0.
Manifest-dependent bounds/config availability are checked after receipt, before
blob acquisition. Only one simple OCI/Docker schema-2 manifest is selected;
no index/subject traversal or platform choice occurs. Arbitrary config/layer
media types and opaque bytes are supported.

### Preflight and caller ownership

Preflight checks source/selector/output support and temporarily creates/removes
a private filesystem probe before credentials or registry effects. It does not
reserve the destination, fetch a manifest, or prove that a layer position exists.
Downloads repeat checks under retained handles. Use
`validate_component_source` when only syntax validation is wanted.

The operation borrows the client; it neither closes nor reconfigures it.
Pooled workers own their operation-scoped transport resources and use the
borrowed client's resolved credential snapshot. See
[scoped authentication and ownership](downloads.md#scoped-authentication-and-ownership).
No authentication flags or interactive login are owned by this API.

### Limits and transport

`limits=None` constructs `ArtifactLimits()`. Relevant byte defaults are
`max_manifest_bytes=16777216` and `max_total_bytes=1099511627776`.
The latter counts distinct **verified manifest plus selected blob** bytes:
equality succeeds, overflow fails, and duplicate content addresses count once.
The manifest receiver is capped at the smaller of those two limits; the
selected descriptor size is reserved before blob receipt. A zero-byte blob
still requires a matching digest and exact declared size; any extra byte fails.
There is no separate component `max_blob_bytes` argument.
Strict parsing also enforces shared structural caps, including
`max_json_depth=128` and `max_edges=500000`.
Every `ArtifactLimits` field must be a positive non-boolean integer.

`chunk_size` is a positive non-boolean integer I/O buffer size, not range size;
pooled reads remain capped at 64 KiB.
`download_options=None` uses the
[shared transport resolution/defaults](downloads.md#policy); supported clients
default to pooled C=4/W=1, with the existing custom-client serial fallback.
Use `DownloadOptions.sequential()` for pooled C=W=1, or
`DownloadOptions.legacy()` for original serial transport. Explicit unsupported
pooled customization fails before effects.

Range/retry/status behavior is shared with blob receipt. Scratch defaults to
private component staging; explicit `scratch_dir` wins and must be caller-trusted.
`download_status_callback` requires pooled transport: pairing it with legacy
is invalid. The API does not fetch descriptor URLs or fall back to embedded
`data`, even if valid. Complete-artifact pull retains its embedded fallback.

## Result

`ComponentDownloadResult` is a frozen dataclass with only scalar metadata,
returned **only after publication, owned cleanup and terminal callbacks succeed**:

| Field | Python type | Meaning |
| --- | --- | --- |
| `source` | `str` | Formatted registry/repository plus supplied tag/digest. |
| `resolved_reference` | `str` | Digest-pinned verified simple-manifest reference. |
| `component` | `Component` | `"config"` or `"layer"`. |
| `layer_index` | `int \| None` | Zero-based position, or `None` for config. |
| `digest` | `str` | Verified selected descriptor digest. |
| `size` | `int` | Exact payload byte count, including zero. |
| `media_type` | `str` | Descriptor media type; bytes are not interpreted. |
| `output_path` | `str` | Absolute file path. |

There is no result after a late failure, even when complete output remains.
Do not treat lack of a result as evidence of absent output.

## Callback lifetime and publication

Observers are optional, synchronous, immutable typed snapshots delivered on the
invoking thread, not worker threads. Non-callables fail before effects.

| Keyword | Event and lifetime |
| --- | --- |
| `manifest_progress_callback` | `ManifestProgress`; one manifest receipt, ending at `RECEIVED`, not whole-operation success. |
| `progress_callback` | `TransferProgress`; actual selected blob bytes with the trusted descriptor total and one transfer identity across retries. Receipt completion is exposed as `COMMITTING`; terminal `COMPLETED` waits for publication and owned cleanup. |
| `phase_callback` | `ArtifactPhase.PUBLISHING` immediately before the irreversible public hard-link step. Raising here still prevents publication. |
| `download_status_callback` | Pooled `DownloadStatus`; live strategy/retry/failure records, then deferred `completed`/`summary` only after publication and owned cleanup. |

Progress terminal completion is delivered before deferred status records.
Callbacks may abort by raising; the **same exception object** propagates,
including domain exceptions, I/O errors and `KeyboardInterrupt`. No wrapper
turns a callback exception into an operation-domain error.
See [event fields](transfer-progress.md#event-fields) and
[download status schemas](downloads.md).

The public exclusive hard-link syscall is the retained-output boundary, even
if Python never observes its return. Prior receipt, verification or
receiver-to-staging handoff is not publication. **Even verified private bytes
are removed on prepublication callback/transport-teardown failure when safe.**
Late terminal callback, cleanup or identity-verification errors after the link
can fail the call while retaining the complete public file; the operation
never rolls it back.

Cleanup only removes identities it owns. Replacement entries are retained,
not adopted or deleted. Secondary cleanup/close failures are reported without
replacing the primary; a sole cleanup failure propagates. Hostile same-user
mutation and process kills are outside automatic cleanup guarantees. Publication
is not an `fsync` crash-durability guarantee.

## Errors

Import errors from `regshape.libs.errors`. **`ComponentError` subclasses
`ArtifactError`**, so an artifact-domain catch also catches component errors.

| Error | Conditions |
| --- | --- |
| `ComponentError` | Component-specific source/selector/output policy, index target, missing config, empty/out-of-range layers, total-budget reservation or operation-owned file I/O. Owned `OSError` causes are chained; output collisions never authorize replacement. |
| `ArtifactError` | Strict manifest/descriptor/structural parsing, unsupported platform/native filesystem, unsafe parent/ACL, storage identity or cleanup policy. Not all failures are normalized to `ComponentError`. |
| `ManifestError` | Shared manifest acquisition, integrity or receipt cap. |
| `BlobError` | Shared selected blob missing/corrupt/digest/size failure; `status_code` may identify HTTP 404. |
| `AuthError`, `requests.RequestException`, `OSError` | Existing auth/receiver/transport/I/O failures pass through where not operation-owned I/O. |
| `ValueError` | Invalid limits, chunk size or download policy, including legacy/status incompatibility. |
| `TypeError` | A supplied observer is not callable. |
| Exact caller exception | Any observer failure or cancellation, retaining object identity. |

`preflight_component_download` raises `ComponentError`, native/storage
`ArtifactError`, or invalid-limit `ValueError`; it does not classify remote
manifest/selection failures. `validate_component_source` instead raises
`ReferenceValidationError` (a `ValueError` subtype), with diagnostics omitting
the invalid source.
See [CLI status and late-metadata guidance](../../specs/cli/components.md#output-publication-and-process-status)
when using the command rather than Python.

## Network-free SDK example

Run this complete block from a development checkout using `PYTHONPATH=src python3`.
It uses the existing **test-only** fake registry, not a public fixture API.
There are no sockets, credentials or CLI imports; native storage operations
and trust requirements are real. Only the known temporary parent is resolved
for macOS aliases. Temporary outputs are removed when the block exits.

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from regshape.libs.components import download_config, download_layer
from regshape.tests.artifact_fixtures import Registry, address, document, edge

config_bytes = b' { "opaque": true }\n '
layer_bytes = b"\x1f\x8bopaque non-image content\x00\xff"
manifest = document(
    config=edge(config_bytes, "application/example.config"),
    layers=[edge(layer_bytes, "application/example.layer")],
)
registry = Registry(
    manifest, {address(raw): raw for raw in (config_bytes, layer_bytes)},
)
with TemporaryDirectory() as temporary:
    parent = Path(temporary).resolve()
    config = download_config(registry, "repo", "moving", parent / "config.bin")
    layer = download_layer(
        registry, "repo", address(manifest), parent / "layer.bin", layer_index=0,
    )
    assert Path(config.output_path).read_bytes() == config_bytes
    assert Path(layer.output_path).read_bytes() == layer_bytes
    assert config.layer_index is None and layer.layer_index == 0
    assert config.resolved_reference == layer.resolved_reference
    assert config.size == len(config_bytes) and layer.digest == address(layer_bytes)
    assert len(registry.calls) == 4
print("Verified exact config and layer 0 bytes from tag/digest targets.")
```

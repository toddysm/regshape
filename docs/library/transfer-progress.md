# Transfer progress callbacks

<!-- doc-type: reference -->

The Python library exposes optional, synchronous observations of blob
transfers and manifest receipt. Library functions do not render progress bars;
configured telemetry
remains separate. For the command-line
display, see [Monitor live transfer progress](../guides/live-transfer-progress.md).
This reference describes the current development implementation and releases
that include it, not necessarily the latest published package. The project
requires Python 3.10 or newer; confirm the installed function signatures expose
the keywords below.

## Imports and supported operations

```python
from regshape.libs.progress import (
    ProgressCallback,
    TransferDirection,
    TransferPhase,
    TransferProgress,
)
```

These names are also exported by `regshape.libs.blobs`.
`ProgressCallback` is `Callable[[TransferProgress], None]`.
The table lists progress-related keywords; complete operation signatures are
linked below. New observer/total parameters are keyword-only.

| Operation | Observer keyword | Trusted total |
| --- | --- | --- |
| `get_blob` | `progress_callback=None` | `total_size=None`; derives a usable response length when observing |
| `upload_blob_stream` | `progress_callback=None` | `total_size=None`; seekable source length is inferred |
| `upload_blob_chunked` | `progress_callback=None` | `total_size=None`; supply only a length you trust |
| `push_layout` | `transfer_callback=None` | Per-blob descriptor sizes |
| `push_image` | `transfer_callback=None` | Forwarded unchanged to layout push |

The existing bytes-based `upload_blob(...)` API is unchanged and does not gain
byte callbacks. Use `upload_blob_stream(...)` for a file-backed monolithic
upload; this preserves the POST-plus-PUT protocol instead of silently choosing
chunked uploads.

See the complete [blob signatures](../../specs/operations/blobs.md),
[layout push signature](../../specs/cli/layout-push.md), and
[Docker push signature](../../specs/docker/docker-desktop-integration.md).

## Event fields

`TransferProgress` is a frozen dataclass. Events are immutable snapshots,
not instructions to redraw or byte deltas.

| Field | Contract |
| --- | --- |
| `transfer_id: str` | Opaque client invocation identity, stable across that blob's replay attempts |
| `digest: str` | Valid SHA-256 or SHA-512 blob digest |
| `direction: TransferDirection` | `UPLOAD` or `DOWNLOAD` |
| `phase: TransferPhase` | Lifecycle state listed below |
| `bytes_completed: int` | Absolute logical processed/accepted bytes in the current attempt |
| `total_bytes: int \| None` | Trusted payload length; `None` means unknown, `0` means known empty |
| `attempt: int` | One-based logical attempt; monolithic source replay increments it |

Construction requires a nonempty identity, a valid digest, the declared enum
types, and nonnegative integer counts (not booleans). Attempts start at one;
counts cannot exceed a known total. Invalid values raise `ValueError`, or
`TypeError` for an invalid direction/phase type.

Phases are `PREPARING`, `TRANSFERRING`, `RETRYING`, `VERIFYING`, `COMMITTING`,
`COMPLETED`, `SKIPPED`, `FAILED`, and `CANCELLED`. Their string values are
lowercase, for example `TransferPhase.COMPLETED.value == "completed"`.
Not every operation emits every phase: an existing layout blob can emit only
`SKIPPED`, and a download does not commit an upload.

Do not sum successive `bytes_completed` values. Phase transitions can repeat
the same count, and a replay resets it. Downloads count consumed payload chunks;
chunked uploads count successfully accepted chunks; monolithic uploads count
bytes consumed by the Hypertext Transfer Protocol (HTTP) client, not proof of
registry acceptance.
The streamed protocols require a final PUT with `201 Created` and applicable
digest checks before completion.

Callbacks receive all library observations even when a command-line renderer
coalesces its screen updates. Keep callbacks lightweight and synchronous;
do not pass an `async def` callback.

## Collect state without printing per-event lines

Given a stable local `blob.bin` and access to your registry:

```python
import hashlib
from pathlib import Path

from regshape.libs.blobs import TransferProgress, upload_blob_stream
from regshape.libs.transport import RegistryClient, TransportConfig

payload = Path("blob.bin")
hasher = hashlib.sha256()
with payload.open("rb") as source:
    while chunk := source.read(65536):
        hasher.update(chunk)
digest = "sha256:" + hasher.hexdigest()

latest: dict[str, TransferProgress] = {}

def remember(event: TransferProgress) -> None:
    """Keep the latest state for a digest instead of appending console lines."""
    latest[event.digest] = event

client = RegistryClient(TransportConfig(registry="registry.example.com"))
with payload.open("rb") as source:
    confirmed = upload_blob_stream(
        client, "team/example", source, digest,
        progress_callback=remember,
    )
```

Your application can redraw one row from `latest` or consume the events
without any display. When recording separate invocations of the same digest,
use `transfer_id` as well; digest alone does not identify independent operations.

The same observer works for downloads and chunked uploads:

```python
from regshape.libs.blobs import get_blob, upload_blob_chunked

info = get_blob(
    client, "team/example", digest, output_path="downloaded.bin",
    progress_callback=remember,
)

with payload.open("rb") as source:
    confirmed = upload_blob_chunked(
        client, "team/example", source, digest,
        total_size=payload.stat().st_size,
        progress_callback=remember,
    )
```

Only use that file-size total when the file is stable and its metadata describes
its content. Pipe-backed and virtual-file size hints are not reliable totals.

## Ownership, unknown totals, and replay

- Upload sources are **borrowed and never closed by the blob API**. Their
  position advances as they are consumed; callers own the handle.
- A seekable monolithic source is replayed from its original offset for
  authentication, redirects, or configured retries. A supplied total must match
  its remaining length.
- A nonseekable monolithic source requires an explicit total that frames one
  payload. The operation reads that frame without waiting for a following EOF
  and fails before replaying a consumed source that cannot be rewound.
- Chunked upload reads until EOF; its optional total is a validation bound,
  not a framing instruction. It does not seek the caller's source.
- Buffered chunk retries count only once after success and retain logical
  `attempt == 1`. Monolithic replays emit `RETRYING`, reset counts, and increment
  the attempt. Progress does not enable retry policy or resumability.
- Downloads infer a total only from a valid length without transfer encoding
  and with identity content encoding. Otherwise it remains unknown.
- Explicit total mismatches fail rather than clamp a counter or report success,
  including when no observer is supplied.
- Downloads own and close their responses/output handles. Digest mismatch
  removes invalid output; other failures can leave partial files.
- Layout push owns its file handles; Docker push also owns its temporary layout.

## Failures and cancellation

Once a transfer starts, an operational observer receives a terminal `FAILED` or
`CANCELLED` observation when it fails. Input/preflight errors can occur before
any blob event. `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit`
are cancellation; other exceptions are failures.

An observer exception propagates unchanged and disables further notifications
to that observer. It is not called again to report its own failure. If a
failure notification itself raises, the original transfer exception remains
primary and only the secondary exception type is logged.

Always use the function's return or exception to determine caller success.
An observer can fail while processing `COMPLETED`; that exception does not
undo a blob already committed remotely. A completed blob also does not imply
that a later manifest push succeeded.

Events contain no credentials, URLs, request/response headers, payload content,
or raw exception text. Progress callbacks are independent of telemetry flags
and do not resolve the [separate telemetry gaps](../guides/live-transfer-progress.md#control-output-and-combine-telemetry).

## Manifest receive observations

This optional observation surface targets the 0.2 development interface; an older
installed library may not accept `progress_callback`.

The manifest fetch adds a keyword-only callback while preserving its existing
four positional arguments and three-string result:

```python
from regshape.libs.manifests import (
    ManifestProgress,
    get_manifest,
)
from regshape.libs.transport import RegistryClient, TransportConfig

manifest_latest: dict[str, ManifestProgress] = {}

def remember_manifest(event: ManifestProgress) -> None:
    """Keep the latest receipt state for an invocation, not a fabricated digest."""
    manifest_latest[event.transfer_id] = event

manifest_client = RegistryClient(
    TransportConfig(registry="registry.example.com"),
)
body, content_type, reported_digest = get_manifest(
    manifest_client, "team/example", "v1",
    accept="application/vnd.oci.image.manifest.v1+json",
    progress_callback=remember_manifest,
)
```

Replace the example registry/repository/tag with an accessible target and
authenticate separately if needed. This example does not depend on `blob.bin`
or a previously created client. It retains state without printing per-event
lines; parsing/storage/output remain the caller's responsibility.

`ManifestProgress`, `ManifestKind`, `ManifestPhase` and
`ManifestProgressCallback` are exported by `regshape.libs.manifests` and
`regshape.libs.progress`.
`ManifestProgressCallback` is `Callable[[ManifestProgress], None]`.
Omitting the callback keeps buffered requests and emits no observations.
An invalid non-callable callback raises `TypeError` before the GET request.

| Frozen event field | Contract |
| --- | --- |
| `transfer_id: str` | Nonempty opaque identity, unique per invocation and stable through client-internal authentication/retries |
| `kind: ManifestKind` | Advertised `UNKNOWN`, `MANIFEST` or `INDEX`; not payload validation |
| `phase: ManifestPhase` | `PREPARING`, `TRANSFERRING`, `RECEIVED`, `FAILED` or `CANCELLED` |
| `bytes_completed: int` | Absolute consumed payload bytes before text decoding, not characters or compressed wire size |
| `total_bytes: int \| None` | Comparable advisory size; `None` means unknown and zero means known empty |

Enum string values are lowercase. Invalid identity, enum types, booleans,
negative/noninteger counts or a count above a present total raise `ValueError`.
Manifest events have no digest, direction or attempt field; the existing blob
event's required digest and verification semantics are unchanged.

The sequence starts with zero-byte `PREPARING` (unknown kind and total), then
`TRANSFERRING` at headers and after each nonempty chunk requested in 64 KiB reads.
A small body may finish in one read; there is no artificial delay. Missing digest
headers are valid, and tag/digest targets use the same event path without a
progress-only HEAD request.

Only a valid ASCII decimal Content-Length with no transfer encoding and
identity/absent content encoding supplies a comparable total. Missing, invalid
or compressed lengths remain unknown. If a successful stream disagrees with a
comparable size hint, the total is discarded before an impossible count is
published and one warning is logged. Actual bytes are never clamped; Requests
framing/transport errors still propagate rather than returning partial success.

`RECEIVED` follows consumption, Requests-compatible text decoding and response
cleanup. It does **not** mean model-valid, digest-verified, stored or printed.
The return remains complete decoded text, not byte-exact export or a
bounded-memory API. Callers must still handle parsing/storage/output failures.

Observers run synchronously. An observer exception propagates unchanged and
disables further notifications, including when it fails on `RECEIVED`.
Preparation-observer failure aborts before the GET; a `RECEIVED` observer can
fail after the response has already closed. Do not expect a further failure
notification after the observer itself raises.
Transport/HTTP failures close the owned response and emit `FAILED`;
interruptions emit `CANCELLED`. Secondary cleanup or failure-observer errors are
logged without replacing the primary error. If logging itself fails, those
secondary failures are retained as exception context.

Observed requests bypass both reads and writes of the optional response cache;
repeated buffered fetches retain cache reuse. Two observed fetches can therefore
make two network requests where buffered fetches hit cache. This is safe stream
ownership, not an extra probe. Retry-discarded streams close before backoff; the
final response remains owned by the fetch. Diagnostics that fail after acquiring
a stream but before handoff close it at that boundary, including in legacy
transport mode.
The command always supplies an observer, including with `--no-progress`; animation
choice does not switch it back to buffered/cache-hit semantics. No new retry
policy or partial-body resumption is enabled.

Streamed request diagnostics do not capture the body: `body_preview: null`,
`body_size: 0`, and the existing text `(empty)` placeholder describe no captured
preview, not a zero-byte manifest. Request timing ends at headers, while operation
timing includes consumption. No body pre-read is added for diagnostics.

The [manifest get command](../../specs/cli/manifest.md#manifest-get) uses these
observations with the shared renderer and supports `--no-progress`. It delays
final visual success until parsing/extraction/output and in-context diagnostics
succeed; the library itself remains terminal-free.
See the [complete manifest signature](../../specs/operations/manifests.md#get_manifest).
For runnable command variants, row snapshots and recovery, see
[Download a manifest or index](../guides/live-transfer-progress.md#download-a-manifest-or-index).

## Layout compatibility

`push_layout(..., progress_callback=...)` retains the legacy string events:
`blob_start`, `blob_skip`, `blob_done`, and `manifest_done`, with their existing
keyword shapes. Use the independent keyword-only `transfer_callback` for typed
byte observations; both callbacks can be supplied together.

| Legacy event | Keyword fields |
| --- | --- |
| `blob_start` | `digest`, `size`, `media_type` |
| `blob_skip` | `digest`, `size` |
| `blob_done` | `digest`, `size` |
| `manifest_done` | `digest`, `reference` |

Typed skip events occur once per unique existing digest. Shared references do
not create another typed transfer or downgrade an uploaded digest. Legacy
callbacks and result skip counters still include repeated references, preserving
their established schemas. Skipped blobs and retransmissions do not inflate
logical uploaded-byte totals.

# Download policy, batch API and status observations

<!-- doc-type: reference -->

This development-build reference covers pooled downloads, verified ranges,
parallel artifact acquisition and public batch/status APIs, targeting **0.2**,
not a published release. Start with the
[tuning guide and singular/artifact/batch examples](../guides/parallel-downloads.md).
See also the [approved design](../../specs/operations/parallel-downloads.md).
The [blob API](../../specs/operations/blobs.md#get_blob) and
[artifact API](../../specs/operations/artifacts.md#signatures-and-caller-ownership)
accept keyword-only `download_options`; existing positional arguments, results,
output ownership and progress schemas are unchanged. Both operations also accept
`download_status_callback` on supported pooled transport.

Maintainers can reproduce correctness, performance and registry release checks
with the [download validation workflow](download-validation.md). Local fixture
results are not actual-registry certification.

## Upgrade note: default transport changes

Omitting `download_options` on `get_blob` or `pull_artifact` now selects a pooled
worker for supported clients, even if the borrowed client has
`enable_retries=False`. Unless an explicit retry policy says otherwise, pooled
downloads retry 429/transient 5xx responses up to three times, honor bounded
`Retry-After` directives, and log each attempt. Responses update the worker's
`last_response`, not the borrowed client's. Result types and positional
arguments are unchanged; these transport and observability changes are intentional.
Pooled full-stream results report the verified requested digest and consumed
entity size, including when Content-Length is absent; HEAD/legacy metadata keeps
its existing header-derived behavior.

Pass `download_options=DownloadOptions.legacy()` to either operation to retain
the original client's retry, logging/middleware and `last_response` behavior.
`DownloadOptions.sequential()` still uses pooled transport and is **not** a
compatibility rollback. The legacy example below shows the explicit rollback.
Legacy unbounded receipt also retains its original mismatch cleanup: it unlinks
the output pathname without file-identity checks and only logs deletion errors.
Do not assume the pooled worker's replacement-file protection applies to legacy.

## Policy

Import the same frozen `DownloadOptions` type from `regshape.libs.blobs` or
`regshape.libs.artifacts`.

| Field | Default | Allowed values |
| --- | --- | --- |
| `concurrency` | `4` | Integer 1..32 |
| `ranges_per_blob` | `1` | Integer 1..concurrency |
| `range_size` | `8388608` | Integer 65536..67108864 bytes |
| `max_retries` | `None` | None or integer 0..10 |
| `scratch_dir` | `None` | Existing caller-trusted directory path |
| `transport` | `"pooled"` | `"pooled"` or `"legacy"` |

Booleans are not valid integer options. Invalid policy and unsupported explicit
pooled customization raise `ValueError` before requests or output creation.
Scratch paths are pinned to absolute paths and revalidated before acquisition;
the library cannot establish the caller's trust in that directory.

Pooled retries default to three, including HTTP 429 and the existing transient
server statuses. An enabled explicit `TransportConfig.retry_config` instead
supplies its original budget, backoff, statuses and exception policy.
An explicit retry configuration with `enable_retries=False` retains its policy
with a zero budget unless a download override is supplied. The resolved budget,
including one inherited from the client, must be a non-boolean integer in 0..10
and is checked before auth/pool creation.
`max_retries` explicitly overrides its budget, including zero to disable retries.
Each request uses the original finite client timeout.
The retry budget belongs to each request, shared across its authentication
resends/probes; it is not a single artifact-wide budget or one independently
nested budget per worker. Explicit retry configuration retains its status set:
overriding only `max_retries` does not add 429 to a caller policy that excludes it.
Pooled 429/503 retries honor a valid `Retry-After` delay or HTTP date, waiting at
least the greater of that delay and configured exponential backoff after releasing
the response slot. Malformed values emit a sanitized diagnostic and retain normal
backoff. Server-directed delays above 300 seconds raise `ValueError` after the
response is closed, rather than hanging acquisition or retrying early.
Legacy retry timing is unchanged.
One retry owner wraps the complete authentication flow, so registry status and
transport retries share their budget across authentication resends. If a
headerless object 401 requires a `/v2/` challenge probe, retryable probe responses
use that same budget and bounded `Retry-After` policy, not a separate retry loop.

`DownloadOptions.sequential()` means pooled C=W=1. `DownloadOptions.legacy()`
means C=W=1 with the **original borrowed-client transport**; it requires
`max_retries=None` and `scratch_dir=None`. It never initializes the new auth
provider, shared adapter, worker session or coordinator. It retains the original
client's retry/middleware/progress and `last_response` behavior.

```python
from regshape.libs.blobs import DownloadOptions, get_blob

# client is an already configured RegistryClient; no new credential lookup.
info = get_blob(client, "team/image", digest,
                download_options=DownloadOptions.sequential())

# Genuine rollback, including unsupported existing custom clients.
info = get_blob(client, "team/image", digest,
                download_options=DownloadOptions.legacy())
```

## Verified ranged blob downloads

`get_blob` and `get_blobs` share the range engine. `ranges_per_blob=1` deliberately
uses ordinary full GET; raise it above one to enable range negotiation for blobs
at least four times `range_size`. `chunk_size` is still an I/O buffer size
(pooled reads are capped at 64 KiB), not a range size.

```python
from regshape.libs.blobs import DownloadOptions, get_blob

info = get_blob(
    client, "team/image", digest, output_path="layer.blob",
    download_options=DownloadOptions(
        concurrency=4, ranges_per_blob=3, range_size=8_388_608,
    ),
)
```

Trusted `total_size` skips HEAD. Without it, the first real range discovers size;
there is no extra one-byte probe or required HEAD. The first valid 206 is retained
as range zero. Later requests carry the established strong ETag in `If-Range`,
request identity HTTP encoding, and must agree on numeric complete length and
exact body bounds. Size metadata is limited to 0..2^63-1. Missing `Accept-Ranges`
does not prevent negotiation. A valid ignored first-range 200 is consumed as the
full entity without another GET.
Reusing that response also retains any retries already consumed while acquiring
it, including their remaining budget and exponential-backoff position.

The coordinator copies only contiguous completed parts, hashing in original
byte order. It retains at most W consecutive uncommitted parts per blob and C
range reservations overall, each at most `range_size` bytes. A later completion
cannot advance the window past a blocked prefix. Private identity-checked
temporary spools are removed only after owners stop and the caller consumes or
discards them. Verify-only hashes/discards ordered bytes; it never saves a whole
blob. Full streams still hash/write on their owning workers.
Standalone scratch defaults to the system temporary directory. Nested artifact
receipts instead create handle-relative scratch in their trusted private staging
namespace; an explicit `scratch_dir` overrides either default.
The C*R bound excludes saved outputs, artifact staging/archives and metadata.
Range spooling adds writes and ordered reads even for verify-only; tmpfs consumes
memory and disk-backed scratch may consume page cache. There is no machine-wide
or process-global resource cap across independent invocations. See
[scratch budgeting](../guides/parallel-downloads.md#reserve-scratch-space-and-account-for-extra-io).

Unknown size, absent/weak initial ETag, first-response HTTP coding, correctly
declared limited ranges and ignored ranges select explicit one-time capability
fallbacks. A 416 proves nothing by itself: only a validated zero-size first
response selects an empty full GET, which must still verify. Malformed bounds,
overflow, contradictory lengths/totals, unexpected later coding, truncated/extra
bodies, authentication failures and digest mismatch fail without capability
fallback.

An established ETag rotation on a valid 206 or late full response selects reason
`etag_changed`. Stop scheduling that blob, cancel/drain/join its old jobs, discard
old events/spools, validate and truncate/rewind the retained sink, replace the
hasher, reset accepted progress and increment attempt on the same transfer ID
before one fresh unconditional full GET. Original size/digest remain mandatory.
Unrelated batch blobs are not cancelled or replayed. A failed identity/reset
check prevents the full GET. This strategy reset is available even with zero
transport retries, and is never recursive. The full receiver still cannot retry
after a file/hash byte commits.
An interrupted range connection (including incomplete HTTP framing) can retry
only that private, uncommitted part under the configured connection-error retry
policy. Its spool is removed before replay; accepted prefixes are never replayed.
Exhausted budgets and non-retryable malformed length/body checks still fail.
Before strategy reset releases old jobs, their queued diagnostics are drained
on the invoking thread, while cancelled progress/status records are discarded.
An observed ETag change remains in terminal status even when draining or sink
reset fails; the failed attempt and accepted-byte count are not reset, and the
original failure is preserved. Progress observer errors propagate unchanged,
including `OSError` raised during caller-thread range assembly or verification.

The CLI exposes the same policy with `--concurrency`, `--ranges-per-blob`,
`--range-size`, `--download-retries`, `--scratch-dir`, `--sequential` and
`--legacy-transport`; see the [blob command reference](../../specs/cli/blob.md#blob-get)
for conflicts and examples. Changed-ETag warnings are deferred until command
cleanup/result reporting, independently of progress rendering. They use stderr
at normal verbosity, including with `--no-progress`, artifact `--json` and
nonterminal stderr. One artifact warning counts unique affected blobs; success
asserts verified fallback, while failure only reports attempted recovery. JSON
is unchanged. See [exact warning wording and recovery](../guides/parallel-downloads.md#an-etag-changed-check-the-final-outcome).
Artifact graph acquisition uses the same range engine and controls under one
operation-wide cap.

## Scoped authentication and ownership

The operation resolves no credentials: it snapshots the borrowed client's
already resolved credentials and connection configuration. Root/manifest/blob
clients share one operation-local single-flight provider. One leader discovers
the same repository's challenge and exchanges a token while followers wait.
Unsuccessful first responses do not mark discovery complete; waiting peers
still share serialized challenge discovery while a throttled request backs off.
Tokens are bound to the registry origin, credential snapshot, validated
realm/service and repository pull scope. Expiry metadata or rejection of a
generation causes one shared refresh. Anonymous and Basic flows remain supported;
foreign 401 responses cannot select token realms or warm this provider.
Concurrent rejected requests reuse a refreshed generation when their parsed
challenges match or the response omits its challenge; an explicit different realm
or service is negotiated separately.
Exchange failures are not cached: a later request can retry without replaying
another worker's exception. Fractional RFC 3339 `issued_at` timestamps, including
Docker Hub's nanosecond precision, are normalized for Python 3.10 compatibility.
Scopes use the terminal manifest/blob resource; nested repository components
named `manifests` or `blobs` remain part of the repository identity.

Token exchanges use the same instrumented HTTP hook, finite timeout and pool.
Both GET and refresh-token POST request identity encoding rather than inheriting
the session's optional compression codecs.
Token GET connection/timeouts preserve their retryable Requests exception type,
with sanitized messages, so they share the operation's retry budget. Exhaustion
surfaces that sanitized transport exception; refresh POST failures retain the
original GET 401 as described below.
External registry-issued token realms are supported without credential-bearing
URLs or HTTPS-to-HTTP downgrade. Token-endpoint redirects are rejected. The
ACR refresh-token POST fallback requires the sentinel username
`00000000-0000-0000-0000-000000000000` and a nonempty password, sends
`client_id=regshape`, and retains the original GET 401 if the fallback fails.
Ordinary rejected username/password credentials do not trigger this POST.
Challenges may omit `service` or include additional actions such as `pull,push`
on the same repository; token requests still ask only for the expected pull
scope. Different repositories or scopes without pull are rejected.
GET and refresh-token POST reject redirects in a response hook before Requests
can buffer their bodies, preserving the token-response byte limit.

Worker-local Requests sessions isolate headers, cookies, caches, retry state and
`last_response`. One blocking adapter bounds its origin-pool cache and per-origin
pool size to C; operation-wide slots stay reserved until streamed responses close,
including redirect/token requests. Sessions preserve Requests' environment proxy
and CA configuration and normal redirect credential stripping. Proxy-manager
initialization is single-flight, with at most max(2, C) proxy managers per operation.
No streamed GET
is cached. Close each response and stop/join peers before closing the context.
Closing a worker session detaches the borrowed adapter, never closes a peer's
pool. The operation closes its adapter exactly once, including when closure
detects a still-open response and raises `RuntimeError`. Callers must still close
their responses and join workers; cleanup does not make premature closure safe.
The original client is neither closed nor mutated.

The extension hook is `RegistryClient.fork_for_download(*, context)`. An exact
base client with configured built-ins is supported. Subclasses, custom middleware
or unrecognized runtime request/pipeline changes require an explicit override
that preserves customization and returns a distinct client, private configuration
and worker-local session attached to that context. Omitted options on the two
existing operations instead retain unsupported custom clients' original serial
path with one logged notice. New batch/status consumers have no such exception.
Connection/retry snapshots do not copy custom middleware; the override must
reconstruct it with isolated state, including locks. Nested blob/artifact calls
inherit their existing context's policy without reforking or compatibility
warnings, and reject a conflicting explicit policy.

`DownloadTransport(client, options)` is an internal building block for the
scheduler. It allows one discovery/root session and at most C open worker
sessions, reclaiming closed session records. Worker callers must not share one
client/session concurrently.

## Internal coordinator and full-stream workers

These are internal integration contracts, not new public download APIs.
The [coordinator](../../src/regshape/libs/transport/_coordinator.py) owns one
executor inside an existing `DownloadTransport`. Its ordered, lazy per-blob job
sources share the global `concurrency` ceiling. Each source retains at most
`ranges_per_blob` consecutive uncommitted jobs. Completed out-of-order work
keeps its slot and scratch reservation until its ordered result consumer and
cleanup finish; it cannot advance an unbounded range window.

Ready sources are selected round-robin. At most C jobs and C times `range_size`
reserved spool bytes are retained. The immutable observation queue holds at
most 4C records; a record exceeding 64 KiB is rejected, not silently truncated.
Entity payload never enters this queue. Producers check cancellation while
waiting for space, and the caller pumps records while waiting for tasks.
Completion checks visit only sources retaining jobs, not every unstarted or
retired source, so a long waiting list does not cause quadratic completion scans.

The [full-stream worker](../../src/regshape/libs/blobs/_download_worker.py)
hashes and writes its exclusive sink locally, or hashes/discards for verify-only.
There is no full-blob spool or coordinator-copy bottleneck. Callers perform
shared `ContentStore`/`SafeDirectory` bookkeeping and publication only after
the worker closes its response/output and verifies size/digest. The saved
output's identity is revalidated on the invoking thread after queued callbacks
drain and before result consumption, so a callback's replacement cannot become
a verified result. Bounded outputs are exclusive and check entity limits before
each write. Unbounded outputs
preserve overwrite and partial-file-on-most-failures behavior, including trusted
caller-owned symlink outputs. Their identity checks follow the opened target;
retargeting a link to a different object still fails verification. Bounded outputs
never overwrite symlinks. On digest mismatch, cleanup unlinks the output filename
after checking the opened object's identity, including an overwritten regular
file. For an unbounded symlink output, this removes the caller's link but leaves
its target containing the unverified bytes; callers must not use that target as
verified content. Cleanup never removes an output whose identity no longer
matches.

Worker clients disable child retry middleware. A single worker retry owner
uses the resolved download budget and server backoff. A full-stream receive may
retry only before any bytes have committed to either a file or its hasher,
including verify-only. Body failures after commitment cannot replay/append.
Configured retry exception types apply to request/body receive failures, not
sink, validation or cleanup errors.
Authentication, protocol budget and content-decoding rejections remain permanent
even when a broad configured exception policy matches them.
Backoff runs after the discarded response releases its network slot and wakes
on cancellation.

Each task has a fresh telemetry context with preserved options, private metrics
and bounded method timing aggregation. Existing progress models, internal
strategy/retry/fallback/final records, and sanitized diagnostics cross the
bounded bridge. All observer, result-consumer, terminal and library-log sinks
run on the invoking thread. Failed observers are disabled permanently and their
original exception objects propagate after producer shutdown. Parent scenario
metrics and timings are merged, never cleared by a worker scenario.
Successful terminal progress/status is emitted only after worker/session
closure, telemetry merge, caller validation, result consumption and job cleanup.
Cancellation is rechecked before consuming a result. Terminal progress and
status observers are attempted independently; one failing observer cannot hide
the terminal notification from the other. On failure, healthy terminal observers
are attempted for every affected transfer before reporting secondary cleanup
errors, so a broken diagnostic logger cannot suppress terminal notifications.

An explicit fallback reset is allowed only after old jobs drain and stop.
The caller supplies an identity-checked sink reset; accepted bytes reset,
received bytes remain cumulative, and the same row advances its attempt.
`etag_changed` is retained on fallback and verified terminal observations.
This layer emits no immediate ETag warning; the CLI observation consumer owns
deferred, coalesced warnings under
[#145](https://github.com/toddysm/regshape/issues/145).

Cancellation stops scheduling and drains/discards pending records before waiting
for every active task. Requests timeouts limit socket inactivity, not elapsed
read time: a trickling peer can keep a buffered read active. No bounded
cancellation-to-join latency is promised. Sink/scratch cleanup waits until its
worker stops, and the sole executor is joined before caller namespace cleanup.
Cleanup errors are reported without masking the original failure.

## Public batch downloads

Import `BlobDownload`, `get_blobs`, `BlobBatchResult`, `DownloadStats` and
`DownloadStatus` from `regshape.libs.blobs`. The artifact package exports the same
`DownloadStats` and `DownloadStatus` types.

```python
get_blobs(
    client, repo, downloads, *,
    download_options=None, progress_callback=None, download_status_callback=None,
) -> BlobBatchResult
```

`downloads` is consumed once and fully validated before authentication, pool
creation or output writes. It may contain at most **100,000** frozen
`BlobDownload` requests:

| Field | Default | Contract |
| --- | --- | --- |
| `digest` | Required | Strict lowercase SHA-256/SHA-512 address |
| `output_path` | `None` | Trusted filename; None hashes/discards |
| `total_size` | `None` | Trusted entity bytes, including zero |
| `max_bytes` | `None` | Positive pre-write entity cap; saved output is exclusive |
| `output_dir_fd` | `None` | Borrowed safe directory handle for one bounded filename |
| `chunk_size` | `65536` | Positive streaming buffer bytes, not range size |

Duplicate digests, aliased output paths and existing hard-link identities are
rejected. Filesystem failures inspecting output paths or directory handles raise
`BlobError` with the original `OSError` as the cause, before acquisition or output
creation. A nonexistent target is allowed when its parent is accessible.
None outputs do not collide. Parent directory identities correlate
ordinary paths and handle-relative names. This first batch API conservatively
treats Unicode-normalized/case-folded filenames in the same parent as collisions,
even on case-sensitive storage. Checks do not reserve paths or protect against
hostile namespace changes; callers retain the singular API's trust obligations.

One invocation shares the global C cap across all entries, including physical
HTTP/authentication/redirect requests. Each full-stream blob uses one worker;
out-of-order completion does not reorder `result.blobs`, a tuple of unchanged
`BlobInfo` records in input order. `result.stats` is the exact final immutable
summary snapshot. An empty batch returns an empty tuple and zero byte/request
counts, with one summary and no requests.

On the first observed failure, scheduling stops, peers drain, workers join and
the original domain/observer exception propagates. No partial result is returned.
Earlier verified standalone files may remain: this is **not atomic multi-file
publication**. Unbounded outputs retain overwrite/partial-file behavior; digest
mismatch removes only this invocation's invalid output. Bounded outputs are
exclusive and cleaned on failure by retained file identity. Directory handles
and the input client remain borrowed.

```python
from regshape.libs.blobs import BlobDownload, DownloadOptions, get_blobs

# client, digest and other_digest are supplied by the calling application.
statuses = []
result = get_blobs(
    client, "team/image",
    [BlobDownload(digest, output_path="first.blob"),
     BlobDownload(other_digest)],  # verify-only
    download_options=DownloadOptions(concurrency=2),
    download_status_callback=statuses.append,
)
assert result.blobs[0].digest == digest
assert result.blobs[1].digest == other_digest
assert result.stats is statuses[-1].stats
```

Use one batch for a shared cap; manually concurrent singular calls do not share
a process-global scheduler.

## Status and statistics

`get_blob`, `get_blobs` and `pull_artifact` accept
`download_status_callback: Callable[[DownloadStatus], None] | None = None`.
Legacy or unsupported custom transport is rejected before effects when this
new observer is requested. Existing omitted-option custom singular/artifact
calls without it retain their one-warning compatibility path. No legacy metrics
are fabricated.

Every `DownloadStatus` is frozen and contains:

| Field | Meaning |
| --- | --- |
| `transfer_id`, `digest` | Blob identity matching its progress row; both None for summary |
| `event` | `strategy`, `fallback`, `retry`, `completed`, `failed`, `cancelled`, `summary` |
| `strategy` | `full`, `ranged`, or None before selection/for summary |
| `reason` | Fixed safe code below, or None |
| `http_status` | Optional integer HTTP status, never exception/registry text |
| `attempt` | Logical attempt >=1 for a blob; None for summary |
| `stats` | Frozen per-blob or aggregate `DownloadStats` |
| `scratch_root` | Effective caller-owned scratch path; None when unused |

Reasons are `sequential`, `ranges_disabled`, `range_enabled`, `small_blob`,
`unknown_size`, `no_strong_validator`, `range_ignored`, `nonidentity_encoding`,
`short_range`, `etag_changed`, `http_status` and `transport_error`.
The facade preserves `etag_changed` on fallback and verified successful
completion, enabling a final warning without log parsing. It does not itself
activate ranges or invent a successful completion after failure.

`DownloadStats` fields are `request_count`, `retry_count`, `bytes_received`,
`bytes_accepted`, `active_requests_peak`, `scratch_bytes_peak`, `elapsed_seconds`,
`receive_seconds`, `hash_seconds`, `write_seconds` and `queue_wait_seconds`.
Counts/durations are nonnegative and durations finite. Physical request counts
include failed dispatches, authentication exchanges and redirects. Aggregate
retry counts include root/manifest retries as well as blob retries.
Received bytes count consumed blob entities, including discarded attempts,
not auth responses or wire-exact egress. Accepted bytes are the committed prefix
of the current attempt, not proof of verification; fallback may reset them.
Work/wait durations sum across jobs and can exceed wall time. Full-stream receipt
uses no scratch, even when a scratch root was supplied for future range use.

Observers run only on the invoking thread. Strategy precedes payload;
completion follows verification and owned-file cleanup. Failure/cancellation
and the single final summary carry available counters. An artifact operation
shares one coordinator across nested blobs and emits its summary after artifact
publication/cleanup. Pooled C>1 pins/discovers all documents and reserves the
complete graph before overlapping independent blobs/ranges under that same cap;
C=1 retains one-at-a-time graph acquisition. Final local verification precedes
publication without replacing discovery provenance/reuse counts. The original raised
error remains authoritative. A failed observer is never invoked again, and
its exact exception propagates after draining, including `OSError` or
`KeyboardInterrupt`. Passing the same callable to multiple observer parameters
does not permit another call through a different adapter after it fails.
Separate references to the same bound instance method share failure state;
different receivers or different methods remain independent.

## Delivery boundaries and evidence

The separately reviewed components have distinct ownership:

| Delivery | Responsibility |
| --- | --- |
| [Core #135](https://github.com/toddysm/regshape/issues/135) | Internal coordinator, workers, bounded observations, caller-thread delivery and shutdown |
| [Public API #144](https://github.com/toddysm/regshape/issues/144) | `get_blobs`, typed status/statistics and shared invocation cap |
| [Functional renderer #145](https://github.com/toddysm/regshape/issues/145) | Interleaved identities/attempts, bounded visible rows and deferred CLI ETag warnings |
| [Visual formatting #141](https://github.com/toddysm/regshape/issues/141) | Colors, aligned columns, 20-cell bars and width/encoding fallbacks, without changing lifecycle or stdout |

Transport [#134](https://github.com/toddysm/regshape/issues/134), ranges
[#136](https://github.com/toddysm/regshape/issues/136) and artifact graph
integration [#137](https://github.com/toddysm/regshape/issues/137) are also merged.
These are library capabilities exposed by the CLI, not CLI-only schedulers.
Existing callbacks remain synchronous on the invoking thread.

[Validation #138](https://github.com/toddysm/regshape/issues/138) is merged;
the [evidence runbook](download-validation.md) distinguishes actual-registry
checks from controlled Linux benchmark acceptance and the retained failed
macOS ranged-performance gate. No universal speedup, registry certification,
release or parent-feature close-out is implied. Final documentation is tracked
by [#139](https://github.com/toddysm/regshape/issues/139); the
[capability inventory](../project-state.md) is reconciled separately before
closing [#131](https://github.com/toddysm/regshape/issues/131).

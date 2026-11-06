# Parallel artifact and ranged blob downloads: requirements and engineering design

- **Status:** Draft revision 1; awaiting owner approval. No runtime implementation.
- **Feature:** [#131](https://github.com/toddysm/regshape/issues/131).
- **Design item:** [#132](https://github.com/toddysm/regshape/issues/132).
- **Target:** [0.2](https://github.com/toddysm/regshape/milestone/1), not a release commitment.
- **Baseline:** `c128569112710ebe5c91fc990ba6e0b97c0198dd`, inspected 2026-11-06.
- **Deferred upload follow-up:** [#133](https://github.com/toddysm/regshape/issues/133).
- **Approval boundary:** approval of this document/breakdown and authorization to
  implement are separate. Do not merge this design or start implementation implicitly.

Open Container Initiative (OCI), command-line interface (CLI), application
programming interface (API), product requirements document (PRD), engineering
design document (EDD), and continuous integration (CI) are abbreviated below.
All new APIs, defaults, examples and estimates below are **proposals for review**,
not descriptions of capabilities already present.

## Part I: product requirements

### 1. Executive overview

Let registry operators and Python callers download independent artifact blobs
concurrently and split a large blob into concurrent HTTP byte-range requests.
Support both `artifact pull` and `blob get` through shared library machinery,
without changing blob contents, manifest ordering or verification guarantees.
Keep existing sequential defaults and expose parallelism explicitly for 0.2.

### 2. Problem and evidence

The owner's requests and subsequent OCI-compliance clarification are preserved in
[#131](https://github.com/toddysm/regshape/issues/131). Source inspection confirms
that [artifact acquisition](../../src/regshape/libs/artifacts/operations.py)
calls the [graph verifier](../../src/regshape/libs/artifacts/graph.py)
synchronously for each object. The existing
[blob receiver](../../src/regshape/libs/blobs/operations.py) streams buffers
but does not issue range requests; its bounded path explicitly rejects partial
responses. This is a scheduling/protocol gap, not an absence of streaming.

> NOTE: Evidence is the requesting maintainer and inspected source. No additional
> customers, production latency baseline, adoption figures or speedup measurements
> have been established. Do not turn proposed benchmarks into measured claims.

### 3. Customer scenarios

- **S1 - Complete artifact.** A registry operator preparing an offline copy
  currently waits for each layer before the next starts. They opt into four
  concurrent requests, pull a multi-platform artifact to a new directory or
  archive, and receive the same complete verified graph. Shared layers transfer
  once; completion order does not rewrite a manifest or select a platform.
- **S2 - One large blob.** A Python caller or operator needs one large
  digest-addressed blob. They enable two ranges per blob within four total
  requests. Parts arrive out of order, but the result has exactly the original
  bytes and digest. Omitting `--output` still verifies without retaining content.
- **S3 - Limited registry.** An operator enables ranges against a registry or
  storage redirect that returns full bodies, omits validators, or lacks usable
  size information. They see a safe reason for full-stream fallback, not an
  assertion that every such registry violates OCI. Independent artifact blobs
  can still download concurrently.
- **S4 - Failure or interruption.** A corrupt part, exhausted disk, observer
  exception or Ctrl-C stops the transfer. Workers relinquish their owned files
  and responses before cleanup. No incomplete artifact is published, and no
  blob verification success is printed. The operator fixes the cause and retries;
  persistent resume is not promised.

### 4. Outcomes, priorities and scope

| ID | Must-have requirement |
| --- | --- |
| R1 | Concurrent independent config/layer blobs for complete artifacts, with deduplication and unchanged graph/layout/archive semantics. |
| R2 | Concurrent ranged downloads of a single blob, shared by artifact pull and blob get, with full size/digest verification. |
| R3 | OCI/HTTP-compliant negotiation, response validation, consistency and explicit full-stream fallback. |
| R4 | One operation-wide concurrency bound; bounded queued work, memory and temporary range storage. |
| R5 | Preserve sequential defaults, streaming-buffer meaning, library returns, CLI output, verify-only blob get and credential behavior. |
| R6 | Caller-thread observers/diagnostics, truthful progress, cancellation and ownership-safe cleanup. |
| R7 | Requirement-mapped tests and comparative performance evidence before release. |
| R8 | User-facing documentation for both commands, resource costs and recovery. |

Outcome: improve attainable throughput where independent requests help, while
retaining integrity and interoperability. Correctness is mandatory; a universal
speedup is not. Network, storage, throttling and hashing can limit improvement.

Uploads are separate, deferred work in
[#133](https://github.com/toddysm/regshape/issues/133); chunked upload already
exists and must not be reimplemented here. This design does not add persistent
resume, upload parallelism, manifest-range downloads, platform selection,
referrer discovery, remote telemetry collection or config/layer selection.
The latter remains [#112](https://github.com/toddysm/regshape/issues/112).

### 5. Existing implementation and reuse

Extend the synchronous Requests transport rather than adding an asynchronous
HTTP stack. Reuse blob hashing, bounded entity reads, response cleanup, artifact
storage and publication, progress models and CLI display. Refactor the shared
graph traversal carefully; do not run the existing mutable verifier in threads.
Existing chunked uploads remain unchanged.

### 6. External alternatives

An external client is not introduced: delegating downloads would bypass
RegShape's transport diagnostics, credential handling and observer contracts.
No unverified competitor-performance comparison is asserted.

### 7. Desired experience

Both CLI commands add the same three options described in PM-2. Python callers
pass one optional immutable download policy. Omitted policy retains current
behavior. To enable object-only parallelism use `--concurrency 4`; to enable
range parallelism too, add `--ranges-per-blob 2`.

`blob get --chunk-size` continues to mean **streaming I/O buffer bytes**, not
HTTP range size. `--range-size` is separate. Artifact authentication, complete
graph selection, format and limits are unchanged. Blob get keeps its existing
repository/digest selector and JSON result.

GUI, hosted agent and Bicep/Terraform/Helm surfaces are N/A: this is a local
library/CLI change, not a deployed service or resource-provisioning API.

### 7A. Proposed user-facing output

The following is illustrative design output, not captured runtime evidence.
The artifact root is a mock value. The blob example uses the real SHA-256 of
32 MiB of ASCII `a` bytes; existing successful result schemas remain unchanged.
The artifact example assumes an existing trusted private working directory,
`/home/operator/artifacts`.

```text
$ regshape artifact pull --artifact registry.example/team/app:v1 \
    --output ./app-layout --concurrency 4 --ranges-per-blob 2
Downloaded complete artifact.
Source: registry.example/team/app:v1
Resolved: registry.example/team/app@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
Output: /home/operator/artifacts/app-layout
Format: oci
Objects: 0 indexes, 1 manifests, 4 blobs
Shared references reused: 0
```

```text
$ regshape blob get --repo registry.example/team/app \
    --digest sha256:facb58ac139bf9fc0e1f8b1f147003236b1b69e84f3a4c94166fa66f18f89932 \
    --output ./layer.bin --concurrency 4 --ranges-per-blob 2 --range-size 8388608
```

Proposed stderr when the registry omits a usable validator:

```text
Range download unavailable (no strong validator); using a full-stream download.
```

Stdout after successful full-stream verification:

```json
{
  "digest": "sha256:facb58ac139bf9fc0e1f8b1f147003236b1b69e84f3a4c94166fa66f18f89932",
  "content_type": "application/octet-stream",
  "size": 33554432
}
```

```text
$ regshape blob get ... --concurrency 1 --ranges-per-blob 2
Error: --ranges-per-blob must not exceed --concurrency.
```

The fallback diagnostic is literal proposed text, independent of progress being
enabled. Artifact `--json` and both commands' stdout remain free of diagnostics.
Protocol/integrity errors exit nonzero rather than printing a fallback success.

### 8. Success metrics

Before delivery, deterministic local fixtures must demonstrate two overlapping
blob requests for S1 and two overlapping ranges for S2, with peak active
requests at or below the selected limit. Every successful fixture must reproduce
the exact size, digest and artifact graph; zero invalid publications are allowed.

During implementation, compare sequential, object-parallel, range-parallel and
combined modes on many-small-blob and single-large-blob workloads. Record request
count, received bytes, elapsed time, scratch high-water mark and peak active
requests. Report p50/p95 over at least 20 measured runs after warm-up, with
payload sizes, machine, latency and bandwidth conditions. Before 0.2 release,
the maintainer reviews improvement and regression results; no arbitrary
production speedup threshold or service availability promise is invented.

### 9. Pricing

N/A: open-source local tooling; no billing or product-pricing change. Extra GETs,
egress, CPU and scratch I/O remain costs to the caller/registry.

### 10. Performance and scale

Proposed client limits are C = 1..32 concurrent request jobs and W = 1..C ranges
per blob. A job retains its slot through response close and range-spool disposal.
Range size R is 64 KiB..64 MiB, default 8 MiB. Range mode is eligible when W > 1
and known blob size N >= 4R. Zero-byte and smaller blobs use ordinary GET.

At most C jobs/futures and C range spool files are live. Additional range scratch
is at most C * R (maximum 2 GiB at the allowed maxima), independently of
artifact output staging. Existing artifact object/edge/depth/content/archive
limits remain mandatory; scratch does not count as unique artifact content.
Budget checks precede writes, and ENOSPC remains an explicit failure.
No per-range buffers of R bytes are held in memory: network and assembly reads
use at most 64 KiB each, further bounded by blob get's existing `--chunk-size`.
Metadata is O(V + E) under existing graph limits; queue data is bounded in D6.
Byte records and I/O buffers together use at most `(5C + 1) * 64 KiB`,
excluding bounded protocol metadata and the pre-existing graph/document budget.

### 11. Observability and troubleshooting

Retain object-level progress; chunks do not become new public progress rows.
Emit a safe per-blob fallback reason on stderr/library logging without URLs,
credentials or payloads. Counters separate logical accepted bytes from actual
network bytes including probes/retries. Existing opt-in request and timing
telemetry remains the source of HTTP detail.

Missing range support/validators: use the documented full-stream path. Repeated
429s: lower C and use configured retries. Corrupt parts/digests: fail and inspect
the registry/proxy, never bypass verification. ENOSPC: free space or lower C/R
and retry. Incomplete standalone blob output: remove/retry deliberately; artifact
output is only visible after the existing verified publication boundary.

### 12. Security

Use the existing stored credentials and repository pull permissions. Preserve
origin-bound challenge handling and authorization stripping on storage redirects.
TLS remains the default; `--insecure` stays explicit. No new identity, permission
or remote collector is introduced. Validate sizes, integers, ranges and all
resource bounds before allocation/writes. Treat payloads as opaque bytes:
stored gzip/zstd layers are not decompressed for range assembly.

### 13. Risks

Primary risks are request amplification/throttling, optional range metadata,
mutable transport/telemetry state, worker cleanup races and temporary disk use.
Mitigations and component coverage are specified in D and E; reducing C/W to
one is the operational escape hatch.

### 14. Rollout and milestones

Design PR and this breakdown require owner approval first. Implementation lands
in dependency order under PM-6 with unit/integration tests. Maintainer preview
uses opt-in settings; 0.2 readiness requires required checks, docs, measured
benchmarks and explicit release approval. Defaults stay sequential in 0.2;
automatic parallel defaults would be a separately approved behavior change.
There is no cloud-region deployment, private-preview program or inferred ship
date. No version bump, Git tag, release publication or merge is authorized here.

## Part II: engineering design

### A. Abstract

This design extends RegShape's synchronous download path with bounded object
parallelism and optional HTTP byte-range parallelism. A coordinator discovers
and validates the artifact graph, reserves unique content budgets, and schedules
blob request jobs through one shared worker pool. The same range receiver serves
standalone blob get, including verification without a saved file. Workers own
isolated transport state and private range spool files; only the invoking thread
updates observers, hashes ordered bytes, writes destination streams and mutates
artifact storage. HTTP range support is optional under OCI, so capability
limitations explicitly select a full-stream strategy. Invalid protocol responses
or corrupt content fail rather than being hidden as capability fallback. A
strong HTTP validator binds parts before their bytes are combined, and the final
blob is always verified against its requested content digest. Sequential defaults
and public result schemas remain unchanged. Artifact publication continues to
require the complete verified graph and its existing filesystem trust boundary.
The proposal adds no persistent resume state, upload behavior, new dependency,
remote telemetry backend or asynchronous HTTP implementation.

### B. Prerequisites and authority

Part I and [#131](https://github.com/toddysm/regshape/issues/131) capture the
requested requirements. This revision proposes concrete policy and interfaces
for review; it is not already approved. Full-artifact delivery
[#91](https://github.com/toddysm/regshape/issues/91) is implemented.
The [existing artifact design](../cli/artifact-pull.md) remains authoritative
for graph/storage/platform/publication invariants not changed explicitly here.

### C. Introduction

The current synchronous acquire callback serializes every object, and the
bounded blob receiver refuses HTTP 206. Changing only executor placement or
the existing `chunk_size` parameter would not implement safe ranged downloads:
graph reservations, worker ownership, part validation and observers must change
together. The next-release request is the justification for this additive,
opt-in design.

### D. Design

#### D1. Standards contract

| Source | Normative distinction | RegShape policy |
| --- | --- | --- |
| OCI Distribution v1.1.1, pulling blobs | Ordinary full GET succeeds with 200; missing content is 404. Range support is SHOULD, not MUST. | Ordinary full-transfer receiver requires 200. Capability limitations select full GET explicitly. |
| OCI Distribution v1.1.1, HEAD | Existing content returns 200; Content-Length is SHOULD. | Missing size is not a conformance failure; probe or use full GET. Unsupported HEAD can proceed to a GET probe. |
| OCI Distribution v1.1.1, digests | Digest verification is SHOULD. Docker-Content-Digest is recommended; using it requires verification. | Always verify the requested full digest. Do not require this optional header or mistake a full-object digest header for a chunk hash. |
| RFC 9110 sections 14 and 15.3.7 | Byte offsets are zero-based/inclusive; Accept-Ranges is advisory; 206 parts carry Content-Range. Invalid Content-Range must not be recombined. | Validate each part and count before accepting it; never put a 200 full body in a range slot. |
| RFC 9110 section 15.3.7.3 | Partial responses can be combined when they share the same strong validator. | Require the same strong ETag for every part. Strong Last-Modified validation is not implemented in this first version; otherwise use full GET. |
| OCI Image Spec v1.1.1, descriptors/manifest | Descriptors identify raw size/digest; layer arrays retain stack order. | Verify size/digest, preserve bytes and ordering, but allow transfer completion out of order. |
| OCI Distribution, chunked uploads | Upload session chunks are sent in order using POST/PATCH/PUT. | Not a restriction on download scheduling; no upload code changes. |

These policies are sometimes stricter than the protocols: a missing strong ETag
or usable numeric size disables optimization, not ordinary downloading.
Concurrency values, thresholds, queue bounds and retry policy are not OCI mandates.

#### D2. Component boundary and flow

Audience: maintainers deciding how both command paths share one scheduler.
The diagram shows the **proposed revision-1 runtime**, not the current runtime.
Arrows describe work/data flow, not source imports.

```text
artifact pull / pull_artifact                  blob get / get_blob
            | source + limits                      | digest + policy
            v                                      |
  coordinator: discover manifests ----------------+
  validate DAG; reserve unique blobs                |
            | bounded jobs                         v
            +--------------------------> shared download coordinator
                                         | C request/spool slots
                                         v
                              worker-owned RegistryClient clones
                                         | HEAD/probe/GET
                                         v
                                registry -> storage redirect
                                         |
                              bounded private range spool files
                                         | immutable events/results
                                         v
                         caller thread: ordered assembly + hash
                                         |
                   +---------------------+-------------------+
                   v                                         v
          artifact private store                    existing blob output
          final graph verification                  or verify-only discard
          directory/archive publication             verified BlobInfo
```

Reuse [blob operations](../../src/regshape/libs/blobs/operations.py),
[transport](../../src/regshape/libs/transport/client.py),
[artifact operations](../../src/regshape/libs/artifacts/operations.py),
[graph verification](../../src/regshape/libs/artifacts/graph.py),
[safe storage](../../src/regshape/libs/artifacts/storage.py),
[progress](../../src/regshape/libs/progress.py) and
[telemetry](../../src/regshape/libs/decorators/).
Proposed new files are `libs/blobs/download.py` for shared download policy,
scheduling and range receipt, and `libs/artifacts/download.py` for graph planning.
Keep these private mechanisms separate from existing public exports.
Expose the policy through the existing blob/artifact package exports.

#### D3. Artifact graph planning

For a nondefault parallel policy, separate discovery from blob receipt:

1. Preflight inputs, policy, output and client-fork support before effects.
   Fetch/pin the selected root once and retain original bytes.
2. Discover documents serially with the current strict JSON/media-type/digest
   checks. Extract a shared iterative traversal core used by planning and
   existing local verification, not a second inconsistent graph parser.
3. Reserve every distinct descriptor's size before scheduling its blob.
   Preserve max_objects, max_edges, max_depth including reused subtree heights,
   max_manifest_bytes and max_total_bytes. Reject cycles and conflicting
   repeated size/type/document roles before blob requests. Validate embedded
   content, including repeated descriptor data, without network acquisition.
4. Keep an internal unverified plan, not a `VerifiedGraph`. It records every
   edge, first role, source, postorder and unique reservation. Documents and
   embedded content already have their own bounded, verified stored bytes.
5. Schedule network blobs lazily, at most C jobs at a time; do not submit one
   future per graph object. Reuse one digest-to-state record for repeated edges.
6. Only the coordinator registers/promotes completed blob files. Run the final
   local verifier over the full store before wrapper generation, archive packing
   or publication. Preserve network/embedded provenance and reuse counts from
   the plan; do not replace them with the final verifier's all-local provenance.

Sequential policy continues through the current path. Shared traversal changes
must preserve local snapshots, recursive push and archive validation as well.
Existing Linux/macOS, trusted-private-namespace and native no-replace
publication requirements are not relaxed; Windows artifact pulls still fail closed.

#### D4. Range negotiation and receipt

When W = 1, use ordinary full GET. Otherwise obtain a size hint from a trusted
descriptor or optional HEAD. Invalid/contradictory size metadata fails; missing
metadata can be recovered by a probe. HEAD 405/501 is a capability limitation,
not a blanket reason to ignore auth errors or 404.

If a known N is zero or below 4R, use full GET without a probe. Otherwise issue
`Range: bytes=0-0` with `Accept-Encoding: identity` through the same registry
transport. This request owns a normal global slot until closed. Validate the
probe's one byte, numeric complete length and strong ETag. Close/discard probe
bytes; they count as network traffic, not accepted blob progress.

- Probe 206 with unknown complete length, missing/weak ETag or nonidentity HTTP
  coding: close it and use full GET with an explicit reason.
- Probe 200: close it before starting a full GET; ranges were ignored.
- Probe 416 with a validated size of zero: use full GET to verify the empty
  blob; 416 itself never establishes success. Other 416 responses fail.
- A discovered size below 4R uses full GET. Numeric size must match a supplied
  trusted descriptor/total size. Missing Accept-Ranges does not block probing.

For eligible content, request disjoint contiguous ranges
`[kR, min((k+1)R, N)-1]`, one range per GET, with `If-Range: <strong-etag>`
and identity encoding. Every 206 must have a parseable bytes Content-Range,
the same numeric N and strong ETag, a range within the request, and exactly the
declared number of body bytes. Pre-write checks enforce both part and blob caps.
Parse numeric metadata with bounded decimal length and a 0..2^63-1 client limit
before arithmetic; reject overflow explicitly rather than allocating from it.
Unknown units, impossible bounds, contradictory lengths, encoding/validator
changes, oversized/truncated bodies and unsolicited multipart responses fail.

A correctly declared strict subrange is permitted by HTTP. Rather than
mislabeling it as malformed, this version closes/discards the attempt and uses
full-stream fallback with a specific reason. A 200 after successful probing
also triggers a single full-stream fallback unless an observed validator change
already proves inconsistency, in which case it fails. Cancel/drain all old jobs
for that blob before resetting progress, releasing old spool state or writing
replacement bytes. Never mix partial/full attempts. Permit at most one strategy
fallback per blob; the full receiver does not recursively attempt ranges.

#### D5. Bounded scheduling, assembly and output ownership

Use one `ThreadPoolExecutor` for an invocation, not nested executors.
Every network transaction, including metadata/probes, holds one of C global
slots for its streamed-response lifetime. At most W slots belong to a blob.
Select ready blobs round-robin so one large blob cannot monopolize all work.

Each range worker streams to one exclusive private spool file, at most R bytes.
Artifact scratch stays inside its existing trusted SafeDirectory namespace.
Standalone blob scratch uses a private standard-library temporary directory in
the caller-trusted temporary root, with restrictive platform permissions and
identity-checked file cleanup. Neither promises hostile same-user/privileged
isolation. No worker-supplied path or registry string becomes a cleanup target.
Its slot and scratch reservation remain held until the coordinator reads/deletes
that spool. The per-blob scheduling window contains at most W consecutive
uncommitted ranges, anchored at the committed prefix. An out-of-order result
does not authorize an unbounded sequence of later requests.

Only the coordinator copies contiguous completed parts, updates the full hash
in original byte order, writes the destination sink and advances public bytes.
A successfully transferred but uncommitted part stays in the bounded window;
it is never hashed out of order. Whole blobs are not buffered in memory.
Full-stream workers use a bounded byte/event queue rather than spooling an
unbounded entire blob. Workers wait with cancellation-aware backpressure.

Output contracts deliberately differ where current behavior differs:

- Artifact/bounded `get_blob`: use the existing exclusive, identity-checked
  staging output and pre-write max_bytes checks. Promote only after full
  digest/size verification. Do not mutate SafeDirectory/ContentStore on workers.
- Standalone unbounded blob get with output: preserve existing overwrite and
  partial-file-on-failure semantics, with only the caller thread writing the
  output. Do not promise atomic replacement or delete a preexisting caller
  destination on failure. Strict artifact cleanup must not leak into this path.
- Blob get without output: hash ordered bytes and discard them. It needs only
  bounded range scratch, not a full-blob saved file.

Before abandoning any owned temporary state, stop new jobs, signal cancellation,
pump/discard pending records so producers cannot deadlock, close each worker's
response in that worker's finally block, and join all workers. Do not call
Future.cancel() and assume active I/O stopped. Requests read timeouts remain
finite; cancellation during a blocked read is bounded by that timeout, not
instantaneous. Cleanup failures are reported without masking the primary error.
Only identity-proven owned files/directories may be removed.

#### D6. Transport, callbacks and telemetry

Add `RegistryClient.fork_for_download()` to produce isolated worker clients
from a frozen snapshot of already resolved credentials/configuration. Do not
resolve Docker credentials once per range or copy shared mutable auth/cache
middleware. Each child owns its own auth state, middleware and last_response.
The original client is borrowed and never closed or mutated by workers.

The built-in implementation supports the exact base RegistryClient and built-in
middleware. A subclass or configured custom middleware must provide an explicit
safe fork override; otherwise reject parallel policy with ValueError **before
network/filesystem effects**. Never silently drop custom middleware or copy it
unsafely. Sequential/custom-client behavior remains available unchanged.
Worker clones preserve origins, TLS/insecure mode, timeout, auth behavior and
logging/cache policy; streamed GETs still bypass response caching. The caller's
last_response is not an authoritative list of parallel responses and is not
updated by workers. Document this opt-in contract.

Workers must not inherit the same mutable TelemetryConfig merely through
copy_context(). Give each task a fresh context/config with isolated metrics and
method timings. Queue immutable diagnostics and metric deltas to the coordinator,
which alone merges counters and writes actual terminal/log sinks. Preserve
enabled options; do not globally disable telemetry to make threading work.
Worker timing summaries must not clear the parent scenario's accumulators.
Aggregate repeated range method timings by the fixed internal method names;
do not append one retained timing record per range to the parent list.
Retain existing sanitization; new scheduler/fallback records contain no URLs,
headers, payloads or credentials.

The bounded coordinator queue holds at most 4C records. Payload records are at
most 64 KiB; range results reference spool files, not payload copies. Reject
oversized diagnostic records explicitly rather than buffering without limit.
Producers check cancellation while waiting to enqueue. The coordinator pumps
events while awaiting jobs; it must not block on a future while workers block
on its full queue.

All user progress callbacks execute on the invoking thread, one at a time,
through the current immutable public event models. Callback exceptions retain
their exact identity, permanently disable that observer, cancel work and
propagate after worker cleanup. Full-stream/range fallback starts a new logical
attempt with zero accepted bytes; range retries do not double-count accepted
bytes. Artifact adapters preserve attempt values and one logical row per digest.
Completed/stored events and stdout require verification and applicable output
close/publication; telemetry close errors cannot leave a success-shaped result.

### PM-1. Requirements traceability

| Requirement/scenario | Design and acceptance evidence |
| --- | --- |
| R1 / S1 | D3 planning/deduplication, D5 scheduler, final graph/publication tests. |
| R2 / S2 | D4 range protocol, D5 ordered hashing and verify-only sink. |
| R3 / S3 | D1 standards matrix, D4 capabilities/consistency, PM-5 errors. |
| R4 | Part I.10 limits, D3 reservations, D5 windows, D6 queue bounds. |
| R5 | PM-2 additive interfaces, D3 sequential path, D5 output contracts. |
| R6 / S4 | D5 drain/join/ownership, D6 observer/telemetry isolation. |
| R7 | Part I.8 measurements, PM-7 deterministic and hosted tests. |
| R8 | PM-6 final documentation item and O launch checklist. |

No separate remote service, upload change or persisted resume format is
introduced without a requirement. Optional layer selection is not silently added.

### PM-2. API and CLI contracts

Proposed frozen public `DownloadOptions`, exported by blob/artifact packages:

| Field | Type/default | Validation |
| --- | --- | --- |
| concurrency | int = 1 | 1..32, not bool |
| ranges_per_blob | int = 1 | 1..concurrency, not bool |
| range_size | int = 8388608 | 65536..67108864 bytes, not bool |

Add keyword-only `download_options: DownloadOptions | None = None` to existing
`get_blob` and `pull_artifact`; preserve all existing positional parameters,
return types, errors, max_bytes/output_dir_fd and progress parameters. None and
C=W=1 preserve the sequential path regardless of range_size. Invalid policy/fork support
raises ValueError before effects; existing digest validation retains BlobError.
No public scheduler/context parameter or global singleton is added. Artifact
orchestration uses an internal shared coordinator, not one executor per get_blob.

Both commands expose `--concurrency`, `--ranges-per-blob`, `--range-size` with
the table's defaults and constraints. Validate relationships before credential
lookup or filesystem writes. C > 1 with W = 1 enables object-only parallelism;
for a standalone single blob it remains one full-stream request. W > 1 enables
eligible ranges. Lower C/W to one for legacy behavior.

Keep the existing `BlobInfo`, `ArtifactPullResult`, TransferProgress and
ArtifactProgress wire fields and stdout schemas unchanged. Strategy diagnostics
use existing stderr/logging paths, not new result keys. The new fork method's
return is caller-owned for the download lifetime; responses always close even
though RegistryClient currently has no persistent Session close API.

No OCI endpoint/version, layout format, descriptor bytes, persisted schema or
cache-key version changes. New Python symbols/options are additive 0.2 APIs;
no REST api-version or event serialization schema is introduced.

### PM-3. Metrics, usage and cost

Measure request jobs, active-request high water, accepted versus received bytes,
fallback reason, retry count, receive/hash/overall duration and scratch high
water. Merge existing per-request metrics on the coordinator; do not call HTTP
header latency complete-transfer time. Tests/benchmarks can capture internal
high-water counters without adding public progress fields.

Map integrity to final verification failures/publications; boundedness to peak
requests/scratch; attainable throughput to benchmark bytes/elapsed; adoption to
the explicit options in user-supplied diagnostics. Use low-cardinality strategy
and reason values, not registry/repository/digest labels in aggregate metrics.

This is not a hosted service: there is no backend customer identity, collection
consent, adoption database or churn measurement. Do not add unsolicited remote
tracking to satisfy a service rubric. Maintainers may retain benchmark reports;
operators may persist existing opt-in local logs under their retention policy.
Longitudinal customer adoption data is an explicitly unestablished product metric.

Caller cost is original content bytes plus HEAD/probe/retry/fallback overhead,
extra HTTP requests and at most C*R range scratch beyond existing output.
Compare CPU/hash time, bytes and scratch high water in benchmark reports; registry
GET/egress costs use the operator's own provider rates, not an invented price.

### PM-4. Authentication, authorization and networking

Identity and permissions remain unchanged: stored registry credentials or
anonymous access, with repository pull scope. No push permission is needed.
Every job, auth exchange and storage redirect stays within its worker slot and
the existing transport security policy. A foreign 401 must not initiate registry
credential negotiation. Credentials are not attached indiscriminately to
storage redirects or recorded in new diagnostics.

No listening port, cloud resource, VNet/NSG/private endpoint or inbound path is
created. Existing caller-controlled outbound registry/storage/token-service
connectivity and TLS settings remain applicable.

### PM-5. Errors, resilience and boundaries

| Condition | Required behavior |
| --- | --- |
| Invalid options, unsupported client fork | Preflight ValueError / Click parameter error; no side effects. |
| Missing range support, usable size or strong ETag | Explicit one-time per-blob full-stream fallback under D4. |
| Unknown HTTP range total or valid shorter range | Capability-limited fallback, not a false protocol-corruption diagnosis. |
| Auth rejection, 403, missing blob 404 | Preserve AuthError/BlobError taxonomy/status; no capability fallback. |
| Malformed/contradictory Content-Range, changed ETag, unexpected coding, short/extra body | BlobError; cancel; do not publish or silently retry as full content. |
| 416 | BlobError, except validated zero-size probe selecting full GET; never mark successful from 416 alone. |
| 429/selected transient 5xx/transport errors | Retry only under the caller's enabled retry policy; otherwise explicit failure. |
| Byte/graph limit, disk error, unsafe/replaced path | Existing domain/filesystem error and identity-safe cleanup. |
| Callback/sink exception, Ctrl-C | Preserve original exception, cancel/drain/join; no more calls into a failed observer. |

Use one retry owner per request transaction. With retries disabled, do not add
hidden automatic body retries. With retries enabled, the coordinator snapshots
RetryConfig and manages at most `max_retries + 1` attempts per job; disable the
child's built-in RetryMiddleware for those jobs so the budget is not multiplied.
Retain configured retry statuses and backoff. Discard failed part spools before
retry; do not accept partial chunks. No retries for integrity/protocol failures
or arbitrary callback exceptions.

For retryable 429/503, honor a valid Retry-After by scheduling no sooner than
the greater of it and configured exponential backoff; absent headers use existing
backoff. A malformed Retry-After is an explicit protocol diagnostic with the
configured backoff still applied. Backoff is cancellation-aware and releases the
network slot after response close; delay metadata remains under the C-job bound.
GET/HEAD retries are read-only; there are no registry mutations or upload sessions.

Existing finite request/read timeouts apply. Slow-progress streams are not given
a new undocumented whole-operation deadline. A stale size, encoded response,
duplicate descriptor, zero length, missing Content-Length, retry/fallback attempt,
and completion in arbitrary order must each have an explicit test.

### E. Risk identification and assessment

| Risk category | Assessment / mitigation |
| --- | --- |
| API/CLI compatibility | Sequential defaults; additive policy; preserve output and buffer meaning (PM-2). |
| Security/resource exhaustion | Global/window/byte/graph bounds, strict range parsing, private identity-checked scratch (D3-D5). |
| Privacy | No new remote collection; safe new diagnostics and owned temporary retention (H). |
| Auth | Isolated clients; preserve origin/redirect checks; custom clients fail preflight unless forked safely (D6). |
| Dependency/infrastructure | No new package/service; optional registry capabilities fall back explicitly (D1/D4). |
| Deployment/rollback | Opt-in options, unchanged storage format and sequential escape hatch (K/N). |
| Contention/noisy neighbor | One C cap, round-robin blobs, bounded slots/backpressure and configurable retry policy (D5/PM-5). |
| Regression: blobs, transport, telemetry | Existing sequential tests plus fork/queue/encoding/observer regressions (M/PM-7). |
| Regression: graph, storage, archive, CLI | Shared walker equivalence, complete-layout/archive round-trip, JSON and native-platform tests (M/PM-7). |

### F. Breaking changes

No intended breaking default, positional API, result schema or persisted-format
change. Parallel mode documents its new worker-fork requirement and does not
silently ignore unsupported customization. Discovery-before-blob-transfer can
change progress ordering only in explicitly selected parallel mode. This does
not permit concurrent user callbacks or change standalone output overwrite
semantics. Shared code must retain regression coverage in all current CI jobs.

### G. Security risk management

Trust boundaries remain registry/token/storage services, untrusted descriptors
and bytes, caller-owned sinks, and trusted local private namespaces. Digest checks
do not replace HTTPS or HTTP validators. Never accept a server offset as a local
path. Reserve scratch before writes, avoid filename/digest injection, and do not
claim same-user/privileged isolation from file permissions. No payload execution,
layer extraction, new cryptography/key storage or changes to auth login.

### H. Privacy and data sovereignty

Artifact bytes remain on operator-selected storage and the private temporary
location. Scratch is removed after workers stop; failures to remove owned
content are reported. Do not claim secure erasure on SSDs or deletion of
unproven replacement paths. Registry egress/redirect destinations remain
operator/registry controlled, not newly region-pinned by the client.
No new personal/customer identifiers or external diagnostic service is added.

### I. Observability risk management

D6 owns serial emission and PM-3 defines measurements. A missing fallback reason,
count above total, duplicate completion, exceeding C or unbounded scratch is a
test failure, not a dashboard-only concern. Existing local diagnostics support
operator troubleshooting; no availability SLO or managed alerting service exists.
Benchmarks and failure-injection tests are the maintainer's health evidence.

### J. Dependencies

No new third-party/runtime service dependency. Use standard-library futures,
queues, contexts, temporary-file/handle utilities, existing Requests and current
Python >=3.10 support. Optional registry ranges/strong ETags are capability
dependencies with full-stream fallback, not new minimum registry requirements.

### K. Deployment and buildout

Land reviewed components and tests in PM-6 dependency order, then final docs.
Run the existing PR workflow and keep required checks/reviews as merge gates.
The existing package publishing automation is reused only after separate release
authorization. No region rollout, config backfill, registry mutation, database
migration or manual cloud buildout is needed. This design PR changes docs only.

### L. Multi-tenancy and contention

No multi-tenant backend is introduced. Registry contention still matters:
apply one per-operation C cap across all blobs, authentication and redirects,
and reduce concurrency on operator instruction. Separate process invocations
are not globally coordinated; document that C is not a machine-wide quota.

### M. Regression assessment

- Blob operations/CLI: sequential paths, overwrite/error behavior, verify-only,
  digest algorithms, unknown totals, buffer validation and pipe-independent use.
- Transport/auth: legacy and middleware paths, foreign challenges, redirects,
  retries, streamed-cache bypass, custom-client rejection and credential lookup.
- Telemetry/progress: isolated contexts, coordinator thread identity, immutable
  events, sink/callback errors, attempt resets, all no-progress/JSON modes.
- Graph/storage/archive: deduplication, subjects/indexes, reused subtree depth,
  inline data, every limit, safe namespace ownership, archive equivalence,
  recursive push/snapshot validation and unsupported Windows artifact guards.

No half-enabled default rollout: old callers remain sequential even while the
implementation pieces are landing. No unrelated pre-existing telemetry rewrite
or legacy source-documentation sweep is authorized.

### N. Rollback

Set C=W=1 (or omit the new policy) for sequential behavior. A package rollback
leaves valid OCI layouts/blobs readable because no new persisted format exists.
Revert code through normal reviewed PRs; this design PR can be reverted as docs.
Downloaded bytes/remote registry content do not need a data migration. Ordinary
blob output overwrite remains an existing irreversible caller-selected action;
do not imply package rollback restores a previous output file.

### PM-6. Engineering phasing, estimates and work breakdown

Estimates are initial engineering judgments, not delivery commitments. A day is
one focused engineer-day. Test time is included separately; assumptions are reuse
of current helpers, no new dependency, and no cross-process resume. Registry
interop, custom-client forking and cleanup race coverage are the long poles.

| Item | Deliverable / affected components | Depends on | Implement / test / rollout days |
| --- | --- | --- | --- |
| Design #132 | This design and approval gate | None | 1 / 0.5 / 0 |
| W1 [#134](https://github.com/toddysm/regshape/issues/134) | Download policy and safe client forking | Approved design | 1 / 1 / 0.5 |
| W2 [#135](https://github.com/toddysm/regshape/issues/135) | Bounded coordinator, cancellation and caller-thread telemetry/progress bridge | W1 | 2 / 2 / 0.5 |
| W3 [#136](https://github.com/toddysm/regshape/issues/136) | Shared ranged blob engine, ordered assembly and standalone blob CLI | W1, W2 | 3 / 2 / 0.5 |
| W4 [#137](https://github.com/toddysm/regshape/issues/137) | Shared graph-planning refactor and parallel artifact integration/CLI | W3 | 3 / 2 / 0.5 |
| W5 [#138](https://github.com/toddysm/regshape/issues/138) | Cross-mode integration/fault tests, benchmarks and explicit CI matrix coverage | W3, W4 | 1 / 2 / 0.5 |
| W6 [#139](https://github.com/toddysm/regshape/issues/139) | Final user/API/CLI documentation and capability close-out | W1-W5 | 1 / 0.5 / 0.5 |

These are native sub-issues of the feature and all target milestone 0.2.
Each code item must include current file-purpose and applicable type/callable
contracts, including private code and test helpers, following the repository's
Sphinx-style Python convention. This obligation is scoped to new/changed
declarations, not legacy boilerplate. Exactly one final user-documentation item
is required and does not substitute for source contracts.

### PM-7. Test plan and CI

| Coverage | Required deterministic evidence |
| --- | --- |
| S1 / graph | Multiple blocked handlers prove overlapping distinct blobs; shared digest requested once; all graph limits reserve before work; repeated incompatible descriptors fail. |
| S2 / range | Multiple blocked ranges prove overlap; reverse completion; one-byte tails; exact-size/digest checks; verify-only hash equals file-output hash. |
| S3 / HTTP | 200-on-Range, 416/empty probe, absent/advisory Accept-Ranges, missing HEAD size, 405/501, missing/weak/changed ETag, valid shorter parts and unknown totals. |
| S4 / failures | Wrong bounds/unit/length/coding, multipart, truncation/overrun, hash mismatch, 401/403/404/429/5xx, retries and Retry-After; no invalid success/publication. |
| Concurrency/resources | Peak requests <= C including auth/probes/redirects; per-blob <= W; pending futures/spools/queue under bounds; stalled earliest range cannot grow scratch without limit. |
| Ownership | Observer throws with active writes, full queue then cancellation, disk-full/short-write/close failures, replaced paths; join before cleanup; preexisting blob output is not deleted. |
| Compatibility | Default-policy equivalence, both CLI option relations, exact result fields, stdout cleanliness, no-progress/JSON, source contracts and custom-client fork errors before effects. |
| Round-trip/platform | Multi-platform/subject/shared/inline directory and gzip-tar layouts validate and push; Linux/macOS storage tests remain valid; Windows artifacts still fail closed and standalone blob tests remain portable. |

Add focused pytest modules for policy/coordinator, range download and graph
planning alongside existing blob/artifact/transport/progress tests. Prefer local
HTTP handlers and barriers/events over sleep-based timing assertions. Add a
bounded stress test with many references and slow consumers; do not require a
public registry or credentials in unit CI.

The [existing Tests workflow](../../.github/workflows/tests.yml) runs pytest
on pull requests with Python 3.10/3.11/3.12. Update its explicit legacy
Requests/urllib3, minimum-Click and macOS/Windows selectors for the relevant new
tests; do not rely only on the main automatic collection for compatibility jobs.
No new workflow is needed. Hosted checks/reviews are authorized for this design
PR; implementation PRs still need their own required validation/review gates.
Benchmark wall-clock results are recorded for review, not flaky CI timing gates.

### O. Next actions and launch checklist

- [ ] Owner approves this revision, proposed defaults and linked breakdown.
- [ ] Separately authorize the first implementation item and its test mode.
- [ ] Complete dependency-ordered delivery with required checks/reviews.
- [ ] Review measured performance/resource results without a universal speedup claim.
- [ ] Deliver W6 docs for both commands, scratch cost, fallback and troubleshooting.
- [ ] Reconcile the capability inventory only after implementation/docs merge.
- [ ] Obtain separate release authorization; no external partner/legal/cloud
  buildout is identified for this local open-source enhancement.

### P. References

- [OCI Distribution v1.1.1: pulling blobs](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#pulling-blobs)
- [OCI Distribution v1.1.1: existence checks](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#checking-if-content-exists-in-the-registry)
- [OCI Distribution v1.1.1: chunked uploads](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#pushing-a-blob-in-chunks)
- [OCI Image Spec v1.1.1: descriptors](https://github.com/opencontainers/image-spec/blob/v1.1.1/descriptor.md)
- [OCI Image Spec v1.1.1: manifest ordering](https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md)
- [RFC 9110: ranges](https://www.rfc-editor.org/rfc/rfc9110.html#section-14)
- [RFC 9110: partial responses and recombination](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.7)
- [Current artifact workflow](../../docs/guides/artifact-pull.md)
- [Current blob API](blobs.md) and [blob CLI](../cli/blob.md)

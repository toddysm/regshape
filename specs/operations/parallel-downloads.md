# Parallel artifact and ranged blob downloads: requirements and engineering design

- **Status:** Draft revision 2; awaiting owner approval. No runtime implementation.
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

Revision 2 addresses the selected feedback on
[design PR #140](https://github.com/toddysm/regshape/pull/140). It proposes
default object parallelism, an explicit sequential escape hatch, public batch
and diagnostic APIs, shared scoped authentication/connection reuse, renderer
ownership, and corrected retry/cleanup/cancellation contracts. These material
changes require renewed review; neither revision has implementation approval.

## Part I: product requirements

### 1. Executive overview

Let registry operators and Python callers download independent artifact blobs
concurrently and split a large blob into concurrent HTTP byte-range requests.
Support both `artifact pull` and `blob get` through shared library machinery,
without changing blob contents, manifest ordering or verification guarantees.
Default to four concurrent requests for independent blobs in 0.2, with
`--sequential` and a library equivalent to opt out. Splitting one blob into
ranges remains explicit: ranges-per-blob defaults to one.

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

These role-based stories illustrate the owner's requested workflows; they are
not additional claimed customers or measured production incidents.

**S1 - Release operator copying a complete artifact.** Before preparing an
offline release, the operator waits for every layer in turn or writes a script
to coordinate separate downloads. They run the normal artifact pull command
without tuning flags. Four request slots allow independent config/layer blobs
to overlap, and stable progress rows show which objects are still active.
Shared layers transfer once. The operator receives the same verified
multi-platform graph as a directory or archive, without rewritten manifests.
On a constrained link they rerun with `--sequential`, rather than abandoning
the complete-artifact workflow.

**S2 - Incident responder retrieving one large blob.** The responder already
knows the digest of a large diagnostic artifact, but today's blob get uses one
connection regardless of its streaming buffer size. They explicitly choose
`--ranges-per-blob 2`, optionally select scratch storage, and watch one blob row
instead of a row per part. Out-of-order parts reconstruct the exact requested
bytes. They can omit output to verify content without retaining it; a verified
result, not a full progress bar, tells them the operation succeeded.

**S3 - Compatibility operator using a limited registry.** The operator tries
ranges through a registry/proxy whose capabilities are not documented. Today
they must inspect HTTP responses manually to distinguish a limitation from
corruption. The new downloader identifies missing validators or ignored ranges,
uses full streaming, and reports a bounded reason summary. The artifact still
benefits from independent-blob parallelism. The operator does not have to
misclassify optional metadata as an OCI violation or scrape thousands of notices.

**S4 - Automation operator handling a failed copy.** An unattended copy can
currently leave partial standalone output, and a byte count alone does not say
whether content verified. A corrupt response, full disk or interruption now
produces the explicit failure behavior in 7A and D5, never successful JSON or
an incomplete published artifact. The operator frees space or investigates
corruption and retries deliberately. Digest-mismatched standalone output is
removed as today; other failures may retain a partial file. Ctrl-C requests
cancellation, but a trickling peer can delay worker shutdown; there is no
invented cancellation deadline or persistent resume promise.

**S5 - SDK integration author exporting several blobs.** The author currently
loops over `get_blob` serially or invents threads, authentication sharing and
an aggregate connection bound. They instead call public `get_blobs` with
`BlobDownload` requests and one `DownloadOptions`. One internal scheduler
enforces the batch-wide cap; results retain input order. Caller-thread progress
and structured status callbacks let their application correlate stable transfer
IDs, collect strategy/fallback/cost counters, and cancel by raising its own
exception without parsing logs. The same options work with direct `get_blob`
and `pull_artifact` calls, as illustrated in 7B.

**S6 - Operator encountering registry throttling.** The operator's copy begins
with four slots and the registry returns 429. Today's CLI neither enables
retries nor exposes a retry option, so advice to "configure retries" is not
actionable. They now see a safe throttling diagnostic, enable
`--download-retries 3`, and reduce `--concurrency` to two or use `--sequential`.
Retry-After controls when another request may be sent. Exhaustion still exits
nonzero with no verified result; status counters distinguish retries from
accepted bytes. This is bounded operator-controlled recovery, not automatic
adaptive concurrency or an unbounded retry loop.

### 4. Outcomes, priorities and scope

| ID | Release requirement |
| --- | --- |
| R1 | Concurrent independent config/layer blobs for complete artifacts, with deduplication and unchanged graph/layout/archive semantics. |
| R2 | Concurrent ranged downloads of a single blob, shared by artifact pull and blob get, with full size/digest verification. |
| R3 | OCI/HTTP-compliant negotiation, response validation, consistency and explicit full-stream fallback. |
| R4 | One operation-wide concurrency bound; bounded queued work, memory and temporary range storage. |
| R5 | Default C=4/W=1, explicit sequential opt-out, unchanged buffer meaning/results/output/verify-only behavior, and shared scoped credentials without per-range renegotiation. |
| R6 | Public batch/diagnostic SDK access, caller-thread observers, interleaved stable CLI rows, truthful progress, cancellation and ownership-safe cleanup. |
| R7 | Requirement-mapped tests and comparative performance evidence before release. |
| R8 | User-facing documentation for both commands, resource costs and recovery. |

Outcome: improve attainable throughput where independent requests help, while
retaining integrity and interoperability. Correctness is mandatory; a universal
speedup is not. Network, storage, throttling and hashing can limit improvement.

R1 and R2 are co-required for the currently proposed 0.2 feature scope, not an
implicit promise to release R1 alone if R2 slips. R3-R8 are safety/delivery gates,
not optional cleanup. Implement R1 and R2 on the same foundation without forcing
artifact integration to wait for the range-specific code. An R1-only release
would require an explicit owner replan of scope, milestone and acceptance gates;
this document does not silently authorize it.

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

Both CLI commands expose the controls in PM-2. Omitted download policy means
C=4/W=1/R=8 MiB: artifact objects overlap by default, while a standalone
single-blob get still uses one full stream. Add `--ranges-per-blob 2` for ranges.
Use `--sequential` to force C=W=1; combining it with an explicitly supplied
`--concurrency` or `--ranges-per-blob` is a parameter error, even if the value is
one, rather than silently ignoring an argument. Range-size validation still
applies in sequential mode, but no ranges are issued.

SDK callers use `DownloadOptions.sequential()` for the same opt-out. Public
`get_blobs` exposes batch-wide scheduling; `download_status_callback` provides
typed strategy/fallback/statistics events on all three download operations.

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
    --output ./app-layout
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

The single-blob fallback diagnostic above is literal proposed text. For an
artifact, print at most one notice per fixed fallback reason, not one per blob:

```text
Range download unavailable (no strong validator); using full-stream downloads (further notices for this reason suppressed).
Range fallback summary: 12 blobs used full-stream downloads (no strong validator: 12).
```

The summary appears after transfer/cleanup, including failure where available;
it is never a success indicator. Structured status events remain per blob even
when CLI notices are coalesced. Diagnostics still appear with `--no-progress`
and artifact `--json`, on stderr only, without modifying result JSON.

Proposed S4/S6 failure examples use the artifact reference above. Exact OS error
wording may vary; statuses and channel/exception contracts do not:

| Failure | Stderr / SDK outcome | Exit / output |
| --- | --- | --- |
| Corrupt part/digest | `Error [registry.example/team/app:v1]: Blob digest verification failed` | CLI 1; no successful stdout/publication. SDK BlobError. |
| Disk full | `Error [registry.example/team/app:v1]: [Errno 28] No space left on device` | CLI 1; no successful stdout/publication. SDK preserves applicable OSError/BlobError. |
| Observer raises RuntimeError("observer failed") | SDK re-raises that exact exception object. A CLI renderer/status-sink bug remains an unexpected exception, with a traceback ending `RuntimeError: observer failed`, not a fabricated transfer error. | CLI 1 for that unexpected RuntimeError; no completion/result; do not invoke the failed sink again. |
| Ctrl-C | Click prints `Aborted!` once shutdown returns; SDK propagates KeyboardInterrupt after worker cleanup. | CLI 1, not a promised 130; shutdown may wait for a trickling read. No successful stdout. |
| Retry budget exhausted on 429 | `Error [registry.example/team/app:v1]: Download throttled (HTTP 429); lower --concurrency or increase --download-retries.` | CLI 1; SDK BlobError with status 429; no verified result. |

### 7B. Proposed Python experience

These are proposed 0.2 calls, not code executable against the inspected baseline.
The application resolves the client once and owns its output directories:

```python
from regshape.libs.artifacts import pull_artifact
from regshape.libs.blobs import BlobDownload, DownloadOptions, get_blob, get_blobs
from regshape.libs.transport import RegistryClient, TransportConfig

client = RegistryClient(TransportConfig(registry="registry.example"))
options = DownloadOptions(concurrency=4, ranges_per_blob=2, max_retries=3)
digest = "sha256:facb58ac139bf9fc0e1f8b1f147003236b1b69e84f3a4c94166fa66f18f89932"
other_digest = "sha256:e75f883f87d4a8c873d69e3823383a901b00a2dcff331e267c61134135c381ee"
statuses = []

layout = pull_artifact(
    client, "team/app", "v1", "./new-layout",
    download_options=options, download_status_callback=statuses.append,
)
blob = get_blob(
    client, "team/app", digest, "./layer.bin",
    download_options=options, download_status_callback=statuses.append,
)
batch = get_blobs(
    client, "team/app",
    [BlobDownload(digest=digest, total_size=33554432),
     BlobDownload(digest=other_digest, total_size=33554432)],
    download_options=options, download_status_callback=statuses.append,
)
serial = get_blob(
    client, "team/app", digest,
    download_options=DownloadOptions.sequential(),
)
```

Both batch entries verify without saving. The second digest is 32 MiB of ASCII
`b` bytes. Add entries to the **same** get_blobs call; it owns one C cap, not C
per entry. Existing
singular return types stay unchanged; the new batch return and callback
contracts, including partial-failure semantics, are defined in PM-2.

### 8. Success metrics

Before delivery, deterministic local fixtures must demonstrate two overlapping
blob requests for S1 and two overlapping ranges for S2, with peak active
requests at or below the selected limit. Every successful fixture must reproduce
the exact size, digest and artifact graph; zero invalid publications are allowed.

During implementation, compare sequential, object-parallel, range-parallel and
combined modes on many-small-blob and single-large-blob workloads. Record request
count, received bytes, elapsed time, scratch high-water mark and peak active
requests. Report p50/p95 over at least 20 measured runs after warm-up, with
payload sizes, machine, latency and bandwidth conditions.

Proposed reproducible 0.2 release gates, not measured results or production SLOs:

| Workload / comparison | Controlled fixture | Pass gate |
| --- | --- | --- |
| S1 object parallel versus sequential | 32 distinct 1 MiB blobs, 25 ms simulated RTT, 32 MiB/s per connection, >=128 MiB/s aggregate; C=4/W=1 versus C=W=1 | Median elapsed speedup >=1.5x. |
| S2 range parallel versus full stream | One 128 MiB blob, same network shaping, strong ETag and ranges; C=4/W=2/R=8 MiB versus C=W=1 | Median elapsed speedup >=1.5x. |
| Small/unsupported-range guardrail | <=64 KiB single blob at default W=1, and 128 MiB missing-validator/ignored-range cases at W=2; same host and warm connection policy | p95 elapsed <=1.20x the comparable full-stream baseline; report extra requests/bytes. |
| Correctness/resource guardrail | Every mode and fault fixture | Exact final size/digest/graph; zero invalid publications; all configured caps respected. |

Use the same authentication/pooling policy in each comparison so repeated token
exchanges or fresh connections cannot manufacture the speedup. Report
coordinator/worker CPU, hashing/write time, queue wait and scratch I/O to expose
a coordinator ceiling, including a fast-local-storage/network run without
artificial per-connection throttling. Gates are reviewed before release, not
sleep-sensitive PR timing assertions. Failure requires optimization or an explicit
owner-approved scope/gate change, not a narrative claim of improvement.

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

Scratch is not necessarily disk: tempfile's platform root may be tmpfs or a
RAM-backed volume, so C*R then contributes to memory pressure. Artifact scratch
defaults to its private output namespace; standalone blob scratch defaults to
the platform temporary root (including TMPDIR/TEMP configuration). Both expose
`--scratch-dir` / `DownloadOptions.scratch_dir` for an existing caller-trusted
root. Report the effective root in structured status. Ranged bytes are written
to scratch and then the destination (or read/hash-discarded for verify-only);
this adds scratch writes/reads and may approximately double destination-related
write traffic. Range splitting remains opt-in for this reason.

### 11. Observability and troubleshooting

Retain object-level progress; chunks do not become new public progress rows.
Expose per-blob fallback reasons through the typed SDK callback; coalesce CLI
notices per fixed reason and print counts as in 7A, without URLs, credentials or
payloads. Counters distinguish accepted bytes from received payload including
discarded attempts. Existing opt-in request and timing telemetry remains the
source of HTTP detail, not the only programmatic interface.

Missing range support/validators: use the documented full-stream path. Repeated
429s: lower C or use `--sequential`, and opt into `--download-retries N`.
Corrupt parts/digests: fail and inspect
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
validates the proposed defaults and explicit range mode; 0.2 readiness requires
checks, docs, measured benchmarks and explicit release approval.
Default object parallelism in 0.2 is an explicit revision-2 behavior-change
proposal, with a migration note and sequential opt-out. Range splitting and
download retries remain explicit; neither becomes silently enabled with C=4.
There is no cloud-region deployment, private-preview program or inferred ship
date. No version bump, Git tag, release publication or merge is authorized here.

## Part II: engineering design

### A. Abstract

This design extends RegShape's synchronous download path with bounded object
parallelism and optional HTTP byte-range parallelism. A coordinator discovers
and validates the artifact graph, reserves unique content budgets, and schedules
blob request jobs through one shared worker pool. Public batch blob downloads
use the same cap without going through artifact pull or the CLI. Workers have
isolated request state but share one scoped authentication provider and bounded
connection pool. Independent full blobs are hashed/written on their workers;
ranged parts use private bounded spools and ordered assembly. Only the invoking
thread calls observers, updates rendering, and promotes/publishes artifact
storage. HTTP range support is optional under OCI, so capability
limitations explicitly select a full-stream strategy. Invalid protocol responses
or corrupt content fail rather than being hidden as capability fallback. A
strong HTTP validator binds parts before their bytes are combined, and the final
blob is always verified against its requested content digest. Object downloads
default to four slots with an explicit sequential escape hatch; existing
singular result schemas remain unchanged. Artifact publication continues to
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
together. The next-release request justifies this design; revision 2 explicitly
proposes changing default object scheduling while retaining opt-in ranges.

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
The diagram shows the **proposed revision-2 runtime**, not the current runtime.
Arrows describe work/data flow, not source imports.

```text
artifact pull / pull_artifact              blob get / get_blob / get_blobs
            | source + limits                      | digest + policy
            v                                      |
  coordinator: discover manifests ----------------+
  validate DAG; reserve unique blobs                |
            | bounded jobs                         v
            +--------------------------> shared download coordinator
                                         | C request/spool slots
                                         v
                              worker-local clients and Sessions
                              shared scoped auth + HTTP pool
                                         | HEAD/probe/GET
                                         v
                                registry -> storage redirect
                                         |
                              bounded private range spool files
                                         | immutable events/results
                                         v
                    full blobs: worker-local sink + hash
                    ranges: caller-thread ordered assembly + hash
                    caller thread: observers/status/render/store state
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
[telemetry](../../src/regshape/libs/decorators/), with explicit changes to the
[CLI renderer](../../src/regshape/cli/progress.py).
Proposed new files are `libs/blobs/download.py` for shared download policy,
scheduling and range receipt, and `libs/artifacts/download.py` for graph planning.
Expose policy, batch requests/results and typed status/statistics through the
existing blob/artifact package exports; executor implementation remains private.

#### D3. Artifact graph planning

For the default parallel policy, separate discovery from blob receipt:

1. Preflight inputs, policy, output and worker-client support before effects.
   Establish the operation's shared transport/auth context before document
   discovery so roots/manifests and later blobs reuse the same scoped token.
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

Explicit sequential policy retains one-at-a-time graph acquisition. Shared traversal changes
must preserve local snapshots, recursive push and archive validation as well.
Existing Linux/macOS, trusted-private-namespace and native no-replace
publication requirements are not relaxed; Windows artifact pulls still fail closed.

#### D4. Range negotiation and receipt

When W = 1, use ordinary full GET. Otherwise obtain a size hint from a trusted
descriptor or optional HEAD. Invalid/contradictory size metadata fails; missing
metadata can be recovered by a probe. HEAD 405/501 is a capability limitation,
not a blanket reason to ignore auth errors or 404.

If known N is zero or below 4R, use full GET without negotiation. When a trusted
descriptor already supplies eligible N, skip HEAD and send the first **real**
range, `Range: bytes=0-{min(R, N) - 1}` (substitute a decimal end), with identity
encoding. With unknown N, an optional
HEAD may avoid probing small content; missing HEAD metadata can use the same
first-R-byte range to discover N. Do not send a separate `bytes=0-0` probe.

The first request owns a normal global slot/spool reservation. Validate its
numeric complete length, strong ETag, range and body before accepting the part.
Retain it as range zero: it contributes to assembly/progress, not an extra
discarded byte/round trip. Only after establishing the binding issue later
ranges, keeping the normal W-window bound.

- First 206 with unknown complete length, missing/weak ETag or nonidentity HTTP
  coding: close it and use full GET with an explicit reason, without committing
  any part bytes to the sink/hasher.
- First 200: ranges were ignored. Before any range bytes commit, consume this
  same response through the full-stream receiver rather than close/re-GET it.
- First 416 with a validated size of zero: use full GET to verify the empty
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
replacement bytes. For a late range-to-full fallback after committed data,
perform the reset protocol in D5; closing spools alone does not reset a sink or
hasher. Never mix partial/full attempts. Permit at most one strategy
fallback per blob; the full receiver does not recursively attempt ranges.

A changed strong ETag remains a deliberate hard failure, not an OCI mandate or
proof that the final digest would differ. Missing a binding before acceptance
is a capability limit; losing an established binding mid-transfer invalidates
the current part set and may indicate stale proxy/storage behavior. Fail early
rather than automatically hide that inconsistency with another full download.
An explicit subsequent invocation may succeed after storage/validator rotation.
This conservative tradeoff can reject a byte-identical object with rotated
ETags; it is stated for approval rather than justified by the digest URI alone.

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

For independent **full-stream** blobs, the owning worker receives, hashes and
writes its own exclusive blob sink, including hash-and-discard verification
without output. It sends bounded immutable progress/results, not all payload
bytes through one coordinator thread. Do not hold a global lock across disk
writes or hashing. Coordinator ownership of SafeDirectory/ContentStore maps,
registrations and promotion does not prohibit isolated per-file worker writes.

For **ranged** blobs, the coordinator copies contiguous completed parts, updates
the full hash in original byte order, and writes the destination sink.
A successfully transferred but uncommitted part stays in the bounded window;
it is never hashed out of order. Whole blobs are not buffered in memory.
No full-stream entire-blob spool is introduced. Worker progress/status events
use bounded cancellation-aware backpressure. Public bytes are emitted only
by the invoking thread. Benchmarks must compare this choice with a
coordinator-copy reference and report whether ranged assembly becomes a ceiling;
offloading ranged assembly would require an equally bounded, reviewed design.

Output contracts deliberately differ where current behavior differs:

- Artifact/bounded `get_blob`: use the existing exclusive, identity-checked
  staging output and pre-write max_bytes checks. Promote only after full
  digest/size verification. Workers may own isolated sink handles, but may not
  mutate shared SafeDirectory/ContentStore bookkeeping or publication state.
- Standalone unbounded blob get with output: preserve existing overwrite and
  partial-file-on-most-failures semantics. Open/truncate only after acceptable
  response headers as today; do not promise atomic replacement. **Digest mismatch
  is the existing exception:** remove the invalid output even if this invocation
  overwrote a preexisting destination. Delete only if it still identifies the
  file opened by this invocation; never delete an unproven replacement.
  A deletion failure is logged without replacing the digest error. Ordinary
  transport/disk/callback failures may leave a partial output. Do not falsely
  promise preservation/restoration of overwritten contents.
- Blob get without output: hash ordered bytes and discard them. It needs only
  bounded range scratch, not a full-blob saved file.

Before abandoning any owned temporary state, stop new jobs, signal cancellation,
pump/discard pending records so producers cannot deadlock, close each worker's
response in that worker's finally block, and join all workers. Do not call
Future.cancel() and assume active I/O stopped. Requests read timeouts limit
socket **inactivity**, not elapsed read/operation time: a trickling peer can keep
a buffered read alive indefinitely. This version does not claim a bounded
cancellation-to-join latency or add an interruptible transport. Stop scheduling
and check cancellation between reads, but defer sink/scratch cleanup until the
blocked reader returns and all owners stop. Tests must simulate slow trickle,
verify cancellation is recorded promptly, then release the server so shutdown
and safe cleanup can complete; they must not assert a false timeout bound.
Cleanup failures are reported without masking the primary error.
Only identity-proven owned files/directories may be removed.

Full-stream retry is allowed only **before the first payload byte commits**
to a file or hasher, including verify-only hashing. Track this at the worker,
not from possibly delayed progress delivery. Once a full-stream body commits
anything, a body-read failure fails that blob; do not replay GET and append
a duplicate prefix. Pre-body status/connection failures remain retryable.

A deliberate ranged-to-full strategy fallback uses a separate, explicit reset:
cancel/drain/join every old range job for that blob, discard old-attempt queued
events/results and spools, validate the retained sink identity, seek/truncate
that invocation's writable sink to zero, replace its hasher (also verify-only),
clear accepted-prefix/window state, increment attempt and emit zero progress on
the **same** transfer ID. Only then may a new full GET write. Bytes received
remain cumulative; accepted bytes describe the current attempt. If resetting
cannot be proven safe, fail instead. This reset is not a license to replay
already committed full-stream bodies on a retry.

#### D6. Transport, callbacks and telemetry

Create one operation-owned download transport context before manifest discovery
or blob work. Resolve credentials once from the borrowed client's snapshot.
Worker clients isolate request headers, cookies, middleware/cache and
last_response, but **do not negotiate independent tokens or create a fresh
connection for every range**.

The context owns a thread-safe, single-flight auth provider keyed by registry
origin, credential identity, validated realm/service and repository action
scope. For an unknown scope, one leader discovers/validates the challenge and
fetches the token; followers wait without sending duplicate unauthenticated
requests. Reuse that token on eligible same-origin requests. Track expiry when
provided; expiry or a rejected token generation causes one shared refresh, not
one refresh per waiting worker. Thus "once" means once per valid token
lifetime/scope, not never again after expiry. Anonymous and Basic flows keep
their origin-bound behavior. A foreign 401 cannot warm or poison this provider.
Token requests run inside the leader's request slot, with finite timeouts;
route them through the same session/instrumentation hooks.

Use worker-local Requests Sessions over an operation-owned shared HTTPAdapter
pool with blocking checkout and pool_maxsize=C, reusing connections across jobs.
No Session, cookie jar or default Authorization header is shared across threads.
Extend the existing instrumented http_request path to accept the owned session;
do not bypass telemetry or add a competing HTTP stack. Bound origin-pool caches
and preserve proxy/CA/environment behavior and redirect credential stripping.
After all responses/jobs end, detach the borrowed adapter from child sessions,
close child-local resources, then close the shared adapter exactly once; a
worker Session.close must not tear down pools still used by peers.

`RegistryClient.fork_for_download()` participates in this context rather than
cloning auth state into unrelated token negotiations. The original client is
borrowed and never closed or mutated by workers. Document/validate its extension
hook for explicit custom-client/middleware support without sharing unsafe state.

The built-in implementation supports the exact base RegistryClient and built-in
middleware. A subclass or configured custom middleware must provide an explicit
safe fork override; otherwise reject parallel policy with ValueError **before
network/filesystem effects**. Never silently drop custom middleware or copy it
unsafely. Custom-client callers can select the documented sequential opt-out; default
parallel rejection for unsupported customization is an intentional migration
impact, not a promise that every old caller's scheduling is unchanged.
Built-in serial downloads still use the scoped auth/pool context with C=1, so
benchmark baselines do not retain needless token/connection churn. Unsupported
custom clients may retain their legacy serial singular/artifact path without
new status observations; requesting the new batch/status contract requires a
supported adapter and fails preflight otherwise, rather than fabricating stats.
Worker clients preserve origins, TLS/insecure mode, timeout, auth behavior and
logging/cache policy; streamed GETs still bypass response caching. The caller's
last_response is not an authoritative list of parallel responses and is not
updated by workers. Document this parallel-mode contract.

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

The bounded coordinator queue holds at most 4C records, each at most 64 KiB.
Progress/status records contain no raw payload; range results reference spool
files, not payload copies. Reject
oversized diagnostic records explicitly rather than buffering without limit.
Producers check cancellation while waiting to enqueue. The coordinator pumps
events while awaiting jobs; it must not block on a future while workers block
on its full queue.

All public callbacks, including download status observers, execute on the
invoking thread, one at a time,
through the current immutable public event models. Callback exceptions retain
their exact identity, permanently disable that observer, cancel work and
propagate after worker cleanup. Full-stream/range fallback starts a new logical
attempt with zero accepted bytes; range retries do not double-count accepted
bytes. Artifact adapters preserve attempt values and one logical row per digest.
Completed/stored events and stdout require verification and applicable output
close/publication; telemetry close errors cannot leave a success-shaped result.

#### D7. Interleaved CLI rows and formatting ownership

[Coordinator/renderer item #135](https://github.com/toddysm/regshape/issues/135)
owns removal of the shared renderer's single-active-row RuntimeError and the
functional multi-row state machine. Before either CLI can enable parallel
downloads it must accept interleaved rows, keep transfer IDs/row ordinals stable,
aggregate ranges into the parent blob, and reset attempt/rate/count state on
retry/fallback without creating a row or finalizing another active object.
Only verified terminal events finalize that row; a full bar is not success.
Bound live row state and redraw work by active objects, retaining finished rows
without unbounded retained event history. Preserve stderr/stdout suppression,
diagnostic handoff, narrow-terminal cleanup and other renderer consumers.

[Formatting item #141](https://github.com/toddysm/regshape/issues/141) consumes
that event/row contract for colors, columns and square bars. Its behavioral
integration depends on #135; its visual work is not a prerequisite for the
download scheduler and must not be used to postpone the required functional
renderer fix. Coordinate shared-file edits; test the formatting item against
interleaved/combined events after #135 lands. There is no dependency cycle.

### PM-1. Requirements traceability

| Requirement/scenario | Design and acceptance evidence |
| --- | --- |
| R1 / S1 | D3 planning/deduplication, D5 scheduler, final graph/publication tests. |
| R2 / S2 | D4 range protocol, D5 ordered hashing and verify-only sink. |
| R3 / S3 | D1 standards matrix, D4 capabilities/consistency, PM-5 errors. |
| R4 | Part I.10 limits, D3 reservations, D5 windows, D6 queue bounds. |
| R5 | PM-2 default/opt-out interfaces, D5 output contracts, D6 shared auth/pooling. |
| R6 / S4 / S5 | PM-2 batch/status API, D5 drain/join/ownership, D6 callbacks, D7 renderer. |
| S6 | PM-2 CLI retry controls, PM-5 429 budget/diagnostics and recovery. |
| R7 | Part I.8 measurements, PM-7 deterministic and hosted tests. |
| R8 | PM-6 final documentation item and O launch checklist. |

No separate remote service, upload change or persisted resume format is
introduced without a requirement. Optional layer selection is not silently added.

### PM-2. API and CLI contracts

Proposed frozen public `DownloadOptions`, exported by blob/artifact packages:

| Field | Type/default | Validation |
| --- | --- | --- |
| concurrency | int = 4 | 1..32, not bool |
| ranges_per_blob | int = 1 | 1..concurrency, not bool |
| range_size | int = 8388608 | 65536..67108864 bytes, not bool |
| max_retries | int or None = None | None inherits transport enablement/budget; otherwise 0..10; reject bool. |
| scratch_dir | path or None = None | If given, existing caller-trusted temporary root; validate before effects. |

Add keyword-only `download_options: DownloadOptions | None = None` to existing
`get_blob` and `pull_artifact`; preserve all existing positional parameters,
return types, errors, max_bytes/output_dir_fd and progress parameters. **None
now resolves to C=4/W=1**, not the old sequential default. Add
`download_status_callback: Callable[[DownloadStatus], None] | None = None`
to both operations and the new batch operation. Invalid policy/worker support
raises ValueError before effects; existing digest validation retains BlobError.
`DownloadOptions.sequential()` returns C=W=1 (other fields at defaults);
explicit C=W=1 also selects serial scheduling regardless of range_size.

Expose public `get_blobs(client, repo, downloads, *, download_options=None,
progress_callback=None, download_status_callback=None) -> BlobBatchResult`.
It uses the same scheduler as artifact pull. Each frozen BlobDownload has
`digest`, optional `output_path`/`total_size`/`max_bytes`/`output_dir_fd`, and
`chunk_size=65536`, following singular validation/ownership contracts.
Materialize/validate at most 100,000 requests before effects; reject duplicate
digests and colliding output paths/existing file identities in this first batch
API. Artifact deduplication remains independent and mandatory. None output
means verify-only, not an output collision.

BlobBatchResult contains `blobs: tuple[BlobInfo, ...]` in input order and final
`stats: DownloadStats`. On the first observed failure, cancel/drain the batch
and re-raise the original domain/observer error; do not return a partial
success-shaped result. Earlier successful standalone files need not be undone:
this is not an atomic multi-file publication API. Status events identify which
transfers actually completed. Separate manually concurrent calls do not share
a process-global cap; use one get_blobs invocation for that guarantee.

Both CLI commands expose `--concurrency`, `--ranges-per-blob`, `--range-size`,
`--sequential`, `--download-retries N`, and `--scratch-dir PATH`.
Use Click parameter provenance to reject --sequential with either explicitly
supplied concurrency/ranges-per-blob, rather than confusing the default C=4
with a user conflict. Validate all relations before credentials/filesystem
effects. C > 1/W=1 is object parallelism; a standalone single blob still makes
one full GET. W > 1 enables eligible ranges. `--download-retries` defaults to
inheritance (zero in today's CLI), but N explicitly enables/sets the download
budget and reaches the 429 policy in PM-5. Do not claim existing TransportConfig
already enables retries or already includes 429.

Add these frozen SDK observations without changing existing result/progress
fields:

| Public type / field | Contract |
| --- | --- |
| DownloadStats | request_count, retry_count, bytes_received, bytes_accepted, active_requests_peak, scratch_bytes_peak, elapsed_seconds, receive_seconds, hash_seconds, write_seconds and queue_wait_seconds; nonnegative counts/durations except accepted-byte reset semantics below. |
| DownloadStatus identity | transfer_id/digest for a blob event; both None for an operation summary. The blob ID matches its progress row across chunks/attempts. |
| DownloadStatus event | Fixed values strategy, fallback, retry, completed, failed, cancelled, summary. |
| DownloadStatus strategy/reason | full/ranged or None before selection; fixed safe reason code or None; optional HTTP status for e.g. 429, never a URL/exception message. |
| DownloadStatus attempt/stats/scratch_root | Logical attempt >=1 for a blob, None for summary; immutable stats snapshot; effective caller-owned scratch root or None if unused. |

Per-blob stats measure that transfer; summary/batch stats aggregate the
operation. Duration counters sum work/wait across jobs, so overlapping worker
durations can exceed elapsed_seconds. request_count includes physical HTTP
hops/auth exchanges/retries;
bytes_received means consumed blob entity payload, including discarded
attempts, not TLS/header/auth-response bytes or a wire-exact egress meter.
bytes_accepted is the current attempt's committed prefix, not a claim that it
already verified; a strategy reset may reduce it. Completion still requires
full verification. Peak counters never decrease. Emit strategy before payload,
fallback/retry when selected, and terminal/summary snapshots after cleanup.
On failure, callbacks receive available failed/cancelled/summary events unless
an observer already failed; never call a failed observer again or replace its
exception to deliver a final report.

Fixed reason codes are `sequential`, `ranges_disabled`, `range_enabled`,
`small_blob`, `unknown_size`, `no_strong_validator`, `range_ignored`,
`nonidentity_encoding`, `short_range`, `http_status`, and `transport_error`.
Terminal/summary events may use None; the raised domain exception remains the
failure authority. CLI messages map codes to bounded human-readable notices.

Keep existing BlobInfo, ArtifactPullResult, TransferProgress and ArtifactProgress
fields and singular CLI stdout schemas unchanged. CLI diagnostics consume the
same typed statuses; SDK users never need to scrape logs. Download contexts
own/close their sessions, shared adapter and auth state; the input client remains
borrowed and no persistent credentials/cache file is added.

No OCI endpoint/version, layout format, descriptor bytes, persisted schema or
cache-key version changes. New Python symbols/options are additive 0.2 APIs;
no REST api-version or event serialization schema is introduced.

### PM-3. Metrics, usage and cost

Measure request jobs, active-request high water, accepted versus received bytes,
fallback reason, retry count, receive/hash/overall duration and scratch high
water. Merge existing per-request metrics on the coordinator; do not call HTTP
header latency complete-transfer time. Expose these measurements through DownloadStats/DownloadStatus as well as
benchmark instrumentation, without adding public progress fields.

Map integrity to final verification failures/publications; boundedness to peak
requests/scratch; attainable throughput to benchmark bytes/elapsed; adoption to
the explicit options in user-supplied diagnostics. Use low-cardinality strategy
and reason values, not registry/repository/digest labels in aggregate metrics.

This is not a hosted service: there is no backend customer identity, collection
consent, adoption database or churn measurement. Do not add unsolicited remote
tracking to satisfy a service rubric. Maintainers may retain benchmark reports;
operators may persist existing opt-in local logs under their retention policy.
Longitudinal customer adoption data is an explicitly unestablished product metric.

Caller cost is original content bytes plus optional HEAD/negotiation/retry/
fallback overhead, extra HTTP requests and at most C*R range scratch beyond
existing output. First-range negotiation reuses useful payload; it is not an
extra one-byte request. Scratch may be tmpfs/RAM, and ranged payload incurs
scratch writes/reads in addition to destination I/O; report placement and costs.
Compare CPU/hash time, bytes and scratch high water in benchmark reports; registry
GET/egress costs use the operator's own provider rates, not an invented price.

### PM-4. Authentication, authorization and networking

Identity and permissions remain unchanged: stored registry credentials or
anonymous access, with repository pull scope. No push permission is needed.
Every job, auth exchange and storage redirect stays within its worker slot and
the existing transport security policy. A foreign 401 must not initiate registry
credential negotiation. Credentials are not attached indiscriminately to
storage redirects or recorded in new diagnostics.
Root/manifest/blob/range work shares the operation's scoped single-flight token
provider and bounded connection pool. Cache tokens only in memory for that
operation; validate origin/realm/scope and expiry, never reuse across unrelated
credentials or repositories. Pooling is not permission to share mutable Sessions.

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
| 429/selected transient 5xx/transport errors | Retry only under the resolved download budget and pre-commit guard; otherwise explicit failure with actionable CLI controls. |
| Byte/graph limit, disk error, unsafe/replaced path | Existing domain/filesystem error and identity-safe cleanup. |
| Callback/sink exception, Ctrl-C | Preserve original exception, cancel/drain/join; no more calls into a failed observer. |

Use one retry owner per transaction. Resolve DownloadOptions.max_retries first:
None inherits enable_retries and RetryConfig (zero if disabled); an integer
overrides the download budget without changing uploads/other commands. Validate
the resolved budget in 0..10. Without an explicit RetryConfig, download retry
statuses are `(429, 500, 502, 503, 504)` with exponential backoff factor 1.
An explicitly supplied RetryConfig keeps its statuses/backoff authoritative;
SDK callers must include 429 there if they override the set. Thus the CLI's
`--download-retries 3` really enables 429 retries despite today's transport defaults.

The coordinator manages at most budget+1 attempts per job; disable child
RetryMiddleware for these jobs so budgets are not multiplied. With budget zero,
do not add hidden retries. Discard failed range spools before retry. Full-stream
GET may retry only before **any** sink/hash byte commits, including verify-only;
a later body failure propagates rather than replaying a prefix. Strategy
fallback is a different one-time reset transaction under D5. No retries for
integrity/protocol errors or arbitrary observer exceptions.

For retryable 429/503, honor a valid Retry-After by scheduling no sooner than
the greater of it and configured exponential backoff; absent headers use existing
backoff. A malformed Retry-After is an explicit protocol diagnostic with the
configured backoff still applied. Backoff is cancellation-aware and releases the
network slot after response close; delay metadata remains under the C-job bound.
GET/HEAD retries are read-only; there are no registry mutations or upload sessions.

Existing finite inactivity timeouts apply, not a whole-read or cancellation
deadline. Document the slow-trickle shutdown limitation from D5. A stale size, encoded response,
duplicate descriptor, zero length, missing Content-Length, retry/fallback attempt,
and completion in arbitrary order must each have an explicit test.

### E. Risk identification and assessment

| Risk category | Assessment / mitigation |
| --- | --- |
| API/CLI compatibility | Intentional C=4 default/migration note and sequential opt-out; preserve output/buffer contracts including digest-mismatch removal (PM-2/D5). |
| Security/resource exhaustion | Global/window/byte/graph bounds, strict range parsing, private identity-checked scratch (D3-D5). |
| Privacy | No new remote collection; safe new diagnostics and owned temporary retention (H). |
| Auth | Shared scoped single-flight provider/pool, isolated request state, origin/redirect checks and explicit custom-client migration (D6). |
| Dependency/infrastructure | No new package/service; optional registry capabilities fall back explicitly (D1/D4). |
| Deployment/rollback | Default object parallelism, explicit range/retry options, unchanged storage format and sequential escape hatch (K/N). |
| Contention/noisy neighbor | One C cap, round-robin blobs, bounded slots/backpressure and configurable retry policy (D5/PM-5). |
| Regression: blobs, transport, telemetry | Existing sequential tests plus fork/queue/encoding/observer regressions (M/PM-7). |
| Regression: graph, storage, archive, CLI | Shared walker equivalence, complete-layout/archive round-trip, JSON and native-platform tests (M/PM-7). |

### F. Breaking changes

Default object parallelism is an **intentional behavioral change**: omitted
options now permit four in-flight requests and interleaved object progress.
Document migration to --sequential / DownloadOptions.sequential(), conflict
rules, registry-load effects and unsupported custom-client parallel adapters.
Range splitting and retries remain explicit. No positional API, existing
singular result schema or persisted-format change is intended. Preserve
standalone overwrite and digest-mismatch deletion contracts; concurrent
public callbacks remain forbidden. CLI renderer support must land before the
new default is enabled. Shared code retains all regression/compatibility jobs.

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

Activate the new default only in integration changes with the functional
renderer/transport foundation already tested, not in policy-only scaffolding.
Keep the capability Planned/Partial until both R1 and R2 and final docs meet
their gates. No unrelated telemetry rewrite or legacy documentation sweep.

### N. Rollback

Use --sequential or DownloadOptions.sequential() (C=W=1) for serial behavior;
omitting policy now selects C=4 and is **not** an opt-out. A package rollback
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
| W1 [#134](https://github.com/toddysm/regshape/issues/134) | Policy, scoped single-flight auth and pooled worker clients, including token metadata/instrumented-session hooks | Approved design | 3 / 2 / 0.5 |
| W2 [#135](https://github.com/toddysm/regshape/issues/135) | Common coordinator/full-stream foundation, worker hash/write, public batch/status API, cancellation, telemetry and functional interleaved renderer | W1 | 3 / 3 / 0.5 |
| W3 [#136](https://github.com/toddysm/regshape/issues/136) | Ranged receiver, ordered assembly, safe reset/retry and standalone blob CLI controls | W2 | 2 / 2 / 0.5 |
| W4 [#137](https://github.com/toddysm/regshape/issues/137) | Shared graph planning, parallel artifact integration/CLI and common controls; ranges plug into the common receiver | W2, not range-specific W3 | 3 / 2 / 0.5 |
| W5 [#138](https://github.com/toddysm/regshape/issues/138) | Fault/compatibility validation, numeric benchmarks/profiling, real-registry matrix and CI selectors | W3, W4 | 2 / 3 / 0.5 |
| W6 [#139](https://github.com/toddysm/regshape/issues/139) | Final user/API/CLI documentation and capability close-out | W1-W5 | 1 / 0.5 / 0.5 |

These are native sub-issues of the feature and all target milestone 0.2.
W3 and W4 can proceed independently on W2; W4 must work with full-stream jobs
without waiting for range-specific W3. Both capabilities still gate the proposed
0.2 feature release. Splitting R1 into an earlier release requires an explicit
owner-approved replan, not an engineering dependency masquerading as priority.
#135 owns baseline renderer functionality; #141 owns its separate visual changes
and validates against #135's event contract, without blocking the foundation.
Each code item must include current file-purpose and applicable type/callable
contracts, including private code and test helpers, following the repository's
Sphinx-style Python convention. This obligation is scoped to new/changed
declarations, not legacy boilerplate. Exactly one final user-documentation item
is required and does not substitute for source contracts.

### PM-7. Test plan and CI

| Coverage | Required deterministic evidence |
| --- | --- |
| S1 / graph | Default C=4/W=1 overlaps distinct blobs; explicit sequential does not. Shared digest requested once; all graph limits reserve before work; repeated incompatible descriptors fail. |
| S2 / range | Multiple ranges overlap; reverse completion; one-byte tails; known N skips HEAD and first real part is reused (no extra one-byte probe); exact size/digest and verify-only equivalence. |
| S3 / HTTP | First 200 reuses the response; 416/empty negotiation, absent/advisory Accept-Ranges, missing HEAD size, 405/501, missing/weak/changed ETag, shorter parts and unknown totals. |
| S4 / failures | Invalid bounds/unit/length/coding, multipart, truncation/overrun, hash mismatch, 401/403/404/429/5xx and Retry-After; no invalid success/publication. |
| S5 / public SDK | Distinct get_blobs entries share one C cap, ordered results, duplicate/colliding sink preflight, fail-fast partial-side-effect contract; typed strategy/fallback/retry/final metrics and exact callback exception identity on caller thread. |
| S6 / throttling/replay | CLI retry flag reaches 429, default zero and explicit status overrides, cancellation-aware delay, no nested budgets. Interrupted full GET after a committed prefix never replays/appends, also verify-only; prefix range fallback resets sink/hash/progress/attempt before a clean GET. |
| Concurrency/resources | Peak requests <= C including auth/probes/redirects; per-blob <= W; pending futures/spools/queue under bounds; stalled earliest range cannot grow scratch without limit. |
| Auth/pooling | Same-scope root/manifests/blobs/ranges share one valid token negotiation; forced expiry/rejection refreshes once per generation; origin/scope separation and foreign 401 protections; persistent connection reuse and no peer pool closure or instrumentation bypass. |
| Ownership | Observer throws with active writes, full queue/cancellation, disk/short-write/close errors and replaced paths; join before cleanup. Digest mismatch deletes this invocation's invalid output even after overwrite; other partial-output semantics remain, and replacements are not deleted. |
| Slow trickle | Signal cancellation while an inactivity-safe trickling read remains blocked, assert no new scheduling/unsafe cleanup, release handler and then assert join/cleanup; no bounded whole-read cancellation claim. |
| Renderer/output | Interleaved rows/chunks/attempt resets never raise the old concurrency RuntimeError; final success is verification-gated; bounded fallback notices/count summary, exact stderr/exit table, no-progress/JSON/NO_COLOR and #141 integration contract. |
| Compatibility | Explicit sequential content/output equivalence, default C=4, CLI conflict/provenance validation, exact result fields, stdout cleanliness, source contracts and custom-client preflight. |
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

Before release, run a separate integration matrix against at least two actual
registry implementations (for example pinned Distribution and Zot) plus an
actual storage-redirect-backed deployment. Obtain authorized endpoints/test
credentials and record exact registry versions/configuration, backend/redirect
origin, range/ETag behavior, fallback reasons, auth/connection/request counts,
integrity and timing. Exercise artifact pull and blob get with default, range
and sequential options. Mark unsupported capabilities and expected fallback,
not universal range support. Local synthetic redirect handlers remain necessary
unit tests but do not substitute for this evidence. Do not provision cloud
resources or use private endpoints/credentials without authorization; absent
an authorized redirect environment, report the gap and hold the release gate
or obtain an explicit owner-approved exception.

Record the section 8 numeric benchmark gates and CPU/queue/hash/disk/scratch
profiles with raw measurements, including a fast-local run and common auth/pool
configuration. A high-throughput plateau must be explained, not hidden by RTT
shaping. Neither benchmark fixtures nor registry trials are invented results.

### O. Next actions and launch checklist

- [ ] Owner approves this revision, proposed defaults and linked breakdown.
- [ ] Separately authorize the first implementation item and its test mode.
- [ ] Complete dependency-ordered delivery with required checks/reviews.
- [ ] Meet section 8 numeric performance/resource gates and review CPU/I/O profiles.
- [ ] Record real-registry plus storage-redirect interoperability evidence or approved exception.
- [ ] Deliver W6 docs for default migration, SDK batch/status APIs, both commands,
      retry controls, scratch placement/cost, fallback and failure troubleshooting.
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

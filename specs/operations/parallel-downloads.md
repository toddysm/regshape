# Parallel artifact and ranged blob downloads: requirements and engineering design

- **Status:** Draft revision 3; awaiting owner approval. No runtime implementation.
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

Revision 3 addresses the four selected follow-up comments on
[design PR #140](https://github.com/toddysm/regshape/pull/140). The owner approved
these directions and the two-item split: preserve omitted-option custom-client
calls, expose a genuine legacy transport path, enable three bounded modern
retries by default, and separate coordinator, public APIs and renderer work.
Approval of those directions is not approval to merge this exact revision or
start runtime implementation. The reopened validator question is unchanged.

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
actionable. Modern downloads now automatically allow three retries before any
payload commits, honoring Retry-After and cancellation-aware backoff. After
exhaustion they see a safe diagnostic and can reduce `--concurrency` to two or
choose another `--download-retries N` budget; zero disables modern retries.
Exhaustion still exits nonzero with no verified result, and status counters
separate retry traffic from accepted bytes. This is bounded default resilience,
not automatic adaptive concurrency or an unbounded retry loop. Legacy transport
retains the original caller retry policy rather than receiving this new default.

### 4. Outcomes, priorities and scope

| ID | Release requirement |
| --- | --- |
| R1 | Concurrent independent config/layer blobs for complete artifacts, with deduplication and unchanged graph/layout/archive semantics. |
| R2 | Concurrent ranged downloads of a single blob, shared by artifact pull and blob get, with full size/digest verification. |
| R3 | OCI/HTTP-compliant negotiation, response validation, consistency and explicit full-stream fallback. |
| R4 | One operation-wide concurrency bound; bounded queued work, memory and temporary range storage. |
| R5 | Default pooled C=4/W=1, sequential scheduling and genuine legacy-transport opt-outs, omitted-option custom-client compatibility, unchanged results/buffer meaning, and shared scoped credentials in pooled mode. |
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

SDK callers use `DownloadOptions.sequential()` for serial scheduling on the
modern pooled transport. `--legacy-transport` / `DownloadOptions.legacy()` is the
separate rollback path for singular blob/artifact downloads: it bypasses the new
auth provider, pool and coordinator and invokes the retained original serial
path. It does not promise to undo every shared parser/renderer change.

Existing custom-client get_blob/pull_artifact calls with omitted options use
that original path when no safe pooled adapter exists, with one safe warning per
invocation. Explicit pooled policies still require a supported adapter, even
when a singular W=1 call only needs one GET. Public get_blobs and new status
callbacks require pooled transport; legacy calls preserve existing progress
callbacks/results but reject unavailable new metrics before effects.

Modern CLI defaults allow three retries; --download-retries 0 disables them.
Legacy transport retains the caller's original retry configuration. Its flag
rejects explicit --sequential, --concurrency, --ranges-per-blob,
--download-retries or --scratch-dir; range-size remains validated but unused.
The SDK legacy helper sets C=W=1, transport="legacy", max_retries=None and no
scratch root. This avoids silently ignoring explicitly requested new features.

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
legacy = get_blob(
    client, "team/app", digest,
    download_options=DownloadOptions.legacy(),
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

Use the same authentication/pooling and retry policy in each controlled
comparison so token exchanges, fresh connections or differing budgets cannot
manufacture the speedup. DownloadOptions.sequential() is still pooled C=1;
measure --legacy-transport separately as an end-to-end rollback baseline and
report its original auth/connection behavior rather than attributing every
transport improvement to concurrency. Report
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
Default object parallelism in 0.2 is an explicit revision-3 behavior-change
proposal, with a migration note and sequential opt-out. Range splitting remains explicit. Pooled downloads intentionally receive the
new bounded three-retry default; legacy transport keeps its original retry policy.
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
The diagram shows the **proposed revision-3 pooled runtime**, not the current
runtime. Explicit/compatibility legacy selection bypasses this whole worker
context and uses the retained serial path described in D6/N.
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

For the pooled parallel policy, separate discovery from blob receipt. Resolve
the D6 backend before entering this path: legacy transport uses the retained
original serial acquisition path and never constructs this coordinator/context.

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

Resolve the backend before manifest discovery, worker creation or filesystem
writes. Existing get_blob/pull_artifact calls with download_options=None choose
pooled defaults for supported clients, but use the original serial path for a
subclass/custom middleware without a safe adapter. Emit one sanitized warning
that custom-client compatibility selected legacy transport; do not silently
ignore an explicit parallel request. get_blobs is a new API and does not have
this omitted-option compatibility exception.

Explicit transport="legacy" is valid only for singular blob/artifact operations
with C=W=1, max_retries=None, scratch_dir=None and no new download_status_callback.
Call the retained original serial path with its original borrowed client,
auth/retry/middleware behavior and existing progress/output contracts. Do not
create the new auth provider, shared adapter, worker clones or coordinator, and
do not automatically re-enter pooled mode after a legacy failure. Reject new
batch/status/explicit download-retry features before effects rather than return
fabricated metrics or silently alter custom middleware.

Only pooled mode creates an operation-owned transport context before root,
manifest or blob work. Resolve credentials once from the borrowed snapshot.
Worker clients isolate headers, cookies, middleware/cache and last_response,
but **do not negotiate independent tokens or create a fresh connection per range**.

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

The pooled implementation supports the exact base RegistryClient and built-in
middleware. A subclass or configured custom middleware needs an explicit safe
fork/adapter. An explicit pooled policy, new get_blobs call or status observer
without that support raises ValueError before network/filesystem effects, even
if C=4/W=1 results in only one singular GET. None on the two existing operations
instead preserves the custom client's original path. Never silently drop or
copy unsafe customization.

DownloadOptions.sequential() means pooled C=1, not transport rollback; it still
uses the new scoped auth/pool context and supports new diagnostics. Explicit
legacy selection bypasses that layer for built-in and custom clients alike.
Pooled worker clients preserve origins, TLS/insecure mode, timeout, auth and
logging/cache policy; streamed GETs bypass caching. The borrowed caller's
last_response is not an authoritative list of parallel responses and is not
mutated by workers. Legacy last_response behavior remains the original client's.

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

[Renderer item #145](https://github.com/toddysm/regshape/issues/145) now owns
removal of the shared renderer's single-active-row RuntimeError and its
functional multi-row state machine. [Core #135](https://github.com/toddysm/regshape/issues/135)
owns immutable progress/event records; [API #144](https://github.com/toddysm/regshape/issues/144)
owns public batch/status facades. These are separate work items and PRs.

Before either CLI enables default parallel integration, accept interleaved rows,
retain transfer IDs/ordinals, aggregate ranges into the parent blob, and reset
attempt/rate/count state on retry/fallback without duplicating or prematurely
finalizing rows. Only verified terminal events finalize a row; a full bar is not
success. Bound live state/redraw work and preserve stderr/stdout suppression,
diagnostic handoff, narrow-terminal cleanup and other renderer consumers.
Test pooled sequential and original legacy progress as well as combined modes.

[Formatting #141](https://github.com/toddysm/regshape/issues/141) consumes #145's
functional row contract for columns/colors/bars. Its visual work is not a
prerequisite for #145 or the scheduler; coordinate shared-file edits and test
against #145 after it lands. There is no reverse dependency or cycle.

### PM-1. Requirements traceability

| Requirement/scenario | Design and acceptance evidence |
| --- | --- |
| R1 / S1 | D3 planning/deduplication, D5 scheduler, final graph/publication tests. |
| R2 / S2 | D4 range protocol, D5 ordered hashing and verify-only sink. |
| R3 / S3 | D1 standards matrix, D4 capabilities/consistency, PM-5 errors. |
| R4 | Part I.10 limits, D3 reservations, D5 windows, D6 queue bounds. |
| R5 | PM-2 pooled/legacy selection, D5 output contracts, D6 custom-client compatibility and auth/pooling. |
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
| max_retries | int or None = None | Pooled None resolves to three unless an explicit caller retry policy applies; integer overrides in 0..10, not bool. Legacy requires None and retains the original client policy. |
| scratch_dir | path or None = None | Existing caller-trusted temporary root; validate before effects. Legacy requires None. |
| transport | "pooled" or "legacy" = "pooled" | Legacy requires C=W=1 and the singular/artifact API without new status observers; reject unsupported combinations before effects. |

Add keyword-only `download_options: DownloadOptions | None = None` to existing
`get_blob` and `pull_artifact`; preserve all existing positional parameters,
return types, errors, max_bytes/output_dir_fd and progress parameters. **None
resolves to pooled C=4/W=1 for supported clients**; on the two existing operations,
unsupported custom clients retain their logged original serial path (D6). This
exception is not available to new get_blobs/status contracts. Add
`download_status_callback: Callable[[DownloadStatus], None] | None = None`
to both operations and the new batch operation. Invalid policy/worker support
raises ValueError before effects; existing digest validation retains BlobError.
`DownloadOptions.sequential()` returns pooled C=W=1 with other defaults; explicit
C=W=1 also means serial pooled scheduling. `DownloadOptions.legacy()` returns
C=W=1/transport="legacy"/max_retries=None/scratch_dir=None. Legacy has no new
status callback or batch result metrics; reject those combinations before effects.

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
`--sequential`, `--legacy-transport`, `--download-retries N`, and `--scratch-dir PATH`.
Use Click parameter provenance to reject --sequential with either explicitly
supplied concurrency/ranges-per-blob, rather than confusing the default C=4
with a user conflict. Validate all relations before credentials/filesystem
effects. C > 1/W=1 is object parallelism; a standalone single blob still makes
one full GET. W > 1 enables eligible ranges. `--download-retries` defaults to
the resolved pooled default of three, not today's disabled legacy middleware.
N explicitly sets the budget, including zero to disable, under PM-5. The legacy
flag selects the SDK legacy helper and rejects explicitly supplied --sequential,
--concurrency, --ranges-per-blob, --download-retries and --scratch-dir rather than
silently ignoring them; range-size stays validated/unused. No new auth/pool
context is initialized for that backend. Existing TransportConfig does not
already enable the new retry policy or include 429.

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
own/close their pooled sessions, shared adapter and auth state; legacy creates
none of these new resources. The input client stays borrowed and no persistent
credentials/cache file is added. Legacy returns existing singular result shapes
but cannot satisfy new batch/status observations; reject, do not fabricate stats.

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
| Invalid options, explicit unsupported pooled/batch/status request | Preflight ValueError / Click parameter error; no side effects. Omitted-options compatibility for existing custom callers uses logged legacy transport, not this error. |
| Missing range support, usable size or strong ETag | Explicit one-time per-blob full-stream fallback under D4. |
| Unknown HTTP range total or valid shorter range | Capability-limited fallback, not a false protocol-corruption diagnosis. |
| Auth rejection, 403, missing blob 404 | Preserve AuthError/BlobError taxonomy/status; no capability fallback. |
| Malformed/contradictory Content-Range, changed ETag, unexpected coding, short/extra body | BlobError; cancel; do not publish or silently retry as full content. |
| 416 | BlobError, except validated zero-size probe selecting full GET; never mark successful from 416 alone. |
| 429/selected transient 5xx/transport errors | Retry only under the resolved download budget and pre-commit guard; otherwise explicit failure with actionable CLI controls. |
| Byte/graph limit, disk error, unsafe/replaced path | Existing domain/filesystem error and identity-safe cleanup. |
| Callback/sink exception, Ctrl-C | Preserve original exception, cancel/drain/join; no more calls into a failed observer. |

Use one retry owner per pooled transaction. Resolve max_retries in this order:

1. An explicit integer DownloadOptions/CLI override wins, including zero.
2. Otherwise, if the caller enabled transport retries, retain its configured
   budget (or RetryConfig's default budget when no configuration was supplied).
3. An explicitly supplied RetryConfig with enable_retries=False retains zero.
4. Otherwise use the new pooled default of three retries. Thus today's CLI
   TransportConfig(false, retry_config=None) gets three in pooled mode, not zero.

Validate the resolved budget in 0..10. With no explicit RetryConfig, download
retry statuses are (429, 500, 502, 503, 504), with exponential backoff factor 1.
A supplied RetryConfig retains its statuses/backoff; callers overriding the set
must include 429 themselves. enable_retries=False without an explicit config
controls the old middleware, not the new default budget: use max_retries=0 or
--download-retries 0 to disable pooled retries explicitly. This deliberate
migration applies only to download operations, never uploads/other commands.

Legacy transport bypasses this resolver/coordinator and preserves the original
borrowed client's enablement, budget, status set and middleware. It rejects
explicit new download retry overrides rather than mutating a custom client or
claiming the legacy path now implements the pooled policy.

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
Range splitting remains explicit; three pooled retries are a deliberate new
default, with zero disabling them. Omitted-option unsupported custom callers
retain the original serial transport; explicit pooled requests need an adapter.
--sequential only limits pooled scheduling, while --legacy-transport bypasses
the new auth/pool/coordinator. No positional API, existing
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

--sequential / DownloadOptions.sequential() limits pooled scheduling to C=W=1;
it is not protection from a defect in the new auth provider or connection pool.
Use --legacy-transport / DownloadOptions.legacy() on get_blob/pull_artifact to
invoke the retained original serial transport/acquisition path for the entire
operation, including root and manifests. No new auth provider, shared adapter,
worker clone or coordinator is created; original auth/retry/custom middleware
and progress/result behavior remain. Never silently switch back to pooled mode.

Legacy cannot provide the new get_blobs/status contract; reject those requests
before effects. A batch caller can temporarily loop over legacy singular calls.
No new persisted format exists, so package rollback remains available for shared
parser/renderer or other code regressions that the transport selector cannot
undo. Omitted options are not a general rollback switch: supported clients use
pooled defaults, while unsupported existing custom callers get D6 compatibility.
Rollback does not restore overwritten output files or mutate remote content.

### PM-6. Engineering phasing, estimates and work breakdown

Estimates are initial engineering judgments, not delivery commitments. A day is
one focused engineer-day. Test time is included separately; assumptions are reuse
of current helpers, no new dependency, and no cross-process resume. Registry
interop, custom-client forking and cleanup race coverage are the long poles.

| Item | Deliverable / affected components | Depends on | Implement / test / rollout days |
| --- | --- | --- | --- |
| Design #132 | This design and approval gate | None | 1 / 0.5 / 0 |
| W1 [#134](https://github.com/toddysm/regshape/issues/134) | Policy, scoped single-flight auth and pooled worker clients, including token metadata/instrumented-session hooks | Approved design | 3 / 2 / 0.5 |
| W2a [#135](https://github.com/toddysm/regshape/issues/135) | Core coordinator/full-stream foundation, worker hash/write, cancellation, immutable internal observations and telemetry bridge | W1 | 3 / 3 / 0.5 |
| W2b [#144](https://github.com/toddysm/regshape/issues/144) | Public batch/status API, policy/backend compatibility validation and export/facade contracts | W2a | 2 / 2 / 0.5 |
| W2c [#145](https://github.com/toddysm/regshape/issues/145) | Functional interleaved CLI renderer over the core progress/event contract | W2a | 2 / 2 / 0.5 |
| W3 [#136](https://github.com/toddysm/regshape/issues/136) | Ranged receiver, ordered assembly, safe reset/retry and standalone blob CLI controls | W2a-W2c | 2 / 2 / 0.5 |
| W4 [#137](https://github.com/toddysm/regshape/issues/137) | Graph planning, parallel artifact integration/CLI and shared controls; ranges plug into the common receiver | W2a-W2c, not range-specific W3 | 3 / 2 / 0.5 |
| W5 [#138](https://github.com/toddysm/regshape/issues/138) | Fault/compatibility validation, numeric benchmarks/profiling, real-registry matrix and CI selectors | W3, W4 | 2 / 3 / 0.5 |
| W6 [#139](https://github.com/toddysm/regshape/issues/139) | Final user/API/CLI documentation and capability close-out | W1-W5 | 1 / 0.5 / 0.5 |

These are native sub-issues of the feature and all target milestone 0.2.
The former W2 estimate did not cover its expanded API/renderer scope adequately.
W2a/W2b/W2c now mean three separate owned implementation PRs, not one oversized
PR: seven implementation, seven validation and 1.5 rollout engineer-days in
aggregate. Estimates are initial judgments, not elapsed delivery promises;
re-estimate after interface and compatibility spikes instead of hiding overruns.
W2b and W2c can proceed independently on the core event contract after W2a.

W3/W4 proceed independently once the common API/renderer foundation is ready;
W4 must work with full-stream jobs before range-specific W3. Both R1 and R2
still gate proposed 0.2 scope; an R1-only release needs an explicit owner replan.
#145 owns baseline renderer behavior, #144 the public facades, and #135 the core.
#141 consumes #145 for separate visual formatting and does not block it.
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
| S6 / throttling/replay | Default pooled 429 retries succeed within budget; explicit zero disables,
legacy keeps its old policy, and explicit status overrides remain authoritative; cancellation-aware delay, no nested budgets. Interrupted full GET after a committed prefix never replays/appends, also verify-only; prefix range fallback resets sink/hash/progress/attempt before a clean GET. |
| Concurrency/resources | Peak requests <= C including auth/probes/redirects; per-blob <= W; pending futures/spools/queue under bounds; stalled earliest range cannot grow scratch without limit. |
| Auth/pooling | Same-scope root/manifests/blobs/ranges share one valid token negotiation; forced expiry/rejection refreshes once per generation; origin/scope separation and foreign 401 protections; persistent connection reuse and no peer pool closure or instrumentation bypass. |
| Ownership | Observer throws with active writes, full queue/cancellation, disk/short-write/close errors and replaced paths; join before cleanup. Digest mismatch deletes this invocation's invalid output even after overwrite; other partial-output semantics remain, and replacements are not deleted. |
| Slow trickle | Signal cancellation while an inactivity-safe trickling read remains blocked, assert no new scheduling/unsafe cleanup, release handler and then assert join/cleanup; no bounded whole-read cancellation claim. |
| Renderer/output | Interleaved rows/chunks/attempt resets never raise the old concurrency RuntimeError; final success is verification-gated; bounded fallback notices/count summary, exact stderr/exit table, no-progress/JSON/NO_COLOR and #141 integration contract. |
| Compatibility | Pooled sequential content/output equivalence and default C=4; omitted-option unsupported custom get_blob/pull_artifact calls stay legacy with one warning, while explicit pooled/batch/status requests fail before effects. Validate legacy/CLI conflicts and unchanged result/output shapes. |
| Legacy escape | Force the new provider/pool/coordinator constructor to fail and prove explicit legacy singular/artifact root/manifest/blob downloads never invoke it; retain original custom middleware/retry/last_response/progress behavior. New batch/status/override combinations fail before effects. |
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
- [ ] Deliver W2a/W2b/W2c in three separate PRs and re-estimate from interface evidence.
- [ ] Complete dependency-ordered delivery with required checks/reviews.
- [ ] Meet section 8 numeric performance/resource gates and review CPU/I/O profiles.
- [ ] Record real-registry plus storage-redirect interoperability evidence or approved exception.
- [ ] Deliver W6 docs for default migration, SDK batch/status APIs, both commands,
      retry defaults/disable controls, custom-client compatibility, true legacy
      transport escape, scratch placement/cost, fallback and troubleshooting.
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

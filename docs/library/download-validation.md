# Download compatibility and release validation

<!-- doc-type: how-to -->

This maintainer workflow supports [#138](https://github.com/toddysm/regshape/issues/138)
and the [revision-4 design](../../specs/operations/parallel-downloads.md).
It separates deterministic CI correctness, measured performance and actual
registry interoperability. Passing one does not certify the other two.
It does not authorize a release, cloud provisioning or use of private endpoints.

## Run correctness and compatibility checks

Use an editable development installation with the dependencies declared in
[pyproject.toml](../../pyproject.toml). From the repository root:

```bash
python -m pytest -q src/regshape/tests/test_download_validation.py
python -m pytest -q
```

The [Tests workflow](../../.github/workflows/tests.yml) includes the new module
explicitly in the legacy Requests/urllib3, minimum-Click and native macOS/Windows
jobs as well as automatic Python 3.10/3.11/3.12 collection. No real-registry
credentials, public endpoint, benchmark duration assertion or provisioning is
part of unit CI. Native Windows artifact guards remain unchanged; standalone
blob stress tests are portable.

### Acceptance evidence map

The existing focused tests remain authoritative; the new stress matrix composes
their boundaries rather than duplicating every fault injection.

| Contract | Executable evidence |
| --- | --- |
| Public SDK imports in either order, independent of CLI imports | Fresh-interpreter tests in [cross-mode validation](../../src/regshape/tests/test_download_validation.py) |
| Sequential, object-parallel, single-blob range-parallel and combined modes | Three repetitions per mode/capability in [cross-mode validation](../../src/regshape/tests/test_download_validation.py), using real [loopback HTTP/1.1 origins](../../src/regshape/tests/download_fixture.py) |
| One C cap including initial rejection, separate `/v2/` probe, token and cross-origin storage redirects | An independent server counter bounds all handler kinds, not just SDK stats; barriers hold each probe/token/redirect hop alongside three data bodies while a fifth logical request verifies limiter saturation. Cross-mode physical counts agree with public stats; caller-thread completion, pending jobs, sessions, scratch and output bytes are checked |
| First-range reuse, exact one-byte tail, ignored-range response reuse and missing-validator fallback | Cross-mode request-interval assertions plus [range tests](../../src/regshape/tests/test_blob_ranges.py) |
| Bounded pending jobs, W window, C*R scratch, 4C queue, 64 KiB records, slow consumers and cancellation | Cross-mode post-join checks and barrier/event-driven [coordinator tests](../../src/regshape/tests/test_download_coordinator.py); the range committed-window test independently blocks the earliest part |
| Full-stream retries only before commitment, including verify-only; no retry after hash/write; overwritten digest-mismatch output deletion and replacement preservation | [Worker tests](../../src/regshape/tests/test_download_workers.py) |
| Slow-trickle cancellation recorded before read release; no unsafe early cleanup or promised whole-read deadline | `test_slow_trickle_cancel_does_not_clean_before_read_returns` in [worker tests](../../src/regshape/tests/test_download_workers.py) |
| Changed valid 206/late 200 after committed prefix; same-content recovery, wrong-full-content rejection, verify-only reset, exactly one zero-retry fallback | [Range fault matrix](../../src/regshape/tests/test_blob_ranges.py) |
| Shared token generations/expiry/rejection, persistent pools, origin isolation and single close | [Transport tests](../../src/regshape/tests/test_download_transport.py), plus cross-mode storage credential stripping and connection reuse |
| Public batch/status order, partial side effects, callback identity, default three retries, zero disable, explicit-policy precedence | [Batch tests](../../src/regshape/tests/test_blob_batch.py) |
| Omitted-option custom-client compatibility; explicit pooled rejection; legacy avoids failing provider/pool/coordinator constructors | [Batch legacy tests](../../src/regshape/tests/test_blob_batch.py) |
| Complete graph/dedup/limits and no publication before final verification/cleanup | [Artifact integration tests](../../src/regshape/tests/test_artifact_download.py), including physical directory/gzip-tar acquisition |
| Interleaved attempts, verification-gated rows, bounded notices and rendering suppression | [Renderer tests](../../src/regshape/tests/test_progress_display.py) |
| Exactly one post-result/error, post-cleanup normal-verbosity ETag warning; unchanged JSON, NO_COLOR/no-progress/non-TTY and failure/cancellation behavior | [Blob CLI tests](../../src/regshape/tests/test_blob_cli.py) and real artifact fallback tests in [artifact integration](../../src/regshape/tests/test_artifact_download.py) |
| Numeric release decisions reject insufficient samples and distinguish median improvement from p95 regression | Gate and real profiler smoke tests in [cross-mode validation](../../src/regshape/tests/test_download_validation.py) |

The SDK-first compatibility test exposed an eager artifact-acquisition import
cycle. [Artifact orchestration](../../src/regshape/libs/artifacts/operations.py)
now imports that acquisition wiring only when entering its pooled parallel path.
Public exports and operation behavior are unchanged.

The independent server peak bounds requests through their final write, not
through the SDK's response-close event. Missing-ETag range negotiation discards
a response body; its server handler may still be unwinding while the client
starts fallback. That matrix case therefore checks the SDK cap and physical
counts, but does not equate lingering server handlers with live client leases.
The dedicated oversubscribed barrier tests independently verify limiter
saturation for every probe/token/redirect path without discarded data bodies.

## Reproduce the performance measurements

Run on an otherwise quiet host with enough temporary storage for a 128 MiB
output and bounded spools. Avoid running local tests concurrently. This uses
only loopback sockets and caller-owned temporary files:

```bash
python -m regshape.tests.benchmark_downloads \
  --samples 20 --warmups 1 --output /path/to/evidence/download-benchmark.json
```

Replace the example output path with a unique filename in an existing,
caller-owned evidence directory outside the checkout. Retain each raw report
as a separately named ZIP attachment on
[#138](https://github.com/toddysm/regshape/issues/138), or as an approved release
asset, rather than committing generated reports. Keep the summary, provenance,
attachment link and checksum in this runbook.

The [benchmark harness](../../src/regshape/tests/benchmark_downloads.py) requires
at least 20 measured samples and at least one unmeasured warm-up per mode.
It alternates forward/reverse mode order each measured round. Payload creation,
independent final digest checks, final-output unlink and server startup/shutdown
are outside the timed interval. Operation construction, authentication/pool
setup, download verification, worker draining and operation-owned cleanup are
inside it.

The pooled comparisons share anonymous authentication, three retries, 8 MiB
ranges, buffered saved output and a fresh operation-owned pool per invocation.
Warm-up warms OS caches, not a pool retained across operations. Requests within
an operation can reuse persistent HTTP/1.1 connections. Legacy has separate
end-to-end cases: many blobs use a loop of singular legacy calls because no
legacy batch API exists. Its original transport/pooling behavior is retained;
no public pooled worker metrics are fabricated for it.
The 1 MiB entities are below the 32 MiB range-eligibility threshold with 8 MiB
ranges, so their combined policy deliberately exercises ordinary object
downloads, not smaller unapproved range fixtures.

### Fixtures and gates

| Fixture | Compared policy | Release decision |
| --- | --- | --- |
| 32 distinct 1 MiB blobs | Pooled C=1/W=1 vs C=4/W=1 | Median speedup >=1.5x |
| Single 128 MiB blob with strong ETag/ranges | Pooled C=1/W=1 vs C=4/W=2 | Median speedup >=1.5x |
| Single 64 KiB blob | Pooled C=1/W=1 vs default C=4/W=1 | Candidate p95 / baseline p95 <=1.20 |
| Single 128 MiB blob without ETag, then ignored ranges | Pooled full vs C=4/W=2 fallback | Candidate p95 / baseline p95 <=1.20 for each |
| Four distinct 32 MiB blobs | Sequential vs combined C=4/W=2 | Observational; no invented numeric gate |
| Unshaped many-small, single-large and combined fixtures | Same corresponding policies, no artificial latency or bandwidth limit | Expose coordinator/CPU/storage ceilings, including regressions |
| Separate legacy many-small and single-large | Pooled sequential vs original serial transport | End-to-end observation, not an isolated concurrency improvement |

Shaped data connections use a 25 ms delay before headers and application-level
32 MiB/s pacing of 64 KiB writes. There is no shared aggregate limiter; the
fixture must support at least four such streams (128 MiB/s aggregate).
This is not packet-level RTT/loss emulation. Both origins use TCP_NODELAY.
There is no TLS, compression, external registry, authenticated cloud backend
or remote network. These results cannot establish production speedups.

Medians use the conventional sample median; p95 uses nearest rank (rank 19
of 20). Wall-clock comparisons use like fixtures and the same pooling/retry
policy. Gate failure makes the harness exit 1 **after retaining all results**;
it requires optimization or explicit owner-approved gate/scope changes.
Before the first fixture the output is overwritten with `complete: false`, so
even a failed rerun cannot retain a previous successful report. Runtime/protocol
errors propagate and leave that incomplete checkpoint; a partially written
report is not complete evidence.

### Read the evidence

The [recorded raw run ZIP](https://github.com/user-attachments/files/35053758/download-benchmark-2026-11-18.zip),
[attached to #138](https://github.com/toddysm/regshape/issues/138#issuecomment-6991708597),
retains environment, source commit, each measured sample, p50/p95, comparisons
and gate outcomes. Its contained JSON is byte-identical to the original report;
the generated data is not part of the source tree. Download and unpack the ZIP,
then check `complete` before interpreting `numeric_gates_passed`.

SHA-256 checksums:

```text
f2bbb8d979b45b6dc3ad0681c49f96881d2f01dd7c590eb50c0e28b53393f681  download-benchmark-2026-11-18.zip
8356ee2a3bcd96ec16b7502ce8772e854b25da0d53329e502e4ac5600afbd71e  download-benchmark-2026-11-18.json
```

Each sample records:

- Physical requests, consumed entity bytes, accepted bytes, retries,
  active-request peak, scratch-reservation peak and data connection count.
  Server body bytes sent are separate: an abandoned capability response may
  send bytes that the client never consumes.
- Process CPU, caller-thread CPU, summed actual worker-thread CPU/wall time,
  receive/hash/write durations and producer-to-coordinator queue waits.
  Summed work/wait can exceed wall time. Process CPU also includes the local
  HTTP servers; caller CPU includes ordered range assembly.
- Actual buffered spool read/write bytes and elapsed time, plus total ordered
  range assembly time. These are logical filesystem operations, **not device
  throughput**, fsync latency or durable persistence. Warm caches and final
  output buffering are intentional and disclosed.
- Observed pending-job peak and final verification/resource checks. The
  public scratch metric measures reserved spool capacity, not artifact staging
  or a filesystem-wide disk-usage estimate.

The run used a shared developer Mac with 32 GiB RAM and 10 physical/logical CPUs,
without CPU affinity, load isolation or frequency locks. The raw report records
the exact OS, Python and Requests/urllib3/Click versions. Temporary storage used
the local macOS data volume (about 448 GiB free before the run); writes were
buffered with no fsync. Other users/processes may affect tail measurements.

### Recorded result: performance gate not met

The run completed on 2026-11-18 with 20 measured samples and one warm-up per mode:
11 fixtures, 24 mode/fixture pairs and 480 measured operations. It exited **1**
with `complete: true` and `numeric_gates_passed: false`; this is completed
measurement evidence, **not release approval**.

The measured source is the clean commit
`79bfd5c1129257379f64d5e6d591d0e8462722ef`, not the later documentation and
review-fix commits. Later fixture fixes add independent request observations,
safe redirect encoding and Windows disconnect handling; they are not included
in these measurements. Any optimization needs a new, separately retained run.
The original harness also gated the observational many-small combined mode
(which passed); the corrected harness gates only the approved object mode.
It now waits for final server counter updates outside the timed interval;
the original report's sent-byte fields were sampled without that synchronization.
Neither correction changes the recorded ranged timing failure.

| Shaped comparison | Baseline p50 / candidate p50 (s) | Decision |
| --- | --- | --- |
| Many-small object parallel | 2.463763 / 0.580154 | **Pass**, 4.2467x median speedup (required >=1.5x) |
| Single-large ranged | 4.055621 / 2.854269 | **Fail**, 1.4209x median speedup (required >=1.5x) |
| Four-blob combined | 4.186905 / 1.355772 | Observation only, 3.0882x median speedup |
| Small/default | 0.060273 / 0.060254 | **Pass**, p95 ratio 0.9552 (limit <=1.20) |
| Missing-ETag fallback | 4.056825 / 4.097276 | **Pass**, p95 ratio 1.0115 (limit <=1.20) |
| Ignored-range reuse | 4.056291 / 4.058956 | **Pass**, p95 ratio 1.0071 (limit <=1.20) |

The single-large ranged samples used exactly 16 requests, two data connections
and 16 MiB peak scratch reservation. Median summed worker wall time was
4.498094 s and caller CPU 0.174258 s. Median buffered spool writes took
0.180920 s; ordered assembly took 0.216467 s, including 0.022922 s of spool
reads. These overlapping/summed measurements are not additive wall-clock
phases and do not establish a unique bottleneck. Negotiation, W=2
committed-window scheduling and ordered assembly remain costs to investigate;
no protocol, integrity, ownership or resource-bound checks were relaxed.

Unshaped loopback exposes the trade-off: single-large ranged p50 was
0.467349 s versus 0.262568 s sequential (0.5618x speedup; p95 ratio 1.7473).
Unshaped combined p50 was 0.491014 s versus 0.435071 s sequential. These have
no numeric release gate, but are retained regressions, not omitted outliers.
The separate shaped legacy many-small loop had p50 2.051571 s versus pooled
sequential 2.440152 s; single-large legacy was 4.032591 s versus 4.055862 s.
Do not conflate those end-to-end serial baselines with concurrency speedups.

**Next performance gate:** optimize the measured ranged path or obtain an
explicit owner-approved gate/scope change, then rerun the unchanged fixtures,
sample minimums and numeric thresholds. The current report does not satisfy
the ranged performance acceptance criterion.

## Complete the actual-registry release gate

**Partially exercised: eight Harbor download trials passed; the second
implementation, full deployment pinning and storage-redirect evidence remain
outstanding.** No exception is approved. Synthetic redirect coverage is not a
substitute. Neither the passing CI suite nor a passing benchmark report makes
this gate complete.

| Required deployment | Pinned implementation/configuration | Trial status |
| --- | --- | --- |
| First actual deployment: Harbor | Reports v2.17.0-e7dbdea2, db_auth and filesystem storage; exact deployed image digests/full backend configuration still require operator confirmation | Eight CLI/SDK trials passed on 2026-11-18 |
| Different actual implementation, for example Zot | Not supplied; obtain exact version/image digest and backend configuration | Not run |
| Authorized storage-redirect deployment | Not supplied; obtain registry/backend versions and approved storage origin | Not run; release blocked |

### Recorded Harbor trial

The [sanitized trial checkpoint on #138](https://github.com/toddysm/regshape/issues/138#issuecomment-6993559246)
records source `250319b252a3784927f37a3cc56e30522ba40662`, fixture identities,
per-operation timings/counts, authorization and limitations. The operator
authorized synthetic uploads and a new private `regshape-validation-6aaba73b`
project permitting unsigned test fixtures. The existing `library` project's
cosign requirement was preserved and checked again after testing.

The fixture has 32 distinct 1 MiB blobs, one 128 MiB blob, an OCI empty config
and an OCI artifact manifest: 35 unique objects / 167,777,232 stored bytes.
It is opaque synthetic data, not an executable application image. The test
repository/tag is `regshape-validation-6aaba73b/fixture:compat-20261118`.
Downloads were pinned to root
`sha256:808f476ab8c94b2d8a52e599c690f6bcaa007d0b6d4942b5426dce2d7372b06f`.

Both `artifact pull` and `blob get` passed in default, sequential,
`--concurrency 4 --ranges-per-blob 2 --range-size 8388608` and explicit legacy
modes. Each used a distinct temporary output. All output digests/sizes and the
exact OCI layout object set were independently verified.

- Both ranged operations received sixteen valid 206 parts with strong ETags;
  range zero was fetched once and subsequent parts used `If-Range`.
- Pooled artifact pulls reused four data connections, or one when sequential;
  standalone ranged receipt used two. SDK active-request peaks respected the
  selected cap; ranged scratch peaked at 16 MiB. These peaks are client SDK
  metrics, not independently instrumented server measurements.
- No retries, capability fallbacks or redirects occurred. Token expiry/refresh,
  ETag rotation and protocol faults were not induced on this real registry.
- Cumulative consumed response bodies, including setup diagnostics and the
  final policy audit, were 1,208,194,362 bytes (1.1252 GiB), below the authorized
  2 GiB cap. This does not measure total wire traffic or server egress.
- CLI login used an isolated temporary credential store. Credentials and local
  download outputs were not published; temporary stores/outputs were removed.
  The remote synthetic fixtures remain available for reproduction.

The sanitized raw report remains outside the source tree, with SHA-256
`7af70a77920aba082cc2c84bb6a29b9ae75dd0682fa9fb6705ca0ac0f829190a`.
Its checksum and detailed observations are retained in the issue checkpoint.
These single unshaped-localhost trials are interoperability evidence only;
ranged receipt was slower here and does not supersede the failed controlled
performance run. This filesystem-backed deployment does not exercise storage
redirects or prove cross-origin credential stripping.

### Trial procedure

Before a trial:

1. Obtain owner authorization for each existing endpoint, read-only repository,
   expected artifact/blob digests and stored sizes, and permissible request/egress
   volume. Pin server versions/image digests and configuration. Do not provision
   cloud resources, change registry settings or upload fixtures without separate
   approval.
2. Authenticate through the existing credential-store workflow. Do not put
   passwords, bearer tokens, signed redirect URLs, Authorization headers or raw
   debug traffic in reports, issues or PRs.
3. Run **both** artifact pull and blob get with defaults, `--sequential` and
   `--concurrency 4 --ranges-per-blob 2 --range-size 8388608`, using distinct output
   paths. Also record explicit legacy rollback behavior separately. Use the
   [CLI/API contract](downloads.md) and [artifact reference](../../specs/cli/artifact.md).
4. Record usable strong-ETag/range behavior, first-range reuse or explicit
   fallback reasons, authentication/refresh behavior, persistent-connection
   observations, total requests, stored/received bytes and exact digest/size/layout
   verification. Record the approved redirect origin without its signed path or
   query, and confirm registry credentials are stripped on cross-origin hops.
5. Record timing and environment, distinguishing client-consumed bytes from
   server/egress measurements. Obtain counts from safe server-side instrumentation
   when they cannot be observed by the SDK. Mark unavailable measurements
   explicitly; never infer capability from `Accept-Ranges` alone.
6. Sanitize and rescan evidence before publishing it. Unsupported ranges with
   verified full-stream fallback are a capability result, not an interop failure.
   Missing access, missing integrity evidence or unexplained protocol failures
   leave the release gate open. Any exception requires explicit owner approval.

Final feature documentation remains the separate
[#139](https://github.com/toddysm/regshape/issues/139) work item. This validation
record does not mark the parent capability Implemented or publish version 0.2.

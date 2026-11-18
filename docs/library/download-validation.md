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
| One C cap including initial rejection, separate `/v2/` probe, token and cross-origin storage redirects | Cross-mode physical request counts agree with public stats; caller-thread completion, pending jobs, sessions, scratch and output bytes are checked |
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

## Reproduce the performance measurements

Run on an otherwise quiet host with enough temporary storage for a 128 MiB
output and bounded spools. Avoid running local tests concurrently. This uses
only loopback sockets and caller-owned temporary files:

```bash
python -m regshape.tests.benchmark_downloads \
  --samples 20 --warmups 1 --output download-benchmark.json
```

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
Runtime/protocol errors propagate and leave `complete: false` in any prior
checkpoint; a partially written report is not complete evidence.

### Read the evidence

The [recorded raw run](download-benchmark-2026-11-18.json) retains environment,
source commit, each measured sample, p50/p95, comparisons and gate outcomes.
Check `complete` before interpreting `numeric_gates_passed`.
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

## Complete the actual-registry release gate

**Blocked: no authorized registry/storage-redirect targets were supplied, and
no actual-registry trial or owner-approved exception is recorded.**
Synthetic redirect coverage is not a substitute. Neither the passing CI suite
nor a passing benchmark report makes this gate complete.

| Required deployment | Pinned implementation/configuration | Trial status |
| --- | --- | --- |
| First actual implementation, for example Distribution | Not supplied; obtain exact version/image digest and backend configuration | Not run |
| Different actual implementation, for example Zot | Not supplied; obtain exact version/image digest and backend configuration | Not run |
| Authorized storage-redirect deployment | Not supplied; obtain registry/backend versions and approved storage origin | Not run; release blocked |

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

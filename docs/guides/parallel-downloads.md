# Tune parallel artifact and blob downloads

<!-- doc-type: how-to -->

Use this guide to overlap independent artifact blobs, opt into parallel HTTP
byte ranges for large blobs, and recover from throttling without weakening
verification. These are development-build capabilities targeting **0.2**, not a
release announcement. Check `regshape artifact pull --help` and
`regshape blob get --help` in your installation.

## Before you start

- Authenticate separately with [`regshape auth login`](../../specs/cli/auth.md).
  Other commands use stored credentials; do not embed credentials in references.
- Substitute an authorized registry/repository and a real blob digest from its
  manifest. Digests must be complete lowercase SHA-256 or SHA-512 addresses.
  The shell commands below are templates, not a public download service.
- For complete artifacts, satisfy the
  [Linux/macOS, filesystem and private-namespace prerequisites](artifact-pull.md#before-you-start-supported-storage-and-trust)
  and choose a **new** output under an existing safe parent. Parallel downloads
  do not enable Windows artifact publication, resume or overwrite.
- For standalone blobs, use caller-trusted output paths. Ordinary saved blob
  downloads can overwrite files and retain partial files on failure.
- Allow space for both saved content and temporary range files. Do not remove
  another invocation's files or expose diagnostic logs containing credentials
  or signed storage URLs.

## 1. Start with the pooled defaults

```bash
ARTIFACT='registry.example.com/team/package:stable'
REPO='registry.example.com/team/package'
# Replace with a real config/layer digest from the selected manifest.
DIGEST='sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03'

regshape artifact pull -a "$ARTIFACT" -o ./package-layout
regshape blob get --repo "$REPO" --digest "$DIGEST" --output ./layer.blob
```

Default **C=4** means at most four simultaneous physical HTTP requests in this
invocation, including authentication and redirects. Default **W=1** means one
ordinary full GET per blob, not four ranges within that blob. Artifact documents
are discovered serially; with C>1 the complete graph and unique-byte budgets are
reserved before independent blobs overlap. Shared config/layer addresses are
downloaded once. All platforms and explicit subjects are still included.

The cap is **per invocation**, not per machine, process, registry or account.
Two concurrent commands or two independently launched SDK calls have separate
caps. Use one SDK batch when several blobs must share a cap.

Pooled downloads default to **three retries per request** beyond its initial
attempt, including 429 and transient 500/502/503/504 responses. This is not a
three-retry total for an artifact, nor a budget multiplied by the number of
workers for one request. Increasing concurrency can cause more registry
throttling than earlier serial downloads.

## 2. Opt into ranges when useful

```bash
regshape artifact pull -a "$ARTIFACT" -o ./ranged-layout \
  --concurrency 4 --ranges-per-blob 2 --range-size 8388608

regshape blob get --repo "$REPO" --digest "$DIGEST" --output ./ranged.blob \
  --concurrency 4 --ranges-per-blob 2 --range-size 8388608
```

Both use **one C=4 cap**, not four objects times two ranges. A blob may hold at
most W=2 consecutive uncommitted parts. Ranges become eligible at **four times
the range size**: 32 MiB for the default 8 MiB range. Smaller known blobs use a
full GET. Without trusted size metadata, the first real range discovers size;
there is no required HEAD or separate one-byte capability probe.

| Control | Default | Meaning |
| --- | --- | --- |
| `--concurrency` / `DownloadOptions.concurrency` | `4` | Global request ceiling, integer 1..32 |
| `--ranges-per-blob` / `ranges_per_blob` | `1` | Per-blob ceiling, integer 1..C; 1 disables ranges |
| `--range-size` / `range_size` | `8388608` | Maximum range/spool bytes, 65536..67108864 |
| `--download-retries` / `max_retries` | Resolved `3` | Retry override, integer 0..10; SDK `None` resolves policy |
| `--scratch-dir` / `scratch_dir` | Operation-specific | Existing caller-trusted scratch parent |

`blob get --chunk-size` and SDK `chunk_size` instead control the **streaming I/O
buffer**, default 65536 bytes. Pooled reads are capped at 64 KiB even if a larger
buffer is requested. This is neither an HTTP range size nor a promise of faster
downloads. Artifact pull uses bounded internal buffers and has no `--chunk-size`.

Ranged GETs are unrelated to OCI **chunked uploads**. Upload chunks are sent
in order through POST/PATCH/final PUT; download ranges are independent GETs
assembled and hashed in original byte order. No multipart download response is
required or accepted. Identity HTTP encoding preserves range offsets; an OCI
gzip/zstd-compressed layer is still downloaded as its original stored bytes,
not unpacked.

### Reserve scratch space and account for extra I/O

Let **R** be range size. Outstanding range-spool capacity is bounded by
**C*R across the invocation**, including completed parts waiting for an earlier
part. C=4 and R=8 MiB reserve at most 32 MiB of range capacity, not 32 MiB per
blob. Full streams do not spool whole blobs. This bound excludes saved outputs,
artifact staging/archives, metadata, open response buffers and filesystem
overhead; it is not a total memory or disk-use bound.

Standalone ranges default to the system temporary directory. Artifact ranges
default to the trusted private staging namespace. `--scratch-dir /trusted/path`
or SDK `scratch_dir` selects an existing caller-trusted root instead; the
operation creates/removes its own private range files there. Directory existence
does not establish trust. Temporary files can contain sensitive blob data.

Ranges add a spool write and ordered spool read before final output writing;
verification-only still incurs that scratch I/O. A tmpfs or memory-backed
scratch root consumes **memory**, and a disk-backed root can consume page cache.
Allow headroom, reduce C/R, or disable ranges if storage/memory pressure dominates.
For artifact output, also budget the separate
[staging/archive costs](artifact-pull.md#before-you-start-supported-storage-and-trust).

## 3. Distinguish serial scheduling from transport rollback

```bash
# Serialize on the modern pooled transport; retain its retries and observations.
regshape artifact pull -a "$ARTIFACT" -o ./serial-layout --sequential
regshape blob get --repo "$REPO" --digest "$DIGEST" --sequential

# Genuine compatibility rollback to the original borrowed-client transport.
regshape artifact pull -a "$ARTIFACT" -o ./legacy-layout --legacy-transport
regshape blob get --repo "$REPO" --digest "$DIGEST" --legacy-transport
```

`--sequential` selects pooled C=W=1. It conflicts with explicitly supplied
`--concurrency` or `--ranges-per-blob`, even matching values. To use one slot
with a retry override, combine `--sequential --download-retries N`.

`--legacy-transport` bypasses the new pool, shared authentication provider,
coordinator and retry resolver. It retains the original client's transport,
middleware, retry and `last_response` behavior. Do not combine it with explicit
sequential/concurrency/range-count/retry/scratch controls. `--range-size` remains
validated but unused. Legacy is not a way to request batch/status APIs or ranged
downloads, and its retry budget is not the pooled default.
It also retains weaker legacy unbounded-output cleanup: digest mismatch unlinks
the pathname without checking file identity, and deletion errors are only logged.
Transport rollback does not add pooled replacement-file protection.

In the SDK, use `DownloadOptions.sequential()` versus
`DownloadOptions.legacy()`. Existing custom singular/artifact clients without a
safe `fork_for_download` override retain their original serial path with one
logged notice **only when options and the new status observer are omitted**.
Explicit unsupported pooled requests, batch calls and status observers fail
before acquisition instead of silently dropping customization.

## 4. Handle capability fallback and retries

Range support and a strong ETag are optional registry/storage capabilities, not
OCI requirements. Missing `Accept-Ranges` alone does not prevent negotiation.
The first valid 206 supplies numeric `Content-Range` size and a strong ETag;
subsequent requests bind that validator with `If-Range`. A valid first-range 200
is reused as the full entity, without another GET.

Unknown complete size, an absent/weak initial ETag, first-response HTTP coding
or correctly declared limited ranges can select **one full-stream fallback per
blob**. This strategy change is separate from transport retries and can occur
even with `--download-retries 0`. It cannot recursively restart. Malformed bounds,
contradictory sizes, truncated/extra bodies and digest mismatches fail rather
than becoming capability fallback.

### An ETag changed: check the final outcome

A changed established ETag can also trigger the one-time full-stream fallback.
ETag rotation alone is **not corruption**. Old jobs stop/drain/join before their
scratch is discarded and the retained output is identity-checked, truncated and
rewound. The hasher and accepted-byte count reset on the **same transfer ID**
with a new logical attempt. Unrelated batch blobs are not replayed.

Only a clean full download matching the original digest and size can succeed.
Received bytes remain cumulative, so discarded ranges/fallback can increase
bandwidth and `bytes_received`. If the sink cannot be safely reset, or fallback
verification fails, the operation fails with the normal partial-output rules.

At normal verbosity, the CLI emits one plain stderr warning **after cleanup and
the flushed success result**, or after error/cancellation reporting. For a blob,
`<digest>` below is the complete requested digest:

```text
Warning: ETag changed during ranged download of <digest>; restarted as a full-stream download and verified the digest.
```

On failure, wording does not assert verified recovery:

```text
Warning: ETag changed during ranged download; a full-stream fallback was attempted, but the operation did not complete.
```

Artifact pull emits one warning with the **unique affected blob count**, not
one per reference: `Warning: ETag changed for 2 blobs; restarted them as
full-stream downloads and verified their digests.` On failure it says
`full-stream fallbacks were attempted, but the operation did not complete.`
Warnings require no verbose/telemetry flag and survive `--no-progress`,
artifact `--json`, nonterminal stderr and non-empty `NO_COLOR`. A failed
diagnostic sink is not reused. JSON fields/stdout and failure exit status are
unchanged. SDK callers observe the fixed reason `etag_changed`; the library
does not print this CLI warning.

### Recover from 429 without disabling verification

```bash
regshape artifact pull -a "$ARTIFACT" -o ./retry-layout \
  --sequential --download-retries 5
regshape blob get --repo "$REPO" --digest "$DIGEST" \
  --concurrency 2 --ranges-per-blob 1 --download-retries 5

# Deliberately disable pooled transport retries, not integrity checks.
regshape blob get --repo "$REPO" --digest "$DIGEST" --download-retries 0
```

`blob get` adds a throttling diagnostic identifying `--download-retries`,
`--concurrency/--ranges-per-blob` and `--sequential`. `artifact pull` reports the
underlying registry/HTTP error; use those same controls to recover. Wait for the registry's
rate-limit window, lower concurrency/range count, then deliberately choose a
budget. More retries do not grant quota or fix authorization.

429/503 `Retry-After` seconds/dates are honored together with exponential
backoff, after releasing the response slot. Delays above 300 seconds fail
explicitly rather than waiting indefinitely or retrying too early; malformed
values log a sanitized diagnostic and use normal backoff. Backoff wakes on
cancellation. One retry owner shares the request's budget across authentication
resends and any challenge probe, rather than nesting independent retry loops.

Full streams retry only **before any byte is committed to a file or hasher**,
including verify-only downloads. A broken private uncommitted range may retry
that part within the configured connection-error policy. Authentication,
integrity/protocol, sink and cleanup errors are not repaired by retry. Neither
mode resumes an earlier invocation's partial file.

## 5. Use the same controls from Python

The snippets below share this setup. Supply `digest` and `other_digest` from
real blob descriptors in `team/package`; use a trusted working directory and
new example output names. Client construction resolves stored credentials;
the operations borrow that configured client and never close it.

```python
from regshape.libs.blobs import (
    BlobDownload, DownloadOptions, DownloadStats, DownloadStatus,
    get_blob, get_blobs,
)
from regshape.libs.transport import RegistryClient, TransportConfig

client = RegistryClient(TransportConfig(registry="registry.example.com"))
policy = DownloadOptions(concurrency=4, ranges_per_blob=2, max_retries=3)
summaries: list[DownloadStats] = []

def observe(status: DownloadStatus) -> None:
    if status.event == "summary":
        summaries.append(status.stats)
```

### Singular receipt and verification-only

```python
info = get_blob(
    client, "team/package", digest, output_path="layer.blob",
    download_options=policy, download_status_callback=observe,
)
assert info.digest == digest
stats = summaries[-1]
print(stats.request_count, stats.retry_count, stats.bytes_received)

# Still downloads/hashes everything; no whole-blob retained output.
verified = get_blob(client, "team/package", digest, download_options=policy)
```

For bounded, exclusive saved output, additionally supply `max_bytes` (a positive
pre-write entity cap); `total_size` is an optional **trusted** exact size,
including zero. Do not guess it from an unrelated compressed/uncompressed size.

### Several blobs under one cap

```python
batch = get_blobs(
    client, "team/package",
    [
        BlobDownload(digest, output_path="batch-layer.blob"),
        BlobDownload(other_digest),  # Verify-only.
    ],
    download_options=policy, download_status_callback=observe,
)
assert tuple(blob.digest for blob in batch.blobs) == (digest, other_digest)
assert batch.stats is summaries[-1]
```

All entries are validated before effects; duplicate digests or aliased outputs
are rejected. Completion order may differ, but `BlobBatchResult.blobs` preserves
input order. The first observed failure stops scheduling, drains/joins peers and
raises the original exception, **without a partial result**. Earlier verified
files and unbounded partial files can remain. This is not an atomic multi-file
transaction. Use [the batch reference](../library/downloads.md#public-batch-downloads)
for entry limits and filename collision rules.

### Complete-artifact receipt

```python
from regshape.libs.artifacts import ArtifactFormat, pull_artifact

artifact = pull_artifact(
    client, "team/package", "stable", "sdk-package-layout",
    output_format=ArtifactFormat.OCI,
    download_options=policy, download_status_callback=observe,
)
print(artifact.resolved_reference, summaries[-1].active_requests_peak)
```

`reference` is a tag or digest, not a full registry URL. The new destination must
meet the native storage prerequisites. One coordinator/cap covers the whole
artifact; final local graph verification precedes packaging/publication.
`ArtifactPullResult` fields and existing progress callbacks are unchanged.

### Retry precedence and typed observations

An explicit non-boolean `DownloadOptions.max_retries` integer in 0..10 wins.
Otherwise, an enabled explicit `TransportConfig.retry_config` retains its budget;
an explicitly supplied retry configuration with `enable_retries=False` resolves
to zero. With no explicit policy, pooled retries resolve to three even though
ordinary `TransportConfig.enable_retries` defaults to false. A budget override
does **not** replace an explicit policy's status set, backoff or exception types:
include 429 in that policy if it must retry throttling. Legacy requires
`max_retries=None` and retains original client behavior.

Frozen `DownloadStatus` events expose strategy, fallback/retry reason, attempt,
HTTP status, per-blob counters and one final aggregate summary.
`DownloadStats.bytes_received` counts consumed blob bytes including discarded
attempts, not wire-exact egress. `bytes_accepted` is the current committed prefix,
not verification proof. Request counts include authentication/redirects; artifact
summary retries also include documents. Summed work durations can exceed wall
time. Callbacks run synchronously on the invoking thread, not workers. A callback
exception cancels/drains work and propagates unchanged; keep observers short and
do not use `async def`. See [all fields and fixed reasons](../library/downloads.md#status-and-statistics).

## Output, cancellation and troubleshooting

`blob get` always emits the same `digest`, `content_type`, `size` metadata JSON
on success, including verification-only. Pooled size is consumed entity bytes;
legacy metadata remains header-derived. `artifact pull --json` emits only its
existing completed-result record. Progress and warnings use stderr.
`--no-progress` disables animation, **not** transfers, status collection, warnings
or verification. JSON telemetry and nonterminal stderr also suppress animation;
redirecting stdout alone does not.

Ordinary standalone output overwrites/truncates the target and can leave
unverified partial bytes on transport/disk/callback failure or cancellation.
On pooled transport, digest mismatch deletes the invalid output filename,
**even if it overwrote an existing file**, when identity checks permit; it does not restore original
contents. For a caller-owned symlink, only the link is removed and its target
can retain invalid bytes. Bounded SDK outputs are exclusive and removed on
failure by retained identity. Legacy unbounded receipt instead unlinks the
output pathname **without identity checks** on mismatch and only logs deletion
errors; it does not protect a replacement file. Neither transport restores
overwritten contents. Never treat file existence as verification.

Artifact failures before publication clean owned incomplete storage when safe,
with no success result. Complete published output remains if later diagnostics
or result writing fails. Publication is neither whole-directory atomic visibility
nor `fsync`-backed crash durability; retain the
[artifact failure boundaries](artifact-pull.md#failure-recovery-and-migration).

| Symptom | Check and recovery |
| --- | --- |
| 429 after retries | Wait for quota; reduce C/W or use pooled `--sequential`; deliberately adjust `--download-retries`. Inspect `retry_count` and retry statuses, not a guessed whole-artifact retry budget. |
| Only full GETs / missing range metadata | W=1, a small blob, weak/missing ETag or valid capability fallback is normal. Inspect status reasons such as `small_blob`, `no_strong_validator`, `unknown_size`, `range_ignored`. Do not require the registry to advertise ranges. |
| ETag warning | Check exit status and final verification outcome. Rotation alone is not corruption; only a verified clean fallback succeeds. Account for extra received bytes. |
| Corruption, invalid range metadata or digest mismatch | Check the selected digest/source and proxy/storage behavior; retry explicitly after correction. Do not bypass digest verification or publish partial content. |
| Disk pressure / memory-backed scratch exhaustion | Budget saved output, artifact staging/archive and C*R separately. Use an existing trusted disk scratch root, reduce C/R or set W=1. tmpfs is memory, not free disk. |
| Ctrl-C appears slow | Scheduling stops and backoff wakes, but active reads must finish before safe cleanup/join. Finite Requests timeouts limit socket inactivity, not total elapsed read time; a continuously trickling peer can cause **unbounded cancellation-to-join latency**. Do not delete scratch while workers still own it. |
| Unsupported custom client or legacy/status conflict | Implement the safe fork extension or explicitly choose the old singular/artifact legacy path without the new status observer. Sequential pooled transport is not rollback. |
| No progress / no success output | Check stderr/JSON suppression and exit status. Usage errors exit 2; runtime failure/cancellation is nonzero (normally 1). Inspect only your own partial output before retrying; artifacts need a new destination. |

Performance is workload-, registry-, host- and storage-dependent. The
[recorded validation](../library/download-validation.md) accepts controlled
Linux fixture results and actual-registry evidence with explicit scope limits;
the retained macOS ranged-performance gate failed. Neither result promises a
speedup on this machine or certifies every registry/backend.

## Related references

- [Blob command](../../specs/cli/blob.md#blob-get) and [blob API](../../specs/operations/blobs.md#get_blob)
- [Artifact command](../../specs/cli/artifact.md) and [artifact API](../../specs/operations/artifacts.md)
- [Policy, custom-client extension, batch and status reference](../library/downloads.md)
- [Interleaved progress, colors and narrow terminals](live-transfer-progress.md#read-aligned-download-rows)

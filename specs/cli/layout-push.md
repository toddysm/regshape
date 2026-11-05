# CLI: `layout push`

## Overview

The `layout push` command pushes a complete Open Container Initiative (OCI)
layout directory or supported gzip-tar archive to a registry. Strict private
snapshotting verifies every reachable index, manifest, explicit subject,
configuration and layer before credentials or network calls. Nested indexes
retain all platforms; original document bytes and identities are never rewritten.
See the [complete artifact design](artifact-pull.md#d7-recursive-layout-push-and-existing-surfaces)
for the approved publication contract.

`layout push` is the natural next step after building a layout with the
`init → add layer → generate config → generate manifest` pipeline. It bridges
the offline layout world and the online registry world.

### Push Algorithm

1. Copy the complete closure into private verified storage and resolve all
   literal publication tags, including overridden source annotations.
2. Upload each unique configuration/layer blob once, unless HEAD returns 200.
   Only a `BlobError` carrying `status_code=404` means missing; an unclassified
   error is not permission to upload. Both upload modes support SHA-256/SHA-512.
3. Publish child documents and subjects by digest before their parents.
4. Publish requested root references last. A root also referenced by another
   document receives its digest publication early and its tag alias last.
   Deduplicate identical `(digest, reference)` operations.

The local wrapper is never published as the artifact root. Snapshot source paths
are never reopened after verification, and mutable source files are not hard-linked.

### Blob Existence Check

Before uploading each blob, the command issues a `HEAD` request to test
whether the registry already has it. This avoids redundant uploads (common
when layers are shared across images or when re-pushing after a partial
failure). The existence check can be disabled with `--force` to
unconditionally upload every blob.

---

## Usage

```
regshape layout push [OPTIONS]
```

---

## Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--path` | `-p` | path | required | Complete OCI directory or supported gzip-tar archive, with at least one top-level root |
| `--dest` | `-d` | string | required | `registry/repo` or `registry/repo:tag`; an override requires exactly one top-level root, not one descendant |
| `--force` | | flag | `false` | Skip the `HEAD` existence check and upload every blob unconditionally |
| `--chunked` | | flag | `false` | Use the chunked (streaming) upload protocol for blobs instead of monolithic |
| `--chunk-size` | | integer | `65536` | Chunk size in bytes when `--chunked` is enabled |
| `--dry-run` | | flag | `false` | Validate the layout and print what *would* be pushed without making any network calls |
| `--json` | | flag | `false` | Preserve the structured result on stdout without animated progress |
| `--no-progress` | | flag | `false` | Disable interactive progress without changing transfers or telemetry |
| `--max-manifest-bytes` | | positive integer | 16777216 | Per-document and wrapper byte cap |
| `--max-objects` | | positive integer | 100000 | Unique content addresses |
| `--max-depth` | | positive integer | 64 | Graph depth; roots are depth zero |
| `--max-total-bytes` | | positive integer | 1099511627776 | Distinct stored object bytes |
| `--max-archive-bytes` | | positive integer | 2199023255552 | Expanded archive bytes |
| `--max-archive-input-bytes` | | positive integer | 2199023255552 | Compressed archive bytes |

Root options include `--insecure`, `--verbose`, and `--log-file`.
JSON and telemetry options belong to `layout push`; use
`regshape layout push --help` for the complete command option list.

---

## Reference Resolution

The `--dest` option follows the same `registry/repo[:tag]` format used
by other regshape commands:

| `--dest` value | Registry | Repo | Tag / Reference |
|----------------|----------|------|-----------------|
| `registry.io/myrepo/myimage` | `registry.io` | `myrepo/myimage` | Taken from the manifest's `org.opencontainers.image.ref.name` annotation; if absent, falls back to the manifest digest |
| `registry.io/myrepo/myimage:v1.0` | `registry.io` | `myrepo/myimage` | `v1.0` (overrides the annotation for single-manifest layouts) |

### Multi-manifest layouts

More than one top-level root prohibits a destination tag override. A single
index root with many descendants permits it. Without an override, use each
root's ref-name tag, or its digest when untagged; never invent `latest`.

Explicit destination tags and present source ref-name annotations must fully
match ASCII `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`, even with an override.
Digest destinations, query/fragment/percent forms, controls and invalid tags
are rejected before credential resolution. Tags are not trimmed or decoded.
An authority port delimiter requires a nonempty numeric port:
`registry.example:/repo` and `[::1]:/repo` are rejected before credentials.
Bracketed Internet Protocol version 6 (IPv6) literals remain supported with
or without a port, including `[::]/repo` and `[::1]:5000/repo`.

---

## Behaviour

The command enters `LayoutPushPlan` before constructing `RegistryClient`, then
uses `execute_layout_push` while that snapshot context is still active.
The public `push_layout` convenience function uses the same plan/executor and
borrows its caller-provided client. Document PUTs supply `expected_digest`,
require `201 Created` and validate any reported digest against exact bytes.
An absent digest header is allowed after verification of the submitted payload.

Strict source operations require the Linux/Darwin native safe-filesystem backend
and a trusted private namespace. Windows fails closed. This is not isolation
from hostile processes with the same user identity or root privileges.

Errors do not delete uploaded remote content. A lost final root response reports
**outcome uncertain**: the registry may already have committed it; explicitly
verify the destination before retrying. No successful result is emitted on failure.

### Dry-run mode (`--dry-run`)

Dry-run enters the identical verified snapshot/plan and prints intended operations.
It constructs no registry client, invokes no credential helper and performs no
HEAD or other network call. It does not claim that uploads were acknowledged or
predict which blobs already exist:

```
[dry-run] Layout ./my-image -> registry.io/myrepo/myimage

[dry-run] Would upload blob sha256:aaa...bbb (1.0 MB)
[dry-run] Would upload blob sha256:ccc...ddd (312 B)
[dry-run] Would push manifest sha256:eee...fff as 'latest'
```

Digests and sizes are formatted the same way as in the normal push output.

---

## Output Format

### Plain text (default)

On an interactive standard-error terminal, the shared renderer replaces one
row per unique blob through transfer, retry, verification/commit and final
state. The `done` and `skipped` counters count unique digests. A repeated shared
blob does not append another row or downgrade an Uploaded row.

Example final screen (intermediate states overwrite these same rows):

```
Pushing layout ./my-image -> registry.io/myrepo/myimage

Preparing OCI layout ... done
Blob 1 [1 done/0 skipped] sha256:aaaa... Uploaded 1.0 MiB/1.0 MiB
Blob 2 [1 done/1 skipped] sha256:cccc... Already present; skipped
Blob 3 [2 done/1 skipped] sha256:eeee... Uploaded 312 B/312 B

Push complete: 1 manifest(s), 2 blob(s) uploaded, 1 blob(s) skipped.
```

Non-TTY output (e.g. piped to a file):

```
Pushing layout ./my-image -> registry.io/myrepo/myimage

Push complete: 1 manifest(s), 2 blob(s) uploaded, 1 blob(s) skipped.
```

Live rows use shortened digests and binary byte units (B, KiB, MiB, GiB);
optional fields are omitted on narrow terminals. The existing final result
counters and legacy callbacks retain their occurrence-based skip semantics,
including repeated references across manifests; live counters are unique.

### JSON (`--json`)

```json
{
  "layout_path": "./my-image",
  "destination": "registry.io/myrepo/myimage",
  "manifests": [
    {
      "digest": "sha256:def456...",
      "reference": "latest",
      "media_type": "application/vnd.oci.image.manifest.v1+json",
      "blobs": [
        {
          "digest": "sha256:aaa...",
          "size": 1048576,
          "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
          "action": "uploaded"
        },
        {
          "digest": "sha256:bbb...",
          "size": 524288,
          "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
          "action": "skipped"
        },
        {
          "digest": "sha256:ccc...",
          "size": 312,
          "media_type": "application/vnd.oci.image.config.v1+json",
          "action": "uploaded"
        }
      ],
      "status": "pushed"
    }
  ],
  "summary": {
    "manifests_pushed": 1,
    "blobs_uploaded": 2,
    "blobs_skipped": 1,
    "bytes_uploaded": 1048888
  }
}
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All manifests and blobs pushed successfully |
| 1 | Validation failure, authentication error, blob upload error, manifest push error, or I/O error |

---

## Error Messages

| Scenario | Message |
|----------|---------|
| Layout not valid | `Error: layout validation failed: <details>` |
| No manifests in index | `Error: index.json contains no manifests; run 'layout generate manifest' first` |
| Tag supplied with multi-manifest index | `Error: --dest includes a tag but index.json has <N> manifests; omit the tag or push a single-manifest layout` |
| Auth failure | `Error: authentication failed for <registry>: <details>` |
| Blob upload failure | `Error: failed to upload blob <digest>: <details>` |
| Manifest push failure | `Error: failed to push manifest <digest>: <details>` |
| Missing blob on disk | `Error: blob <digest> referenced by manifest <manifest-digest> not found in layout` |

---

## Examples

```bash
# Basic push — single manifest with tag from ref.name annotation
regshape layout push \
  --path ./my-image \
  --dest registry.io/myrepo/myimage

# Override the tag
regshape layout push \
  --path ./my-image \
  --dest registry.io/myrepo/myimage:v2.0

# Force re-upload of all blobs (skip existence checks)
regshape layout push \
  --path ./my-image \
  --dest registry.io/myrepo/myimage:latest \
  --force

# Use chunked uploads for large layers
regshape layout push \
  --path ./my-image \
  --dest registry.io/myrepo/myimage:latest \
  --chunked \
  --chunk-size 1048576

# Dry-run to see what would be pushed
regshape layout push \
  --path ./my-image \
  --dest registry.io/myrepo/myimage:latest \
  --dry-run

# Push to an insecure (HTTP) registry with verbose output
regshape --insecure --verbose layout push \
  --path ./my-image \
  --dest localhost:5000/myimage:latest

# JSON output for scripting
regshape layout push \
  --path ./my-image \
  --dest registry.io/myrepo/myimage:latest \
  --json

```

---

## End-to-End Example

Build a layout and push it in one session:

```bash
# Build the layout (offline)
regshape layout init --path ./my-image
regshape layout add layer --path ./my-image --file ./rootfs.tar.gz
regshape layout add layer --path ./my-image --file ./app.tar.gz
regshape layout generate config \
  --path ./my-image --architecture amd64 --os linux
regshape layout generate manifest \
  --path ./my-image --ref-name latest

# Validate before pushing
regshape layout validate --path ./my-image

# Push to registry
regshape layout push \
  --path ./my-image \
  --dest myregistry.azurecr.io/myapp:latest
```

---

## Library Function

The command and library share planning/execution in
[src/regshape/libs/layout/operations.py](../../src/regshape/libs/layout/operations.py):

### `push_layout(layout_path, client, repo, tag_override, force, chunked, chunk_size, progress_callback, *, transfer_callback=None, limits=None) -> PushResult`

**Parameters:**

- `layout_path: str | Path` — OCI directory or supported gzip-tar archive.
- `client: RegistryClient` — Authenticated transport client.
- `repo: str` — Target repository name (e.g. `"myrepo/myimage"`).
- `tag_override: str | None` — If provided, overrides `ref.name` annotation
  for single-root layouts, including nested indexes.
- `force: bool` — Skip existence checks when `True`.
- `chunked: bool` — Use chunked upload protocol when `True`.
- `chunk_size: int` — Chunk size (only used when `chunked=True`).
- `progress_callback: Callable | None` — Optional callable invoked as
  `progress_callback(event, **kwargs)` for UI feedback. Events:
  `"blob_start"`, `"blob_skip"`, `"blob_done"`, `"manifest_done"`.
- `transfer_callback: ProgressCallback | None` — Independent typed observer for
  actual logical byte progress in both protocols and one skipped event per
  unique existing digest. Observer exceptions propagate unchanged.
- `limits: ArtifactLimits | None` — Independent graph/archive resource budgets.

**Returns:** A `PushResult` dataclass containing the per-manifest
push report and summary statistics (manifests pushed, blobs uploaded, blobs
skipped, bytes uploaded).
Index publication reports have `blobs: []`; `manifests_pushed` counts acknowledged
document publications, including required aliases. Blob uploads and bytes are
unique, while skipped counts retain per-manifest reused-reference accounting.
Ordinary flat JSON keys and counts remain compatible. Dry-run retains its keys
and adds `media_type` to distinguish index documents.

`LayoutPushPlan(path, tag_override=None, *, limits=None)` is a context manager:
its `snapshot`, ordered `publications` and unique `blobs` are valid only inside
the context. `PushPublication` contains `document`, `reference`, ordered
layer-then-config `blobs`, and `root_reference`. The executor borrows the active
plan/client and does not close either. Use the context's snapshot store, never
the original input path; exit releases private storage even on observer errors.
These helpers are available from the operations module for command integration.

**Raises:**

- `LayoutError` — Layout is invalid or incomplete.
- `ArtifactError` / `OSError` — Strict graph, tag, budget, archive or filesystem failure.
- `AuthError` — Authentication failure.
- `BlobError` — Blob upload failure.
- `ManifestError` — Manifest push failure.

---

## Dependencies

- **Internal:**
  - [src/regshape/libs/layout/operations.py](../../src/regshape/libs/layout/operations.py) — shared plan and executor
  - [src/regshape/libs/artifacts/operations.py](../../src/regshape/libs/artifacts/operations.py) — directory/archive snapshot selection
  - [Blob operations](../../src/regshape/libs/blobs/operations.py) — `head_blob`, `upload_blob_stream`, `upload_blob_chunked`
  - [src/regshape/libs/manifests/operations.py](../../src/regshape/libs/manifests/operations.py) — verified `push_manifest`
  - [src/regshape/libs/transport/client.py](../../src/regshape/libs/transport/client.py) — `RegistryClient`, `TransportConfig`
  - [src/regshape/libs/errors.py](../../src/regshape/libs/errors.py) — explicit domain errors
- **External:** none (no new third-party packages)

---

## Progress Bars

The shared [renderer](../../src/regshape/cli/progress.py) uses Click terminal
output and typed [transfer observations](../../src/regshape/libs/progress.py).
Routine redraws are limited to ten per second; lifecycle transitions flush.
Only a blob with a trusted size has a percentage. Preparation is indeterminate,
and no whole-image byte percentage is invented.

`--json`, structured telemetry, `--no-progress`, and noninteractive stderr
suppress animation and per-state progress lines. Stdout result schemas remain
unchanged. Text telemetry/logging is coordinated with the active row; progress
controls never enter log files.

See the [progress guide](../../docs/guides/live-transfer-progress.md) for
single-row snapshots and the [callback reference](../../docs/library/transfer-progress.md)
for resource ownership and compatibility.

---

## Implementation Notes

- The existing `push` command remains under the `layout` group in
  [src/regshape/cli/layout.py](../../src/regshape/cli/layout.py).
- `push_layout()` remains exported by
  [src/regshape/libs/layout/__init__.py](../../src/regshape/libs/layout/__init__.py).
- Decorate the library function with `@track_scenario("layout push")`.
- The `head_blob` call may return a `BlobError` with status 404 — catch
  it and treat as "blob not present" rather than a fatal error.
- When `--verbose` is set, print each HTTP request/response summary
  (handled automatically by the transport middleware and `--debug-calls`).
- Both upload modes use file-backed streams and forward descriptor totals.
- Integrity checks hash payload files in bounded chunks rather than buffering
  an entire layer before the progress display starts.

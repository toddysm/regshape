# CLI: `layout push`

## Overview

The `layout push` command pushes the contents of a local OCI Image Layout
directory to a remote OCI-compliant registry. It reads the layout's
`index.json` to discover all manifests and their referenced blobs (layers
and configs), uploads every blob, then pushes each manifest. The command
reuses the existing `libs/blobs` and `libs/manifests` operations — no new
network-level code is required.

`layout push` is the natural next step after building a layout with the
`init → add layer → generate config → generate manifest` pipeline. It bridges
the offline layout world and the online registry world.

### Push Algorithm

For each manifest descriptor in `index.json`:

```
1.  Parse the manifest blob to extract its layer + config descriptors.
2.  For each blob (layers first, then config):
    a.  HEAD /v2/{repo}/blobs/{digest}  — check if already present.
    b.  If 404 → upload the blob (monolithic or chunked).
    c.  If 2xx  → skip (already exists).
3.  PUT /v2/{repo}/manifests/{reference} — push the manifest.
```

If the index contains multiple manifests (multi-platform image), each is
pushed individually. A future extension could wrap them in an index manifest;
that is out of scope for this spec.

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
| `--path` | `-p` | path | required | Root directory of a valid, completed OCI Image Layout (must have at least one manifest in `index.json`) |
| `--dest` | `-d` | string | required | Destination image reference — `registry/repo` or `registry/repo:tag`. If a tag is included it overrides the `ref.name` annotation in `index.json` for single-manifest layouts. See [Reference Resolution](#reference-resolution). |
| `--force` | | flag | `false` | Skip the `HEAD` existence check and upload every blob unconditionally |
| `--chunked` | | flag | `false` | Use the chunked (streaming) upload protocol for blobs instead of monolithic |
| `--chunk-size` | | integer | `65536` | Chunk size in bytes when `--chunked` is enabled |
| `--dry-run` | | flag | `false` | Validate the layout and print what *would* be pushed without making any network calls |

Global options `--insecure`, `--verbose`, `--json`, and the telemetry family
(`--time-methods`, `--time-scenarios`, `--debug-calls`, `--metrics`,
`--telemetry-format`, `--telemetry-verbosity`) are inherited from the parent
groups and are all supported.

---

## Reference Resolution

The `--dest` option follows the same `registry/repo[:tag]` format used
by other regshape commands:

| `--dest` value | Registry | Repo | Tag / Reference |
|----------------|----------|------|-----------------|
| `registry.io/myrepo/myimage` | `registry.io` | `myrepo/myimage` | Taken from the manifest's `org.opencontainers.image.ref.name` annotation; if absent, falls back to the manifest digest |
| `registry.io/myrepo/myimage:v1.0` | `registry.io` | `myrepo/myimage` | `v1.0` (overrides the annotation for single-manifest layouts) |

### Multi-manifest layouts

When `index.json` contains more than one manifest and `--dest` includes a
tag, the command exits with an error — a single tag cannot address multiple
manifests. Each manifest is pushed by its digest, and if it carries an
`org.opencontainers.image.ref.name` annotation, that tag is also created.

---

## Behaviour

1. **Validate** — call `validate_layout(layout)`. Exit 1 on failure.
2. **Read index** — call `read_index(layout)` to get the list of manifest
   descriptors from `index.json`.
3. **Resolve destination** — parse `--dest` via `parse_image_ref()` to
   extract `(registry, repo, reference)`. If the reference component is a
   tag and the index has more than one manifest, exit with an error.
4. **Create client** — build `RegistryClient` with
   `TransportConfig(registry=registry, insecure=ctx.obj["insecure"])`.
5. **For each manifest descriptor** in `index.json.manifests`:
   a. Read the manifest blob via `read_blob(layout, descriptor.digest)`.
   b. Parse the manifest to extract layer and config descriptors.
   c. **Upload blobs** — for each blob descriptor (layers + config):
      - Unless `--force`: call `head_blob(client, repo, blob.digest)`.
        If 2xx, invoke skip callback and continue. If `BlobError` with
        status 404 (or no status), treat as "not present"; re-raise any
        other `BlobError`.
      - For monolithic uploads: read blob bytes via
        `read_blob(layout, blob.digest)` and call
        `upload_blob(client, repo, data, blob.digest, blob.media_type)`.
      - For chunked uploads (`--chunked`): open the blob file directly
        from disk and call `upload_blob_chunked(...)` to stream without
        loading the entire blob into memory.
      - Invoke progress callback with digest and size.
   d. **Push manifest** — determine the reference:
      - If `--dest` included a tag and this is the only manifest: use
        that tag.
      - Otherwise: use the manifest's `org.opencontainers.image.ref.name`
        annotation if present; fall back to the manifest digest.
      - Call `push_manifest(client, repo, reference, manifest_bytes, descriptor.media_type)`.
      - Print confirmation with digest.
6. **Print summary** and exit 0.

### Dry-run mode (`--dry-run`)

Steps 1-3 execute normally. For steps 5a–5d the command prints what it
*would* do without making any network calls:

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

When stderr is a TTY, each blob upload is shown as a Click progress bar
that completes in one step (per-byte streaming progress is not available
from the library upload). When stderr is *not* a TTY, simple
"Uploading / Uploaded" lines are printed instead.

```
Pushing layout ./my-image -> registry.io/myrepo/myimage

  sha256:aaa...bbb (1.0 MB)  [####################################]  1048576/1048576
  sha256:ccc...ddd (512.0 KB) exists, skipping
  sha256:eee...fff (312 B)   [####################################]  312/312
  Manifest sha256:def456...ghi -> latest  pushed

Push complete: 1 manifest(s), 2 blob(s) uploaded, 1 blob(s) skipped.
```

Non-TTY output (e.g. piped to a file):

```
Pushing layout ./my-image -> registry.io/myrepo/myimage

  Uploading sha256:aaa...bbb (1.0 MB)...
  Uploaded  sha256:aaa...bbb
  sha256:ccc...ddd (512.0 KB) exists, skipping
  Uploading sha256:eee...fff (312 B)...
  Uploaded  sha256:eee...fff
  Manifest sha256:def456...ghi -> latest  pushed

Push complete: 1 manifest(s), 2 blob(s) uploaded, 1 blob(s) skipped.
```

Digests are truncated to `sha256:` + 12 hex characters in plain-text
output. Sizes use human-friendly units (B, KB, MB, GB).

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

The CLI command delegates to a library function in `libs/layout/operations.py`:

### `push_layout(layout_path, client, repo, tag_override, force, chunked, chunk_size, progress_callback) -> PushResult`

**Parameters:**

- `layout_path: str | Path` — Root of the OCI Image Layout directory.
- `client: RegistryClient` — Authenticated transport client.
- `repo: str` — Target repository name (e.g. `"myrepo/myimage"`).
- `tag_override: str | None` — If provided, overrides `ref.name` annotation
  for single-manifest layouts.
- `force: bool` — Skip existence checks when `True`.
- `chunked: bool` — Use chunked upload protocol when `True`.
- `chunk_size: int` — Chunk size (only used when `chunked=True`).
- `progress_callback: Callable | None` — Optional callable invoked as
  `progress_callback(event, **kwargs)` for UI feedback. Events:
  `"blob_start"`, `"blob_skip"`, `"blob_done"`, `"manifest_done"`.

**Returns:** A `PushResult` (dataclass or dict) containing the per-manifest
push report and summary statistics (manifests pushed, blobs uploaded, blobs
skipped, bytes uploaded).

**Raises:**

- `LayoutError` — Layout is invalid or incomplete.
- `AuthError` — Authentication failure.
- `BlobError` — Blob upload failure.
- `ManifestError` — Manifest push failure.

---

## Dependencies

- **Internal:**
  - `libs/layout/operations.py` — `validate_layout`, `read_index`, `read_blob`
  - `libs/blobs/operations.py` — `head_blob`, `upload_blob`, `upload_blob_chunked`
  - `libs/manifests/operations.py` — `push_manifest`
  - `libs/transport/client.py` — `RegistryClient`, `TransportConfig`
  - `libs/refs.py` — `parse_image_ref`
  - `libs/models/manifest.py` — `parse_manifest`
  - `libs/errors.py` — `LayoutError`, `AuthError`, `BlobError`, `ManifestError`
- **External:** none (no new third-party packages)

---

## Progress Bars

The CLI command uses `click.progressbar()` to give visual feedback during
uploads:

- **Blob upload progress** — one progress bar per blob. The bar is opened
  when the upload starts and completed in a single update when the upload
  finishes (per-byte streaming progress is not currently available from the
  library upload functions). Blobs that are skipped (already present) show
  a skip message instead of a progress bar.
- There is **no** overall manifest-level progress bar.

In `--json` mode, progress bars are suppressed and only the final JSON
result is printed. Progress bars are also suppressed when stderr is not
a TTY; simple text lines are printed instead (see Output Format above).

---

## Implementation Notes

- Add `push` as a new Click command under the existing `layout` group in
  `src/regshape/cli/layout.py`.
- Implement `push_layout()` in `src/regshape/libs/layout/operations.py`
  and export it from `libs/layout/__init__.py`.
- Decorate the library function with `@track_scenario("layout push")`.
- The `head_blob` call may return a `BlobError` with status 404 — catch
  it and treat as "blob not present" rather than a fatal error.
- When `--verbose` is set, print each HTTP request/response summary
  (handled automatically by the transport middleware and `--debug-calls`).
- Progress bars use `click.progressbar()` for each blob upload. The bar is
  completed in one step because the library upload functions do not expose
  per-byte progress callbacks.
- Chunked uploads open the blob file directly from disk for streaming
  instead of loading the entire blob into memory via `read_blob()`.

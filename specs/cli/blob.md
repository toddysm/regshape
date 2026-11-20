# CLI: blob

## Overview

The `blob` command group provides operations on OCI blobs stored in a registry.
Blobs are the raw content objects (image layers, config objects, arbitrary
artifact payloads) that are referenced by manifests.

All subcommands target a specific blob by registry/repository and digest. The
`--repo` option uses the `registry/name` format (registry hostname embedded
in the repository path); no tag suffix is accepted.

```
Usage: regshape blob [OPTIONS] COMMAND [ARGS]...
```

### Subcommands

| Command | Description |
|---|---|
| `head` | Check blob existence and retrieve metadata |
| `get` | Download a blob to a file or inspect its metadata |
| `delete` | Delete a blob from the registry |
| `upload` | Upload a blob (monolithic or chunked) |
| `mount` | Mount a blob from another repository |

For download controls, scratch budgeting, SDK equivalents and recovery, use
the [parallel-download guide](../../docs/guides/parallel-downloads.md).

---

## Usage

```
regshape blob [OPTIONS] COMMAND [ARGS]...

  Manage blobs in an OCI registry.

Options:
  --help  Show this message and exit.

Commands:
  delete  Delete a blob from the registry.
  get     Download a blob and verify its digest.
  head    Check blob existence and retrieve metadata.
  mount   Mount a blob from another repository.
  upload  Upload a blob to a repository.
```

### Repository Reference Format

`--repo` accepts the format `registry/name`, where `registry` is the registry
hostname (with optional port) and `name` is the repository path:

```
registry.example.com/myrepo/myimage
registry.example.com:5000/myorg/myimage
```

No tag suffix (`:<tag>`) and no digest suffix (`@sha256:...`) are accepted
in `--repo`. The digest is always provided separately via `--digest`.

All five commands delegate this check to the SDK's `parse_repository_ref`.
Trailing slashes remain rejected; ports and nested repository paths are
preserved. A suffix (including `:latest`) exits **1** with:

```text
Error [registry.example.com/myrepo:latest]: Repository must be a plain 'registry/repository' without a tag or digest
```

The shared parser rejects URL/userinfo syntax, whitespace/control characters,
backslashes, `?`, `#` and `%` before credential lookup. Unsafe inputs exit **1**
without echoing the supplied value:

```text
Error [--repo]: Use a registry/repository tag or digest, not a URL.
```

See the [SDK parsing contract](../../docs/guides/artifact-options.md#parse-plain-repositories-and-reject-unsafe-input)
for direct library use and the explicit SDK-only conformance opt-out.

---

## Subcommands

### `blob head`

Check whether a blob exists in the registry and retrieve its metadata without
downloading any content.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Repository in `registry/name` format |
| `--digest` | `-d` | `TEXT` | required | Blob digest in `algorithm:hex` format (`sha256:...`) |

#### Behavior

1. Resolve credentials for the registry embedded in `--repo`.
2. Issue `HEAD /v2/{name}/blobs/{digest}`.
3. Construct a `BlobInfo` from the response headers.
4. Print a JSON object containing the blob metadata to stdout.
5. Exit `0` on success.

#### Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Blob exists; metadata written to stdout |
| `1` | Blob not found (404) |
| `1` | Authentication error |
| `1` | Any other error (network, registry error) |

#### Examples

```bash
# Check existence and print metadata
regshape blob head --repo registry.example.com/myrepo --digest sha256:abc123

# Short flags
regshape blob head -r registry.example.com/myrepo -d sha256:abc123
```

#### Output Format

```json
{
  "digest": "sha256:abc123...",
  "content_type": "application/vnd.oci.image.layer.v1.tar+gzip",
  "size": 4194304
}
```

#### Error Messages

| Condition | Message |
|-----------|---------|
| Blob not found | `Error: Blob not found: registry.example.com/myrepo@sha256:abc123` |
| Auth failure | `Error: Authentication failed for registry.example.com` |
| Registry error | `Error: Registry error for registry.example.com/myrepo` |

---

### `blob get`

Download a blob and verify its digest. When `--output` is provided the blob
content is streamed to that file path. When `--output` is omitted the blob
content is still consumed and verified before metadata is printed to stdout.
Unlike `blob head`, this transfers the entire blob.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Repository in `registry/name` format |
| `--digest` | `-d` | `TEXT` | required | Blob digest (`sha256:...`) |
| `--output` | `-o` | `PATH` | `None` | File path to write blob content to |
| `--chunk-size` | | `INT` | `65536` | Positive streaming I/O buffer bytes, not HTTP range size |
| `--no-progress` | | `FLAG` | `False` | Disable interactive stderr progress |
| `--concurrency` | | `INT` | `4` | Global pooled request ceiling, 1..32 |
| `--ranges-per-blob` | | `INT` | `1` | Per-blob ceiling, 1..concurrency; 1 uses full GET |
| `--range-size` | | `BYTES` | `8388608` | Range/spool bytes, 65536..67108864 |
| `--sequential` | | `FLAG` | `False` | Use pooled concurrency=1/ranges-per-blob=1 |
| `--legacy-transport` | | `FLAG` | `False` | Bypass the new pool, coordinator and retry resolver |
| `--download-retries` | | `N` | resolved `3` | Pooled retry override, 0..10; 0 disables retries |
| `--scratch-dir` | | `PATH` | system temporary root | Existing trusted parent for private bounded range scratch |

#### Behavior

1. Validate download controls and conflicts before resolving credentials.
2. Resolve credentials and issue full or eligible ranged `GET /v2/{name}/blobs/{digest}`.
3. If `--output` is provided: stream/hash into that caller-trusted path,
   overwriting an existing file. Pooled I/O buffers are at most 64 KiB;
   ranges are assembled/hashed in original byte order.
4. After the last byte is written, compare the computed digest against
   `--digest`. If they differ: delete the partial file and exit `1` with
   an error message.
5. If `--output` is not provided: consume bounded chunks, verify the digest,
   and discard the content without buffering the complete blob.
6. Print the blob metadata JSON to stdout and exit `0`.

Supported clients use shared pooled workers, with up to three retries by default,
including HTTP 429. A full-stream failure after a file/hash prefix commits is
not replayed. `--chunk-size` remains an I/O buffer size, not an HTTP range size.
With `--ranges-per-blob` above one, the first real range negotiates size and a
strong ETag; blobs below four range sizes use full receipt. Completed parts stay
inside a bounded committed-prefix window and are hashed in byte order. Verify-only
does not save a whole blob. Capability limitations select an explicit one-time
full-stream fallback. Malformed ranges, wrong lengths and digest failures fail.
An established ETag change drains the old attempt, safely resets sink/hash/progress
and verifies one fresh full GET, retaining the original size/digest requirement.

`--sequential` conflicts with explicitly supplied `--concurrency` or
`--ranges-per-blob`, even if their values match defaults. `--legacy-transport`
conflicts with explicit sequential/concurrency/range-count/retry/scratch controls;
`--range-size` remains validated but unused. Legacy keeps the original transport
and retry behavior; sequential keeps pooled behavior. Pooled retry backoff honors
bounded `Retry-After` and cancellation. The budget is per request, including its
authentication resends/probe, not a whole-blob retry allowance after commitment.
429/503 delays above 300 seconds fail explicitly. Exhausted throttling errors
identify the retry and concurrency controls. For example, the diagnostic suffix
is `registry throttled the download. Adjust --download-retries or reduce
--concurrency/--ranges-per-blob (or use --sequential).`

Both object/range requests and authentication/redirect traffic share one
per-invocation cap; other commands/SDK calls have separate caps. Range scratch
is bounded by concurrency times range size, excluding output and metadata.
Temporary memory-backed storage consumes RAM; spooling adds a write and ordered
read even when no output is saved. HTTP ranges require a strong validator and
usable size metadata, not necessarily an `Accept-Ranges` header. They are not
the in-order POST/PATCH/PUT protocol used by chunked uploads.

Standalone output is **not transactional**. Most transport/disk/callback failures
and cancellation can leave overwritten partial bytes. Pooled digest-mismatch
cleanup deletes the invalid output filename after identity checks, even after overwrite; no
original contents are restored. With a trusted symlink output, only the link
is removed and the target may retain invalid bytes. Legacy unbounded receipt
instead unlinks the output pathname without identity checks on mismatch and
only logs deletion errors; it does not protect replacement files.
Verify-only discards bytes
after hashing, but still downloads the full content and can use bounded range
scratch. Finite socket-inactivity timeouts do not bound cancellation-to-join
latency for a trickling peer; cleanup waits for workers to stop.

The [batch/status APIs](../../docs/library/downloads.md#public-batch-downloads)
are library APIs; this command's stdout JSON is unchanged.
The command collects typed status independently of animation and emits any
changed-ETag warning only after cleanup and the flushed JSON result. Failure
warnings do not claim verification; failed renderer sinks retain their original
exception rather than being reused for formatted errors/warnings.
Warnings use plain stderr at normal verbosity, including `--no-progress`,
nonterminal stderr and non-empty `NO_COLOR`. A successful warning asserts a
verified full fallback; a failure warning says the operation did not complete.
ETag rotation alone is not corruption. See
[exact warning text and partial-output rules](../../docs/guides/parallel-downloads.md#an-etag-changed-check-the-final-outcome).

#### Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Blob downloaded (or verified) and metadata written to stdout |
| `1` | Blob not found (404) |
| `1` | Digest mismatch after download |
| `1` | Authentication error |
| `1` | Output path is not writable |
| `1` | Any other error |
| `2` | Invalid or conflicting download controls |

#### Examples

```bash
# Replace with a real config/layer digest in this repository.
REPO='registry.example.com/myrepo'
DIGEST='sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03'

# Download a blob to a trusted path (can overwrite).
regshape blob get \
  --repo "$REPO" --digest "$DIGEST" --output ./layer.blob

# Verify all bytes and print metadata without retaining content.
regshape blob get --repo "$REPO" --digest "$DIGEST"

# Streaming buffer size is independent of ranges (pooled maximum 64 KiB).
regshape blob get --repo "$REPO" --digest "$DIGEST" --chunk-size 65536

# Request up to three concurrent 8 MiB ranges for an eligible >=32 MiB blob.
regshape blob get --repo "$REPO" --digest "$DIGEST" \
  --concurrency 4 --ranges-per-blob 3 --range-size 8388608

# Lower pressure and deliberately override the retry budget, including zero.
regshape blob get --repo "$REPO" --digest "$DIGEST" --sequential --download-retries 5
regshape blob get --repo "$REPO" --digest "$DIGEST" --download-retries 0

# Explicitly roll back transport; no pooled retry/scratch overrides.
regshape blob get --repo "$REPO" --digest "$DIGEST" --legacy-transport
```

#### Output Format

The fields are unchanged; pooled receipt reports verified digest and consumed
entity size, while legacy retains header-derived metadata. The digest below is
abbreviated for display, not a valid input.

```json
{
  "digest": "sha256:abc123...",
  "content_type": "application/vnd.oci.image.layer.v1.tar+gzip",
  "size": 4194304
}
```

#### Error Messages

| Condition | Message |
|-----------|---------|
| Blob not found | `Error: Blob not found: registry.example.com/myrepo@sha256:abc123` |
| Digest mismatch | `Error: Digest mismatch: expected sha256:abc123, got sha256:def456` |
| Output path not writable | `Error: Cannot write to output path: ./layer.tar.gz` |
| Auth failure | `Error: Authentication failed for registry.example.com` |

---

### `blob delete`

Delete a blob from the registry. Exits `0` on `202 Accepted` (the OCI spec
response for a successful deletion).

> **Warning**: Blob deletion may break manifests that reference the deleted
> blob if the registry does not enforce referential integrity. Use with care.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Repository in `registry/name` format |
| `--digest` | `-d` | `TEXT` | required | Blob digest (`sha256:...`) |

#### Behavior

1. Resolve credentials for the registry.
2. Issue `DELETE /v2/{name}/blobs/{digest}`.
3. On `202 Accepted`: print a confirmation JSON object to stdout and exit `0`.
4. On any other status: call the error helper and exit `1`.

#### Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Blob deleted (`202 Accepted`) |
| `1` | Blob not found (404) |
| `1` | Operation not supported by the registry (405) |
| `1` | Authentication error |
| `1` | Any other error |

#### Examples

```bash
regshape blob delete \
  --repo registry.example.com/myrepo \
  --digest sha256:abc123
```

#### Output Format

```json
{
  "digest": "sha256:abc123...",
  "status": "deleted"
}
```

#### Error Messages

| Condition | Message |
|-----------|---------|
| Blob not found | `Error: Blob not found: registry.example.com/myrepo@sha256:abc123` |
| Not supported | `Error: Operation not supported by this registry` |
| Auth failure | `Error: Authentication failed for registry.example.com` |

---

### `blob upload`

Upload a blob from a local file. By default uses the monolithic upload
protocol (POST + PUT). When `--chunked` is specified, uses the chunked upload
protocol (POST + N×PATCH + PUT), which is required for very large blobs or
registries that do not support monolithic uploads.

Both modes verify the confirmed digest returned by the registry against
`--digest` before reporting success.
Use `--chunked` for pipe-backed or virtual files. Regular-file size hints are
checked at their boundaries without consuming upload bytes; unverified hints
remain unknown rather than being treated as trusted zero or allocation sizes.
Genuinely empty regular files retain a known zero total.
Probing, counting payload bytes and deriving result locations are library
responsibilities; the CLI renders the completed SDK result.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Repository in `registry/name` format |
| `--file` | `-f` | `PATH` | required | Local file to upload |
| `--digest` | `-d` | `TEXT` | required | Expected digest of the blob (`sha256:...`) |
| `--media-type` | | `TEXT` | `application/octet-stream` | Content-Type for the blob |
| `--chunked` | | `FLAG` | `False` | Use chunked (streaming) upload protocol |
| `--chunk-size` | | `INT` | `65536` | Chunk size in bytes (chunked mode only) |
| `--no-progress` | | `FLAG` | `False` | Disable interactive stderr progress |

#### Behavior (monolithic, default)

1. Resolve credentials for the registry.
2. Open `--file` as a binary stream; do not buffer the complete file.
3. Issue `POST /v2/{name}/blobs/uploads/` to initiate the session.
4. Issue `PUT <upload-url>?digest={digest}` with a counted, length-aware file body.
   Replayable authentication/retry/307 redirect attempts rewind this source.
5. Require `201 Created` and confirm the `Docker-Content-Digest` response
   header matches `--digest`; sending every byte alone is not success.
6. Print a JSON summary to stdout and exit `0`.

#### Behavior (chunked, `--chunked`)

1. Resolve credentials for the registry.
2. Open `--file` as a binary stream.
3. Issue `POST /v2/{name}/blobs/uploads/` to initiate the session.
4. Loop: read `--chunk-size` bytes, issue `PATCH <upload-url>` with
   `Content-Range` header; advance the session offset.
5. When the source is exhausted, issue `PUT <upload-url>?digest={digest}`
   with an empty body.
6. Require a final PUT with `201 Created` and confirm the
   `Docker-Content-Digest` response header matches `--digest`.
7. Print a JSON summary to stdout and exit `0`.

#### Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Blob uploaded successfully; confirmed digest matches |
| `1` | File not found or not readable |
| `1` | Confirmed digest does not match `--digest` |
| `1` | Upload session not found (chunked: session expired mid-upload) |
| `1` | Authentication error |
| `1` | Any other error |

#### Examples

```bash
# Monolithic upload
regshape blob upload \
  --repo registry.example.com/myrepo \
  --file ./layer.tar.gz \
  --digest sha256:abc123

# Chunked upload with custom media type
regshape blob upload \
  --repo registry.example.com/myrepo \
  --file ./layer.tar.gz \
  --digest sha256:abc123 \
  --media-type application/vnd.oci.image.layer.v1.tar+gzip \
  --chunked

# Chunked upload with 4 MB chunks
regshape blob upload \
  --repo registry.example.com/myrepo \
  --file ./layer.tar.gz \
  --digest sha256:abc123 \
  --chunked \
  --chunk-size 4194304
```

#### Output Format

`size` is the logical payload bytes actually uploaded, excluding retry traffic,
not a post-upload file stat. `location` now preserves the registry's final
`Location` header verbatim, including absolute URLs and query strings; only an
absent/empty header uses the canonical path shown below. The JSON keys are
unchanged.

```json
{
  "digest": "sha256:abc123...",
  "size": 4194304,
  "location": "/v2/myrepo/blobs/sha256:abc123..."
}
```

#### Error Messages

| Condition | Message |
|-----------|---------|
| File not found | `Error: File not found: ./layer.tar.gz` |
| Digest mismatch | `Error: Digest mismatch: expected sha256:abc123, registry confirmed sha256:def456` |
| Session not found | `Error: Upload session not found: <session-id>` |
| Invalid digest | `Error: Invalid upload: <registry detail>` |
| Auth failure | `Error: Authentication failed for registry.example.com` |

---

### `blob mount`

Mount a blob from another repository in the same registry, avoiding a
full upload when the registry supports cross-repository blob mounting.

A `201 Created` response indicates the mount succeeded. A `202 Accepted`
response indicates the registry does not support mounting or cannot access
the source repository; the operation exits `1` with a message directing
the caller to use `blob upload` instead.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Destination repository in `registry/name` format |
| `--digest` | `-d` | `TEXT` | required | Blob digest (`sha256:...`) |
| `--from-repo` | | `TEXT` | required | Source repository name (without registry prefix) |

#### Behavior

1. Resolve credentials for the registry.
2. Issue `POST /v2/{name}/blobs/uploads/?from={from-repo}&mount={digest}`.
3. On `201 Created`: print a JSON summary to stdout and exit `0`.
4. On `202 Accepted`: print an error message to stderr and exit `1` —
   the registry did not perform the mount; the caller must upload the blob
   directly.
5. On any other status: call the error helper and exit `1`.

#### Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Blob mounted successfully (`201 Created`) |
| `1` | Registry did not accept the mount (`202 Accepted`) |
| `1` | Source blob not found |
| `1` | Authentication error |
| `1` | Any other error |

#### Examples

```bash
# Mount a layer from another repository
regshape blob mount \
  --repo registry.example.com/targetrepo \
  --digest sha256:abc123 \
  --from-repo sourcerepo/myimage
```

#### Output Format

`location` now preserves the mount response's `Location` header verbatim, with
the canonical path below only as an absent/empty-header fallback. Mount JSON
retains `digest`, `status` and `location`; no content size is invented or queried.

```json
{
  "digest": "sha256:abc123...",
  "status": "mounted",
  "location": "/v2/targetrepo/blobs/sha256:abc123..."
}
```

#### Error Messages

| Condition | Message |
|-----------|---------|
| Mount not accepted | `Error [<reference>]: Blob mount not accepted for registry.example.com/targetrepo@sha256:abc123: registry returned 202 — retry with blob upload` |
| Auth failure | `Error [<reference>]: Authentication failed for registry.example.com` |
| Registry error | `Error [<reference>]: Registry error for registry.example.com/targetrepo` |

---

## Live progress (`get` and `upload`)

On interactive stderr, each blob has one row whose byte count, retry,
verification/commit, and final state are overwritten in place. Routine redraws
are limited to ten per second; phase changes and final states flush immediately.
Known nonzero sizes show a percentage; unknown sizes show bytes without an
invented total or estimated completion. Retries reset logical byte/rate tracking.
Failed or cancelled transfers never receive a successful final label.

Progress never enters stdout: the existing JSON result schema is unchanged.
Use `--no-progress` to disable the display without changing transfer protocols
or validation. Noninteractive stderr and `--telemetry-format json` telemetry also
disable animation and per-state progress lines. Text telemetry is coordinated
with the active row; log files receive telemetry only, not terminal controls.

For examples and callback semantics, see the
[progress guide](../../docs/guides/live-transfer-progress.md) and
[library reference](../../docs/library/transfer-progress.md).

## Global Behaviour

- Successful result data goes to **stdout** in JSON format; progress and
  enabled telemetry use their separate stderr destinations.
- All error messages go to **stderr** with an `Error [<reference>]: ` prefix, where `<reference>` is an implementation-defined diagnostic identifier.
- Command options `--time-methods`, `--time-scenarios`, `--debug-calls`,
  `--metrics`, `--telemetry-format`, and `--telemetry-verbosity` control
  instrumentation. There is no global `--telemetry` switch.
- Blob commands do not expose `--dry-run`; `blob head` checks existence without
  downloading the body.

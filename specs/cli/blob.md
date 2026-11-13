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
| `--chunk-size` | | `INT` | `65536` | Streaming chunk size in bytes |
| `--no-progress` | | `FLAG` | `False` | Disable interactive stderr progress |

#### Behavior

1. Resolve credentials for the registry.
2. Issue `GET /v2/{name}/blobs/{digest}`.
3. If `--output` is provided: stream the response body to the file at
   `--output` in `--chunk-size` byte increments, computing a running
   digest as each chunk is written.
4. After the last byte is written, compare the computed digest against
   `--digest`. If they differ: delete the partial file and exit `1` with
   an error message.
5. If `--output` is not provided: consume bounded chunks, verify the digest,
   and discard the content without buffering the complete blob.
6. Print the blob metadata JSON to stdout and exit `0`.

Supported clients use the shared pooled full-stream worker, with up to three
pre-payload retries by default. A failure after a file/hash prefix commits is
not replayed. `--chunk-size` remains an I/O buffer size, not an HTTP range size.
The [batch/status APIs](../../docs/library/downloads.md#public-batch-downloads)
are library-only additions; this command's options and stdout JSON are unchanged.
Range flags and typed-status rendering are separate follow-up work.

#### Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Blob downloaded (or verified) and metadata written to stdout |
| `1` | Blob not found (404) |
| `1` | Digest mismatch after download |
| `1` | Authentication error |
| `1` | Output path is not writable |
| `1` | Any other error |

#### Examples

```bash
# Download a layer to disk
regshape blob get \
  --repo registry.example.com/myrepo \
  --digest sha256:abc123 \
  --output ./layer.tar.gz

# Verify blob exists and print metadata without saving content
regshape blob get \
  --repo registry.example.com/myrepo \
  --digest sha256:abc123

# Use a larger chunk size for a faster download
regshape blob get \
  --repo registry.example.com/myrepo \
  --digest sha256:abc123 \
  --output ./layer.tar.gz \
  --chunk-size 1048576
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

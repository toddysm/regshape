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
metadata is printed to stdout without saving the content to disk; this
behaves the same as `blob head` and is useful for scripts that only need
the metadata after verifying the blob is accessible.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Repository in `registry/name` format |
| `--digest` | `-d` | `TEXT` | required | Blob digest (`sha256:...`) |
| `--output` | `-o` | `PATH` | `None` | File path to write blob content to |
| `--chunk-size` | | `INT` | `65536` | Streaming chunk size in bytes (ignored without `--output`) |

#### Behavior

1. Resolve credentials for the registry.
2. Issue `GET /v2/{name}/blobs/{digest}`.
3. If `--output` is provided: stream the response body to the file at
   `--output` in `--chunk-size` byte increments, computing a running
   SHA-256 digest as each chunk is written.
4. After the last byte is written, compare the computed digest against
   `--digest`. If they differ: delete the partial file and exit `1` with
   an error message.
5. If `--output` is not provided: read the response body fully into memory,
   verify the digest, and discard the content.
6. Print the blob metadata JSON to stdout and exit `0`.

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

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--repo` | `-r` | `TEXT` | required | Repository in `registry/name` format |
| `--file` | `-f` | `PATH` | required | Local file to upload |
| `--digest` | `-d` | `TEXT` | required | Expected digest of the blob (`sha256:...`) |
| `--media-type` | | `TEXT` | `application/octet-stream` | Content-Type for the blob |
| `--chunked` | | `FLAG` | `False` | Use chunked (streaming) upload protocol |
| `--chunk-size` | | `INT` | `65536` | Chunk size in bytes (chunked mode only) |

#### Behavior (monolithic, default)

1. Resolve credentials for the registry.
2. Read `--file` fully into memory.
3. Issue `POST /v2/{name}/blobs/uploads/` to initiate the session.
4. Issue `PUT <upload-url>?digest={digest}` with the file content as body.
5. Confirm the `Docker-Content-Digest` response header matches `--digest`.
6. Print a JSON summary to stdout and exit `0`.

#### Behavior (chunked, `--chunked`)

1. Resolve credentials for the registry.
2. Open `--file` as a binary stream.
3. Issue `POST /v2/{name}/blobs/uploads/` to initiate the session.
4. Loop: read `--chunk-size` bytes, issue `PATCH <upload-url>` with
   `Content-Range` header; advance the session offset.
5. When the source is exhausted, issue `PUT <upload-url>?digest={digest}`
   with an empty body.
6. Confirm the `Docker-Content-Digest` response header matches `--digest`.
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
| Mount not accepted | `Error: Blob mount not accepted for registry.example.com/targetrepo@sha256:abc123: registry returned 202 — retry with blob upload` |
| Auth failure | `Error: Authentication failed for registry.example.com` |
| Registry error | `Error: Registry error for registry.example.com/targetrepo` |

---

## Global Behaviour

- All output goes to **stdout** in JSON format.
- All error messages go to **stderr** with an `Error: ` prefix.
- The `--telemetry` flag (inherited from the root command) controls whether
  timing and scenario data are collected for each operation.
- `--dry-run` (when supported by the telemetry decorator layer) records the
  scenario call chain without issuing real HTTP requests.

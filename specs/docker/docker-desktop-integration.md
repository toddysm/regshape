# Docker Desktop Integration — Export and Push Local Images

## Overview

Add a `regshape docker` command group that bridges Docker Desktop's local
image store with the OCI ecosystem. It connects to the local Docker daemon
via the Docker Engine API (using the `docker` Python SDK), lets users list
available images, export them as OCI Image Layouts on disk, and push them
directly to a remote OCI-compliant registry. Multi-platform images (manifest
lists) are fully supported.

---

## Module Structure

```
src/regshape/libs/docker/
├── __init__.py        # Re-exports public symbols
└── operations.py      # list_images, export_image, push_image + conversion helpers

src/regshape/cli/docker.py   # Click commands: list, export, push
```

---

## Dependency

**New external dependency:** `docker>=7.0.0` (the official Docker SDK for
Python). Added to `requirements.txt`.

The `docker` SDK communicates with the Docker Engine daemon over the Unix
socket (`/var/run/docker.sock`) on Linux/macOS or the named pipe on Windows.
It handles connection negotiation and API versioning automatically via
`docker.from_env()`.

---

## Library Layer — `libs/docker/operations.py`

### `list_images()`

```python
def list_images(name_filter: str | None = None) -> list[DockerImageInfo]:
```

Connects to the local Docker daemon and returns a list of available images.

- Uses `docker.from_env().images.list()` internally.
- Optionally filters by repository name if `name_filter` is provided.
- Returns a list of `DockerImageInfo` dataclasses (see Data Models below).
- Raises `DockerError` if the daemon is unreachable or returns an error.

### `export_image()`

```python
def export_image(
    image_ref: str,
    output_path: str | Path,
    *,
    platform: str | None = None,
) -> None:
```

Exports a Docker image as a valid OCI Image Layout directory.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `image_ref` | `str` | Docker image reference (name:tag or image ID) |
| `output_path` | `str \| Path` | Filesystem path for the OCI layout output |
| `platform` | `str \| None` | Platform filter in `os/architecture` format (e.g. `linux/amd64`). When `None`, all available platforms are exported. |

**Algorithm:**

1. Connect to the Docker daemon; resolve `image_ref` to an image object
   (`client.images.get(image_ref)`).
2. Inspect the image to determine whether it is a single-platform image or a
   multi-platform manifest list.
3. Call `image.save()` to obtain the Docker save tar stream.
4. Extract the tar in-memory (or to a temp directory).
5. Parse `manifest.json` (Docker save format) to discover layers and config
   for each platform variant.
6. Convert to OCI Image Layout:
   - Create the OCI layout scaffold at `output_path` (reuse `init_layout()`
     from `libs/layout/`).
   - **For each platform variant** (or the single image if not multi-platform):
     a. For each layer tarball, gzip-compress it and write as a blob via
        `add_blob()`.
     b. Convert the Docker config JSON to OCI Image Config format, write via
        `add_blob()`.
     c. Build an OCI Image Manifest referencing the config and layers with
        correct OCI media types.
     d. Write the manifest via `add_blob()` and record its descriptor.
   - **If multi-platform:** Build an OCI Image Index with `manifests` array
     containing a descriptor for each platform manifest, each annotated with
     `platform.os` and `platform.architecture`. Write the index as the
     top-level entry in `index.json`.
   - **If single-platform:** Register the single manifest directly in
     `index.json`.
7. Raises `DockerError` on daemon errors, `LayoutError` on filesystem errors.

**Compression:** Layers are always gzip-compressed when written to the OCI
layout, regardless of the compression state in the Docker save tar. This
ensures consistency with OCI conventions and produces layouts with
`application/vnd.oci.image.layer.v1.tar+gzip` media types throughout.

**Docker-save-to-OCI conversion details:**

| Docker save artifact | OCI layout artifact | Media type |
|---|---|---|
| `<layer-id>/layer.tar` | `blobs/sha256/<digest>` (gzip-compressed) | `application/vnd.oci.image.layer.v1.tar+gzip` |
| `<config-digest>.json` | `blobs/sha256/<digest>` | `application/vnd.oci.image.config.v1+json` |
| (generated per platform) | `blobs/sha256/<digest>` | `application/vnd.oci.image.manifest.v1+json` |
| (generated, multi-platform only) | registered in `index.json` | `application/vnd.oci.image.index.v1+json` |

The Docker config JSON is largely compatible with the OCI Image Config spec.
The main conversion steps are: verifying the `mediaType` and stripping any
Docker-proprietary fields that have no OCI equivalent, while preserving
`architecture`, `os`, `rootfs`, `config` (env, cmd, entrypoint, labels,
etc.), and `history`.

### `push_image()`

```python
def push_image(
    image_ref: str,
    dest: str,
    *,
    platform: str | None = None,
    insecure: bool = False,
    force: bool = False,
    chunked: bool = False,
    chunk_size: int = 65536,
) -> PushResult:
```

Exports a Docker image and pushes it to a remote registry.

**Algorithm:**

1. Create a temporary directory.
2. Call `export_image(image_ref, temp_dir, platform=platform)` to produce an
   OCI layout.
3. Parse `dest` via `parse_image_ref()` to get `(registry, repo, reference)`.
4. Build a `RegistryClient` with `TransportConfig`.
5. Call `push_layout()` from `libs/layout/` to push the OCI layout to the
   remote registry.
6. Clean up the temporary directory.
7. Return a `PushResult` (the existing dataclass from `libs/layout/`).

This maximizes reuse — the network push logic is already implemented.

---

## Multi-Platform Image Support

Docker Desktop can store multi-architecture images built with `docker buildx`.
The feature handles these as follows:

### Detection

When `image.save()` produces a tar whose `manifest.json` contains multiple
entries (one per platform), the export pipeline treats the image as
multi-platform.

### Export Behaviour

| Scenario | `--platform` flag | Result |
|---|---|---|
| Single-platform image | omitted | Exports the image as a single manifest in `index.json` |
| Single-platform image | provided | Validates the platform matches; exports normally |
| Multi-platform image | omitted | Exports **all** platform variants; `index.json` contains one manifest descriptor per platform |
| Multi-platform image | `linux/amd64` | Exports **only** the matching platform variant as a single manifest |

### OCI Layout Structure (Multi-Platform)

```
<output-dir>/
├── oci-layout
├── index.json              # OCI Image Index with per-platform manifest descriptors
└── blobs/
    └── sha256/
        ├── <manifest-amd64>     # OCI manifest for linux/amd64
        ├── <manifest-arm64>     # OCI manifest for linux/arm64
        ├── <config-amd64>
        ├── <config-arm64>
        ├── <layer-shared>       # Shared layers written once (content-addressed dedup)
        ├── <layer-amd64-only>
        └── <layer-arm64-only>
```

Each manifest descriptor in `index.json` carries a `platform` object:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:aaa...",
      "size": 528,
      "platform": { "architecture": "amd64", "os": "linux" }
    },
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:bbb...",
      "size": 531,
      "platform": { "architecture": "arm64", "os": "linux" }
    }
  ]
}
```

### Push Behaviour

`push_layout()` already iterates over all manifests in `index.json`, so
multi-platform layouts are pushed correctly without additional logic. Shared
blobs between platforms are deduplicated via the existing `HEAD` existence
check.

---

## Data Models

```python
@dataclass
class DockerImageInfo:
    """Summary of a local Docker image."""
    id: str                    # Short image ID (sha256 prefix)
    repo_tags: list[str]       # e.g. ["nginx:latest", "nginx:1.25"]
    repo_digests: list[str]    # e.g. ["nginx@sha256:abc..."]
    size: int                  # Image size in bytes
    created: str               # ISO 8601 timestamp
    architecture: str          # e.g. "amd64"
    os: str                    # e.g. "linux"
```

Defined in `libs/docker/operations.py` (kept co-located since it is
domain-specific).

---

## Error Handling

New error type in `libs/errors.py`:

```python
class DockerError(RegShapeError):
    """Raised when a Docker daemon interaction fails."""
    pass
```

| Condition | Error | Message |
|---|---|---|
| Daemon not running / socket unavailable | `DockerError` | `"Cannot connect to Docker daemon. Is Docker Desktop running?"` |
| Image not found locally | `DockerError` | `"Image '{ref}' not found in local Docker store"` |
| Platform not found in multi-platform image | `DockerError` | `"Platform '{platform}' not available for image '{ref}'. Available: {list}"` |
| Docker API error | `DockerError` | Wraps the underlying SDK exception message |

---

## CLI Layer — `cli/docker.py`

### Command Group

```
regshape docker <subcommand>
```

Wired into `cli/main.py` via `regshape.add_command(docker)`.

### `regshape docker list`

```
regshape docker list [OPTIONS]
```

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--filter` | `-f` | string | `None` | Filter by image name (substring match) |
| `--json` | | flag | `false` | Output as JSON |

**Default output** (table):

```
REPOSITORY          TAG       IMAGE ID       SIZE       CREATED
nginx               latest    a8758716bb6a   187MB      2025-12-01T10:30:00Z
python              3.12      b5d5cef26b2a   1.02GB     2025-11-15T08:00:00Z
```

**JSON output** (`--json`):

```json
[
  {
    "id": "sha256:a8758716bb6a...",
    "repo_tags": ["nginx:latest"],
    "repo_digests": ["nginx@sha256:..."],
    "size": 196083713,
    "created": "2025-12-01T10:30:00Z",
    "architecture": "amd64",
    "os": "linux"
  }
]
```

### `regshape docker export`

```
regshape docker export [OPTIONS]
```

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image` | `-i` | string | required | Docker image reference (name:tag or ID) |
| `--output` | `-o` | path | required | Output directory for the OCI layout |
| `--platform` | | string | `None` | Platform filter (`os/architecture`, e.g. `linux/amd64`). Omit to export all platforms. |
| `--json` | | flag | `false` | Output as JSON |

**Behaviour:**

1. Call `export_image(image, output, platform=platform)`.
2. Print summary (number of layers, total size, output path, platforms
   exported).
3. Exit 0 on success, 1 on error.

### `regshape docker push`

```
regshape docker push [OPTIONS]
```

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image` | `-i` | string | required | Docker image reference (name:tag or ID) |
| `--dest` | `-d` | string | required | Destination registry reference (`registry/repo[:tag]`) |
| `--platform` | | string | `None` | Platform filter (`os/architecture`). Omit to push all platforms. |
| `--force` | | flag | `false` | Skip blob existence checks |
| `--chunked` | | flag | `false` | Use chunked upload |
| `--chunk-size` | | integer | `65536` | Chunk size for chunked upload |
| `--json` | | flag | `false` | Output as JSON |

**Behaviour:**

1. Call `push_image(image, dest, platform=platform, insecure=..., force=...,
   chunked=..., chunk_size=...)`.
2. Print push summary (blobs uploaded, blobs skipped, manifest digest,
   platforms pushed).
3. Exit 0 on success, 1 on error.

Inherits global options `--insecure`, `--verbose`, `--log-file` from the
parent group.

---

## Wiring in `main.py`

```python
from regshape.cli.docker import docker
regshape.add_command(docker)
```

---

## Dependencies / Reuse

| Existing module | Reused by |
|---|---|
| `libs/layout/init_layout` | `export_image` — scaffold the OCI layout directory |
| `libs/layout/add_blob` | `export_image` — write layers and config as blobs |
| `libs/layout/add_manifest` | `export_image` — write the OCI manifest |
| `libs/layout/push_layout` | `push_image` — push OCI layout to remote registry |
| `libs/layout/PushResult` | `push_image` — return type |
| `libs/refs/parse_image_ref` | CLI push — parse destination reference |
| `libs/transport/RegistryClient` | `push_image` (via `push_layout`) |
| `libs/auth/` | Credentials resolved automatically for registry push |

---

## Files to Create / Modify

| File | Action |
|---|---|
| `src/regshape/libs/docker/__init__.py` | Create |
| `src/regshape/libs/docker/operations.py` | Create |
| `src/regshape/cli/docker.py` | Create |
| `src/regshape/cli/main.py` | Modify (add docker command group) |
| `src/regshape/libs/errors.py` | Modify (add `DockerError`) |
| `requirements.txt` | Modify (add `docker>=7.0.0`) |
| `src/regshape/tests/test_docker_operations.py` | Create |
| `src/regshape/tests/test_docker_cli.py` | Create |
| `specs/docker/docker-desktop-integration.md` | Create (this spec) |
| `docs/guides/docker-desktop-integration.md` | Create |

---

## Testing Strategy

All tests mock the Docker SDK — no real daemon required.

- **`test_docker_operations.py`**: Unit tests for `list_images`,
  `export_image`, `push_image`. Mock `docker.from_env()` and
  `image.save()`. Verify OCI layout structure for export. Test both
  single-platform and multi-platform code paths. Verify platform filtering
  selects the correct variant. Verify gzip compression is always applied to
  layers. Verify `push_layout` is called correctly for push.
- **`test_docker_cli.py`**: CLI invocation tests using Click's `CliRunner`.
  Mock the library functions. Verify output format (plain text and JSON),
  `--platform` option handling, error messages (including
  platform-not-found), and exit codes.

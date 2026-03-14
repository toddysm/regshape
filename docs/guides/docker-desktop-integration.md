# Guide: Docker Desktop Integration

This guide shows how to use `regshape` to interact with your local Docker
Desktop images — listing them, exporting them as OCI Image Layouts, and pushing
them directly to remote OCI registries.

## Prerequisites

- **Docker Desktop** must be running (the Docker daemon must be reachable).
- The `docker` Python SDK is installed (`pip install docker>=7.0.0`).

You can verify connectivity with:

```bash
regshape docker list
```

If Docker Desktop is not running you will see:

```
Error [docker list]: Cannot connect to Docker daemon. Is Docker Desktop running?
```

---

## Listing local images

List all images available in your local Docker store:

```bash
regshape docker list
```

```
REPOSITORY                     TAG             IMAGE ID       SIZE       CREATED
nginx                          latest          a8758716bb6a   187MB      2025-12-01T10:30:00Z
python                         3.12            b5d5cef26b2a   1.02GB     2025-11-15T08:00:00Z
```

### Filtering by name

Use `--filter` to show only images whose name contains a substring:

```bash
regshape docker list --filter nginx
```

### JSON output

Add `--json` for machine-readable output:

```bash
regshape docker list --json
```

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

---

## Exporting an image as an OCI layout

Export a Docker image to a local directory as a valid
[OCI Image Layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md):

```bash
regshape docker export --image nginx:latest --output ./nginx-oci
```

```
Exported nginx:latest to OCI layout at ./nginx-oci
```

The resulting directory has the standard OCI Image Layout structure:

```
nginx-oci/
├── oci-layout
├── index.json
└── blobs/
    └── sha256/
        ├── <manifest-digest>
        ├── <config-digest>
        ├── <layer-digest>
        └── ...
```

### Key details

- **Layers are always gzip-compressed** in the output layout, ensuring
  consistent `application/vnd.oci.image.layer.v1.tar+gzip` media types.
- The Docker config JSON is converted to OCI Image Config format
  (Docker-proprietary fields are stripped; standard OCI fields are preserved).
- The output directory must not already exist as an OCI layout or contain
  other files. Use a fresh directory for each export.

### Multi-platform images

Docker Desktop can store multi-architecture images built with `docker buildx`.
By default, all platform variants are exported:

```bash
regshape docker export --image myapp:latest --output ./myapp-oci
```

The resulting `index.json` will contain one manifest descriptor per platform,
each with a `platform` object:

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

### Exporting a single platform

Use `--platform` to export only one variant:

```bash
regshape docker export --image myapp:latest --output ./myapp-amd64 --platform linux/amd64
```

If the requested platform is not available, the command exits with an error
listing the available platforms.

---

## Pushing a Docker image to a remote registry

Push a local Docker image directly to an OCI-compliant registry without
manually exporting first:

```bash
regshape docker push --image nginx:latest --dest registry.io/myrepo/nginx:v1
```

```
Pushed nginx:latest to registry.io/myrepo/nginx:v1: 1 manifest(s), 5 blob(s) uploaded, 0 blob(s) skipped
```

Under the hood, `regshape docker push` creates a temporary OCI layout, then
uses the existing `push_layout` infrastructure to upload blobs and manifests.

### Authentication

Registry credentials are resolved from the Docker credential store (the same
credentials used by `regshape auth login`). If the registry requires
authentication and no credentials are found, the push will fail with an auth
error.

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--image` | `-i` | Docker image reference (required) |
| `--dest` | `-d` | Destination registry reference (required) |
| `--platform` | | Export and push only one platform variant |
| `--force` | | Skip blob existence checks; upload everything |
| `--chunked` | | Use chunked (streaming) blob uploads |
| `--chunk-size` | | Chunk size in bytes (default: 65536) |
| `--json` | | Output as JSON |

### Pushing to a local registry

For testing, you can push to a local registry:

```bash
# Start a local registry
docker run -d -p 5000:5000 --name registry registry:2

# Push with --insecure (HTTP instead of HTTPS)
regshape --insecure docker push --image nginx:latest --dest localhost:5000/nginx:latest

# Verify
regshape tag list -i localhost:5000/nginx --insecure
```

### Multi-platform push

Multi-platform images are pushed correctly — shared blobs between platforms
are deduplicated via the blob existence check (`HEAD` request before upload).

```bash
# Push all platforms
regshape docker push --image myapp:latest --dest registry.io/myrepo/myapp:v1

# Push only one platform
regshape docker push --image myapp:latest --dest registry.io/myrepo/myapp:v1 --platform linux/amd64
```

---

## Error handling

| Error | Cause | Resolution |
|-------|-------|------------|
| Cannot connect to Docker daemon | Docker Desktop is not running | Start Docker Desktop |
| Image not found in local Docker store | Image name/tag does not exist locally | Run `regshape docker list` to check available images |
| Output directory is already an OCI Image Layout | Previous export exists at the path | Remove the directory or use a new path |
| Output directory already exists and is not empty | Non-empty directory at the output path | Use an empty or non-existent directory |
| Platform not available | Requested platform variant does not exist | Check available platforms in the error message |

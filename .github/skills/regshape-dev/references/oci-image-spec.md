# OCI Image Spec Reference

Quick reference for the OCI Image Format Specification v1.1.0 relevant to RegShape.

This reference complements [oci-distribution-spec.md](oci-distribution-spec.md): the
distribution spec describes how content moves over the wire (endpoints, uploads,
auth), while the image spec describes the *content itself* — descriptors, image
manifests, image indexes, image configuration, and filesystem layers.

This is a condensed summary. The full upstream OCI Image Format Specification is
saved locally under [oci-image-spec-source/](oci-image-spec-source/) (mirrored from
<https://github.com/opencontainers/image-spec>). Consult these files for authoritative,
normative detail:

- [spec.md](oci-image-spec-source/spec.md) — top-level overview
- [descriptor.md](oci-image-spec-source/descriptor.md) — content descriptors
- [manifest.md](oci-image-spec-source/manifest.md) — image manifest
- [image-index.md](oci-image-spec-source/image-index.md) — image index
- [config.md](oci-image-spec-source/config.md) — image configuration
- [layer.md](oci-image-spec-source/layer.md) — filesystem layers
- [media-types.md](oci-image-spec-source/media-types.md) — media types
- [annotations.md](oci-image-spec-source/annotations.md) — annotation keys
- [image-layout.md](oci-image-spec-source/image-layout.md) — OCI image layout
- [conversion.md](oci-image-spec-source/conversion.md), [considerations.md](oci-image-spec-source/considerations.md), [artifacts-guidance.md](oci-image-spec-source/artifacts-guidance.md)

## Table of Contents

- [Image Components](#image-components)
- [Content Descriptors](#content-descriptors)
- [Image Manifest](#image-manifest)
- [Image Index](#image-index)
- [Image Configuration](#image-configuration)
- [Filesystem Layers](#filesystem-layers)
- [Digests and Verification](#digests-and-verification)
- [Media Types](#media-types)
- [Predefined Annotations](#predefined-annotations)
- [Assembling an Image](#assembling-an-image)
- [RegShape Implementation Notes](#regshape-implementation-notes)

## Image Components

An OCI image is a set of content-addressable objects tied together by digests:

| Component | Media Type | Role |
|-----------|-----------|------|
| Image Index | `application/vnd.oci.image.index.v1+json` | Optional top-level, points to per-platform manifests |
| Image Manifest | `application/vnd.oci.image.manifest.v1+json` | Points to one config + ordered layers |
| Image Config | `application/vnd.oci.image.config.v1+json` | Execution parameters + rootfs + history |
| Layer | `application/vnd.oci.image.layer.v1.tar+gzip` | A filesystem changeset (tar, usually gzipped) |

A single-platform image is `manifest -> config + layers`. A multi-platform image is
`index -> [manifest -> config + layers, ...]`.

## Content Descriptors

Every reference between objects is a descriptor. It is the fundamental link type.

    {
      "mediaType": "application/vnd.oci.image.config.v1+json",
      "digest": "sha256:...",
      "size": 1234,
      "urls": [],
      "annotations": {},
      "data": "<base64 inline content, optional>",
      "artifactType": "<optional media type>",
      "platform": { "architecture": "amd64", "os": "linux" }
    }

Required fields: `mediaType`, `digest`, `size`. `platform` is only used inside an
image index. `data` allows small content to be embedded inline (must match `digest`).

## Image Manifest

    {
      "schemaVersion": 2,
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "artifactType": "<optional media type>",
      "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": "sha256:...",
        "size": 1234
      },
      "layers": [
        {
          "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
          "digest": "sha256:...",
          "size": 5678
        }
      ],
      "subject": {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:...",
        "size": 1234
      },
      "annotations": {}
    }

- `layers` are ordered from base (first) to top (last); this order is applied when
   building the rootfs.
- `config.mediaType` determines whether this is a runnable image or a generic
   artifact. For artifacts, set `artifactType` and use `application/vnd.oci.empty.v1+json`
   as the config.
- `subject` links this manifest to another manifest for the referrers API.

## Image Index

    {
      "schemaVersion": 2,
      "mediaType": "application/vnd.oci.image.index.v1+json",
      "artifactType": "<optional>",
      "manifests": [
        {
          "mediaType": "application/vnd.oci.image.manifest.v1+json",
          "digest": "sha256:...",
          "size": 1234,
          "platform": { "architecture": "amd64", "os": "linux" }
        }
      ],
      "subject": {},
      "annotations": {}
    }

Each entry SHOULD carry a `platform` object so clients can select the right manifest.
Optional platform fields: `os.version`, `os.features`, `variant` (e.g. `v7` for arm).

## Image Configuration

The config blob describes how to run the image and how its rootfs is built.

    {
      "created": "2024-01-01T00:00:00Z",
      "author": "regshape",
      "architecture": "amd64",
      "os": "linux",
      "config": {
        "User": "1000:1000",
        "ExposedPorts": { "8080/tcp": {} },
        "Env": ["PATH=/usr/bin"],
        "Entrypoint": ["/app"],
        "Cmd": ["--serve"],
        "Volumes": { "/data": {} },
        "WorkingDir": "/app",
        "Labels": {},
        "StopSignal": "SIGTERM"
      },
      "rootfs": {
        "type": "layers",
        "diff_ids": [
          "sha256:<uncompressed-layer-digest>",
          "sha256:<uncompressed-layer-digest>"
        ]
      },
      "history": [
        {
          "created": "2024-01-01T00:00:00Z",
          "created_by": "COPY app /app",
          "empty_layer": false
        }
      ]
    }

- `architecture` and `os` are required and MUST match the index descriptor `platform`.
- `rootfs.diff_ids` are digests over the __uncompressed__ tar of each layer, in the
   same order as `manifest.layers`. Entries with `empty_layer: true` in `history` have
   no corresponding layer/diff_id.

## Filesystem Layers

A layer is a tar archive representing a changeset applied on top of the previous layer.

- **Additions / modifications**: files present in the tar overwrite lower layers.
- **Deletions (whiteouts)**: a file named `.wh.<name>` marks `<name>` as removed.
- **Opaque directories**: a file named `.wh..wh..opq` clears all lower-layer contents
   of its directory.

Layers are applied in order; the union of all changesets is the final rootfs.

Compression: layers are normally gzip (`+gzip`) or zstd (`+zstd`). The layer digest in
the manifest is taken over the __compressed__ bytes, while the `diff_id` in the config
is taken over the __uncompressed__ tar.

## Digests and Verification

- Digests use the form `<algorithm>:<hex>`, e.g. `sha256:...` (sha512 also allowed).
- A blob is valid only if its computed digest matches the descriptor `digest` AND its
   byte length matches `size`.
- The manifest digest is computed over the exact serialized manifest bytes; do not
   re-serialize before digesting when verifying a pulled manifest.

## Media Types

| Media Type | Description |
|------------|-------------|
| `application/vnd.oci.image.index.v1+json` | Image index (multi-platform) |
| `application/vnd.oci.image.manifest.v1+json` | Image manifest |
| `application/vnd.oci.image.config.v1+json` | Image configuration |
| `application/vnd.oci.image.layer.v1.tar` | Uncompressed layer changeset |
| `application/vnd.oci.image.layer.v1.tar+gzip` | Gzip-compressed layer |
| `application/vnd.oci.image.layer.v1.tar+zstd` | Zstd-compressed layer |
| `application/vnd.oci.image.layer.nondistributable.v1.tar+gzip` | Foreign/nondistributable layer (deprecated) |
| `application/vnd.oci.empty.v1+json` | Empty config for artifact manifests (`{}`) |

## Predefined Annotations

Common `org.opencontainers.image.*` annotation keys:

| Key | Description |
|-----|-------------|
| `org.opencontainers.image.created` | Build date/time (RFC 3339) |
| `org.opencontainers.image.authors` | Contact for the image |
| `org.opencontainers.image.url` | URL to find more info |
| `org.opencontainers.image.source` | URL to source repository |
| `org.opencontainers.image.version` | Version of the packaged software |
| `org.opencontainers.image.revision` | Source control revision |
| `org.opencontainers.image.title` | Human-readable title |
| `org.opencontainers.image.description` | Human-readable description |
| `org.opencontainers.image.base.name` | Base image reference |
| `org.opencontainers.image.base.digest` | Digest of the base image manifest |

## Assembling an Image

To build and push a single-platform image from scratch:

1. Create each layer tar, compute its compressed digest (`layer.digest`/`size`) and
   its uncompressed `diff_id`.
2. Build the config JSON with `architecture`, `os`, `rootfs.diff_ids` (uncompressed,
   ordered), and any `config`/`history`.
3. Push each layer blob and the config blob (see blob operations in the distribution
   spec).
4. Build the manifest referencing the config descriptor and the ordered layer
   descriptors.
5. Push the manifest by tag or digest.
6. (Optional) Build an image index referencing per-platform manifests and push it.

Pulling reverses the process: fetch manifest (or select from index by platform),
fetch config, fetch and apply layers to reconstruct the rootfs.

## RegShape Implementation Notes

- Model image manifests, indexes, config, and descriptors in `libs/models/` as typed,
   serializable structures with strict digest/size validation.
- Keep manifest/index parsing and pushing under the manifests domain (`libs/manifests/`);
   keep image-level assembly (config building, layer packing/unpacking, diff_id
   computation, rootfs reconstruction) under the images domain (`libs/images/`).
- Preserve exact manifest bytes on pull so digest verification is byte-accurate.
- Break mode opportunities specific to images: mismatched `diff_ids` vs layers, wrong
   layer media types, config/architecture mismatches with the index platform, corrupt
   whiteout entries, and manifests whose `config.size`/`layers[].size` disagree with the
   pushed blobs.

# Guide: Create an OCI Image Layout

This guide walks you through building a valid
[OCI Image Layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
on your local filesystem using `regshape`. No registry or network connection is
required — all operations work entirely on disk.

## What is an OCI Image Layout?

An OCI Image Layout is a directory that stores OCI content (manifests, configs,
and layer blobs) content-addressed under `blobs/sha256/`, with a top-level
`oci-layout` marker file and an `index.json` (OCI Image Index) as the entry
point. It can represent any OCI artifact — container images, signatures, SBOMs,
or custom artifact types.

```
my-image/
├── oci-layout               # {"imageLayoutVersion": "1.0.0"}
├── index.json               # OCI Image Index
├── .regshape-stage.json     # regshape staging state (not part of the OCI spec)
└── blobs/
    └── sha256/
        ├── <hex>            # layer tar blob
        ├── <hex>            # config JSON blob
        └── <hex>            # manifest JSON blob
```

## Primary Workflow

The typical workflow for building a layout is a four-step pipeline:

```
layout init  →  layout add layer (×N)  →  layout generate config  →  layout generate manifest
```

State is automatically persisted between steps in a staging file
(`.regshape-stage.json`) created by `layout init`.

---

## Step 1 — Initialise the layout

```bash
regshape layout init --output ./my-image
```

This creates the directory structure, writes the `oci-layout` marker,
creates an empty `index.json`, and seeds the staging file.

```
Initialised OCI Image Layout at ./my-image
```

> **Note:** If the directory already contains a valid OCI Image Layout,
> `init` will refuse to overwrite it and exit with an error.

---

## Step 2 — Add layer(s)

Add one or more layer blobs. Each call stages the layer and records its
content-addressed descriptor.

```bash
regshape layout add layer \
  --layout ./my-image \
  --file ./layer.tar.gz
```

**Compression handling:** if the file is not already a gzip- or
zstd-compressed tar archive (detected by magic bytes), regshape automatically
compresses it with gzip. You can override this with `--compress-format zstd`.

```bash
# Force zstd compression for an uncompressed tar
regshape layout add layer \
  --layout ./my-image \
  --file ./layer.tar \
  --compress-format zstd
```

**Layer annotations:** you can attach arbitrary key=value annotations to a
layer at add time, or add/update them later with `layout annotate layer`.

```bash
regshape layout add layer \
  --layout ./my-image \
  --file ./layer.tar.gz \
  --annotation org.opencontainers.image.created=2026-03-08 \
  --annotation com.example.layer.role=base
```

Repeat `layout add layer` for each additional layer. Layers are staged in the
order they are added.

---

## Step 3 — Generate the image config

```bash
regshape layout generate config \
  --layout ./my-image \
  --architecture amd64 \
  --os linux \
  --media-type application/vnd.oci.image.config.v1+json
```

This reads the staged layer digests to populate `rootfs.diff_ids`, writes the
config JSON blob, and records it in the staging file.

If you omit any flag, regshape will prompt you interactively:

```
Architecture [amd64]: arm64
OS [linux]:
Media type [application/vnd.oci.image.config.v1+json]:
Generated config sha256:abc123... (312 bytes)
```

---

## Step 4 — Generate the manifest

```bash
regshape layout generate manifest \
  --layout ./my-image \
  --ref-name latest \
  --media-type application/vnd.oci.image.manifest.v1+json
```

This builds the OCI image manifest referencing the staged layers and config,
writes it as a blob, and registers it in `index.json` with the optional
human-readable reference name (`org.opencontainers.image.ref.name`).

```
Generated manifest [latest] sha256:def456... (578 bytes)
```

---

## Checking the staging state

At any point you can inspect what has been staged:

```bash
regshape layout status --layout ./my-image
```

```
Layers staged: 2
  [0] sha256:aaa... (4194304 bytes) application/vnd.oci.image.layer.v1.tar+gzip
  [1] sha256:bbb... (2097152 bytes) application/vnd.oci.image.layer.v1.tar+gzip
Config:   set -> sha256:ccc...
Manifest: set -> sha256:def...
```

Add `--json` to any command for machine-readable output.

---

## Viewing the index

```bash
regshape layout show --layout ./my-image
```

Prints `index.json` as pretty-printed JSON.

---

## Validating the layout

```bash
regshape layout validate --layout ./my-image
```

Checks that:
- The `oci-layout` marker is present and well-formed.
- `index.json` is a valid OCI Image Index.
- Every blob digest listed in the index and its manifests is present on disk
  and matches its declared digest.

```
Layout at ./my-image is valid.
```

---

## Post-generation updates

### Update layer annotations

Merge (or replace) annotations on a staged layer without touching the blob:

```bash
regshape layout annotate layer \
  --layout ./my-image \
  --index 0 \
  --annotation com.example.role=base
```

> Layer index is 0-based, as shown by `layout status`.

### Update the config

Re-generate the config blob with new architecture, OS, or annotations. The old
config blob is automatically deleted.

```bash
regshape layout update config \
  --layout ./my-image \
  --architecture arm64
```

> **Warning:** If you have already generated a manifest, updating the config
> produces a new config digest that the existing manifest no longer references.
> You must re-run `layout generate manifest` (or `layout annotate manifest`)
> after updating the config.

### Update manifest annotations

Add or replace top-level manifest annotations. The old manifest blob is deleted
and `index.json` is updated atomically.

```bash
regshape layout annotate manifest \
  --layout ./my-image \
  --annotation org.opencontainers.image.version=1.2.0
```

---

## Full example

```bash
# 1. Initialise
regshape layout init --output ./my-image

# 2. Add layers
regshape layout add layer \
  --layout ./my-image \
  --file ./base.tar.gz \
  --media-type application/vnd.oci.image.layer.v1.tar+gzip

regshape layout add layer \
  --layout ./my-image \
  --file ./app.tar.gz \
  --media-type application/vnd.oci.image.layer.v1.tar+gzip

# 3. Generate config
regshape layout generate config \
  --layout ./my-image \
  --architecture amd64 \
  --os linux \
  --media-type application/vnd.oci.image.config.v1+json

# 4. Generate manifest
regshape layout generate manifest \
  --layout ./my-image \
  --ref-name latest \
  --media-type application/vnd.oci.image.manifest.v1+json

# 5. Validate
regshape layout validate --layout ./my-image
```

---

## Using the Python library directly

All CLI commands are thin wrappers over the `regshape.libs.layout` module,
which you can call directly in Python scripts or other tools.

```python
from pathlib import Path
from regshape.libs.layout import (
    init_layout,
    stage_layer,
    generate_config,
    generate_manifest,
    validate_layout,
)
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
)

layout = Path("./my-image")

init_layout(layout)

with open("layer.tar.gz", "rb") as f:
    stage_layer(layout, f.read(), OCI_IMAGE_LAYER_TAR_GZIP)

generate_config(layout, architecture="amd64", os_name="linux")
generate_manifest(layout, ref_name="latest")
validate_layout(layout)
```

### Post-generation update helpers

```python
from regshape.libs.layout import (
    update_config,
    update_layer_annotations,
    update_manifest_annotations,
    read_stage,
)

# Inspect staging state
print(read_stage(layout))

# Update annotations on the first layer
update_layer_annotations(layout, 0, {"com.example.role": "base"})

# Re-generate config for a different architecture
update_config(layout, architecture="arm64")

# Add a version annotation to the manifest
update_manifest_annotations(layout, {"org.opencontainers.image.version": "1.0.0"})
```

---

## Reference

- [OCI Image Layout Specification](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
- [OCI Image Manifest Specification](https://github.com/opencontainers/image-spec/blob/main/manifest.md)
- [OCI Artifacts Guidance](https://github.com/opencontainers/image-spec/blob/main/artifacts-guidance.md)
- [regshape layout spec](../../specs/layout/oci-layout-create.md)
- [regshape layout CLI spec](../../specs/cli/layout.md)

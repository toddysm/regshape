# CLI: `layout`

## Overview

The `layout` command group creates and manages
[OCI Image Layouts](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
on the local filesystem. An OCI Image Layout is a directory that stores
manifests, configs, and layer blobs content-addressed under `blobs/sha256/`,
with a top-level `oci-layout` marker and an `index.json` as the entry point.

`layout` operates entirely on the local filesystem — no registry or network
connection is required.

### Primary Workflow

```
layout init  →  layout add layer (×N)  →  layout generate config  →  layout generate manifest
```

State between commands is tracked in a staging file (`.regshape-stage.json`)
automatically created by `layout init` inside the layout directory.

## Usage

```
regshape layout init                      [OPTIONS]
regshape layout add layer                 [OPTIONS]
regshape layout annotate layer            [OPTIONS]
regshape layout annotate manifest         [OPTIONS]
regshape layout generate config           [OPTIONS]
regshape layout generate manifest         [OPTIONS]
regshape layout update config             [OPTIONS]
regshape layout status                    [OPTIONS]
regshape layout show                      [OPTIONS]
regshape layout validate                  [OPTIONS]
```

`add`, `annotate`, `generate`, and `update` are subgroups of `layout`.

---

## Subcommands

### `layout init`

Initialise a new, empty OCI Image Layout directory.

Creates the `oci-layout` marker, `blobs/sha256/` directory, an empty
`index.json`, and the staging file `.regshape-stage.json`.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--output` | `-o` | path | required | Directory path to initialise as an OCI Image Layout |

#### Behaviour

1. Call `init_layout(output)` from `libs/layout/operations.py`.
2. On success, print the path of the initialised layout and exit 0.
3. On `LayoutError` (directory already initialised), print the error message
   and exit 1.

#### Output (plain text)

```
Initialised OCI Image Layout at /path/to/layout
```

#### Output (`--json`)

```json
{
  "layout_path": "/path/to/layout"
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Layout initialised successfully |
| 1 | Layout already exists at the path, or I/O error |

#### Examples

```bash
regshape layout init --output ./my-image
```

---

## `layout add` subgroup

Commands under `layout add` add content to the layout's blob store and record
it in the staging file.

### `layout add layer`

Add a layer to the layout and stage it for inclusion in the manifest.

The command first checks whether the input file is already a supported
compressed tar archive (gzip or zstd). If it is, the file is used as-is.
If it is not (e.g. a raw `.tar` or an unrecognised extension), it is
automatically compressed in memory using the algorithm specified by
`--compress-format` (default: `gzip`) before writing. The original
file is never modified.

After writing, the command appends the layer descriptor — including any
`--annotation` values — to the staging file. Annotations can also be added
or changed later with `layout annotate layer`.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an initialised OCI Image Layout |
| `--file` | `-f` | path | required | Path to the layer file |
| `--compress-format` | | `gzip`\|`zstd` | `gzip` | Compression algorithm to apply when the input is not already a supported compressed tar |
| `--media-type` | | string | inferred | Layer media type; if omitted, inferred from the (possibly compressed) content and confirmed interactively |
| `--annotation` | | key=value | `None` | Annotation to store on the layer descriptor; may be specified multiple times |

#### Compression Detection

The command inspects the file by magic bytes (preferred) then by extension:

| Input | Already supported? | Action |
|-------|--------------------|--------|
| `.tar.gz` / `.tgz` (magic `\x1f\x8b`) | ✓ | Use as-is |
| `.tar.zst` (magic `\xfd7zXZ\x00`) | ✓ | Use as-is |
| `.tar` (no compression) | ✗ | Compress with `--compress-format` (default `gzip`) |
| Any other extension | ✗ | Treat as raw content; compress with `--compress-format` |

When compression is applied the user is informed before the blob is written:

```
Input file is not compressed. Compressing with gzip...
```

#### Media Type Inference

Resolved after any compression is applied:

| Resulting format | Proposed Media Type |
|-----------------|---------------------|
| gzip-compressed tar | `application/vnd.oci.image.layer.v1.tar+gzip` |
| zstd-compressed tar | `application/vnd.oci.image.layer.v1.tar+zstd` |
| uncompressed tar | `application/vnd.oci.image.layer.v1.tar` |
| unknown | no default; user must supply `--media-type` |

#### Interactive Prompting

If `--media-type` is not given:

```
Detected media type for layer.tar.gz:
  application/vnd.oci.image.layer.v1.tar+gzip
Accept? [Y/n]:
```

If the user types `n`, they are prompted to enter the media type manually:

```
Enter media type: _
```

#### Behaviour

1. Read the file at `--file`.
2. Inspect the file by magic bytes, then by extension:
   - If not a supported compressed tar, compress in memory using
     `--compress-format` (default `gzip`) and inform the user.
3. Determine `media_type` via `--media-type` flag or interactive prompt.
4. Parse `--annotation` flags into a `dict[str, str]`.
5. Call `stage_layer(layout, content, media_type, annotations)`.
6. Print the staged descriptor and exit 0.
7. On error, print the message and exit 1.

#### Output (plain text)

```
Staged layer:
  digest: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    size: 1048576
    type: application/vnd.oci.image.layer.v1.tar+gzip
```

#### Output (`--json`)

```json
{
  "digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "size": 1048576,
  "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
  "annotations": {}
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Layer staged successfully |
| 1 | Layout not found / not initialised, file not found, compression error, or I/O error |

#### Examples

```bash
# Already compressed: use as-is, confirm media type interactively
regshape layout add layer --layout ./my-image --file ./layer.tar.gz

# Raw tar: auto-compressed with gzip (default)
regshape layout add layer --layout ./my-image --file ./layer.tar

# Raw tar: compress with zstd instead
regshape layout add layer \
  --layout ./my-image \
  --file ./layer.tar \
  --compress-format zstd

# Add with layer annotations
regshape layout add layer \
  --layout ./my-image \
  --file ./layer.tar.gz \
  --annotation org.opencontainers.image.created=2026-03-08 \
  --annotation com.example.layer.role=base

# Explicit media type, no interactive prompt
regshape layout add layer \
  --layout ./my-image \
  --file ./layer.tar.gz \
  --media-type application/vnd.oci.image.layer.v1.tar+gzip
```

---

## `layout annotate` subgroup

Commands under `layout annotate` add or replace annotations after the initial
staging or generation step. Layer annotations update only the staging file;
manifest annotations re-generate the manifest blob (producing a new digest).

### `layout annotate layer`

Add or replace annotations on a staged layer descriptor.

Looks up the layer by its 0-based index (as shown by `layout status`) and
merges the provided annotations into the descriptor in the staging file.
The blob file itself is not changed — only the descriptor metadata is updated.

These annotations are embedded in the manifest's `layers` array when
`layout generate manifest` is called.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an initialised OCI Image Layout |
| `--index` | `-i` | integer | required | 0-based index of the staged layer to annotate (see `layout status`) |
| `--annotation` | | key=value | required (≥1) | Annotation to add; may be specified multiple times |
| `--replace` | | flag | `False` | Replace all existing annotations on this layer instead of merging |

#### Behaviour

1. Call `read_stage(layout)` and bounds-check `--index`.
2. Merge (or replace if `--replace`) `--annotation` values into the layer
   descriptor at that index.
3. Write the updated staging file atomically.
4. Print the updated descriptor and exit 0.
5. On error, print the message and exit 1.

#### Output (plain text)

```
Updated layer [0]:
  digest: sha256:e3b0...
    type: application/vnd.oci.image.layer.v1.tar+gzip
  annotations:
    org.opencontainers.image.created = 2026-03-08
```

#### Output (`--json`)

```json
{
  "index": 0,
  "digest": "sha256:e3b0...",
  "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
  "annotations": {
    "org.opencontainers.image.created": "2026-03-08"
  }
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Annotations updated in staging file |
| 1 | Layout or staging file not found, index out of range, or I/O error |

#### Examples

```bash
# Add an annotation to layer 0
regshape layout annotate layer \
  --layout ./my-image \
  --index 0 \
  --annotation org.opencontainers.image.created=2026-03-08

# Replace all annotations on layer 1
regshape layout annotate layer \
  --layout ./my-image \
  --index 1 \
  --annotation com.example.role=patches \
  --replace
```

---

### `layout annotate manifest`

Add or replace manifest-level annotations on the registered manifest.

Reads the manifest blob from the blob store, updates its `manifest.annotations`
field, re-serialises, writes a new manifest blob, and updates both `index.json`
and the staging file. Because the manifest is content-addressed, this produces
a new digest.

**Only available after `layout generate manifest` has been called.**

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an initialised OCI Image Layout |
| `--annotation` | | key=value | required (≥1) | Manifest-level annotation to add; may be specified multiple times |
| `--replace` | | flag | `False` | Replace all existing `manifest.annotations` instead of merging |

#### Behaviour

1. Call `read_stage(layout)` and verify `manifest` is not `null`.
2. Record the old manifest digest.
3. Call `read_blob(layout, manifest.digest)` to fetch the current manifest bytes.
4. Parse the manifest JSON; merge or replace `manifest.annotations`.
5. Re-serialise and call `add_blob(layout, updated_bytes)` — yields a new digest.
6. Delete the old manifest blob from `blobs/sha256/<old-hex>` to prevent orphaned files.
7. Replace the manifest descriptor in `index.json` (preserving `ref_name` and
   `media_type`; updating `digest` and `size`).
8. Update the staging file `manifest` field.
9. Print the updated manifest descriptor and exit 0.
10. On error, print the message and exit 1.

#### Output (plain text)

```
Updated manifest:
      digest: sha256:newdigest...
        size: 789
  media-type: application/vnd.oci.image.manifest.v1+json
    ref-name: latest
  annotations:
    org.opencontainers.image.version = 1.0.0
```

#### Output (`--json`)

```json
{
  "digest": "sha256:newdigest...",
  "size": 789,
  "media_type": "application/vnd.oci.image.manifest.v1+json",
  "ref_name": "latest",
  "annotations": {
    "org.opencontainers.image.version": "1.0.0",
    "org.opencontainers.image.ref.name": "latest"
  }
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Manifest updated; `index.json` reflects new digest |
| 1 | Manifest not yet generated, layout not initialised, or I/O error |

#### Examples

```bash
# Add version and creation-date annotations
regshape layout annotate manifest \
  --layout ./my-image \
  --annotation org.opencontainers.image.version=1.0.0 \
  --annotation org.opencontainers.image.created=2026-03-08

# Replace all manifest annotations
regshape layout annotate manifest \
  --layout ./my-image \
  --annotation org.opencontainers.image.title="My Image" \
  --replace
```

---

## `layout generate` subgroup

Commands under `layout generate` build OCI artifacts from staged content.

### `layout generate config`

Generate an OCI Image Config JSON from the staged layers.

Reads the layers from the staging file, builds a config JSON containing the
layer diff IDs, writes it to the blob store, and records the config descriptor
in the staging file.

If `--architecture` or `--os` are not provided, the command proposes sensible
defaults and asks for confirmation.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an initialised OCI Image Layout |
| `--architecture` | | string | `amd64` | Target CPU architecture (proposed as default; confirmed interactively if not given) |
| `--os` | | string | `linux` | Target operating system (proposed as default; confirmed interactively if not given) |
| `--media-type` | | string | `application/vnd.oci.image.config.v1+json` | Config media type; proposed as default and confirmed interactively if not given |
| `--annotation` | | key=value (multiple) | `None` | Labels embedded in the config JSON (`config.Labels`) |

#### Interactive Prompting

If `--architecture` is not supplied:

```
Architecture [amd64]:
```

(User presses Enter to accept, or types an alternative.)

If `--os` is not supplied:

```
OS [linux]:
```

If `--media-type` is not supplied:

```
Config media type [application/vnd.oci.image.config.v1+json]:
```

#### Behaviour

1. Call `read_stage(layout)` to confirm layers are staged; raise if empty.
2. Resolve `architecture`, `os_name`, `media_type` via flags or interactive prompts.
3. Call `generate_config(layout, architecture, os_name, media_type, annotations)`.
4. Print the config descriptor and exit 0.
5. On error, print the message and exit 1.

#### Output (plain text)

```
Generated config:
  digest: sha256:abc123...
    size: 234
    type: application/vnd.oci.image.config.v1+json
```

#### Output (`--json`)

```json
{
  "digest": "sha256:abc123...",
  "size": 234,
  "media_type": "application/vnd.oci.image.config.v1+json"
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Config generated and staged |
| 1 | No layers staged, layout not initialised, or I/O error |

#### Examples

```bash
# Interactive prompts for all fields
regshape layout generate config --layout ./my-image

# Provide all fields explicitly (no prompts)
regshape layout generate config \
  --layout ./my-image \
  --architecture arm64 \
  --os linux \
  --media-type application/vnd.oci.image.config.v1+json \
  --annotation org.opencontainers.image.version=1.0.0
```

---

### `layout generate manifest`

Generate an OCI Image Manifest from the staged config and layers, and
register it in `index.json`.

Reads the config descriptor and layer descriptors from the staging file,
builds a manifest JSON — including any `--annotation` values — writes it to
the blob store, and appends its descriptor to `index.json`.

If `--ref-name` or `--annotation` are not provided, the command prompts for
values (user may leave them blank to skip). To add or update manifest
annotations after generation, use `layout annotate manifest`.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an initialised OCI Image Layout |
| `--ref-name` | | string | `None` | Human-readable reference name (e.g. `latest`, `v1.2.3`); prompted if not given |
| `--media-type` | | string | `application/vnd.oci.image.manifest.v1+json` | Manifest media type; proposed as default and confirmed interactively if not given |
| `--annotation` | | key=value (multiple) | `None` | Annotations embedded in the manifest JSON |

#### Interactive Prompting

If `--ref-name` is not supplied:

```
Reference name (e.g. latest) [skip]:
```

If `--media-type` is not supplied:

```
Manifest media type [application/vnd.oci.image.manifest.v1+json]:
```

If `--annotation` is not supplied:

```
Add manifest annotations? [y/N]:
```

If the user enters `y`, they are prompted for `key=value` pairs one at a
time; an empty line finishes input.

#### Behaviour

1. Call `read_stage(layout)` to confirm config and layers are staged.
2. Raise if config is not yet generated (instruct user to run `generate config` first).
3. Resolve `ref_name`, `media_type`, `annotations` via flags or interactive prompts.
4. Call `generate_manifest(layout, ref_name, media_type, annotations)`.
5. Print the registered manifest descriptor and exit 0.
6. On error, print the message and exit 1.

#### Output (plain text)

```
Generated manifest:
      digest: sha256:def456...
        size: 742
  media-type: application/vnd.oci.image.manifest.v1+json
    ref-name: latest
```

#### Output (`--json`)

```json
{
  "digest": "sha256:def456...",
  "size": 742,
  "media_type": "application/vnd.oci.image.manifest.v1+json",
  "ref_name": "latest",
  "annotations": {
    "org.opencontainers.image.ref.name": "latest"
  }
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Manifest generated and registered in `index.json` |
| 1 | Config not yet generated, layout not initialised, or I/O error |

#### Examples

```bash
# Interactive prompts
regshape layout generate manifest --layout ./my-image

# Explicit options
regshape layout generate manifest \
  --layout ./my-image \
  --ref-name latest \
  --annotation org.opencontainers.image.version=1.0.0
```

---

## `layout update` subgroup

Commands under `layout update` modify previously generated artifacts in the
staging file, replacing the old blob and descriptor with a freshly generated one.

### `layout update config`

Re-generate the OCI Image Config with updated parameters.

Useful when you want to change the architecture, OS, or labels after an initial
`generate config` call, without re-starting the pipeline. The staging file is
updated with the new config descriptor. If a manifest has already been generated,
a warning is printed advising you to re-run `layout generate manifest`.

**Only available after `layout generate config` has been called.**

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an initialised OCI Image Layout |
| `--architecture` | | string | *(keep existing)* | New target CPU architecture; omit to keep current value |
| `--os` | | string | *(keep existing)* | New target OS; omit to keep current value |
| `--annotation` | | key=value | `None` | Annotation to merge into `config.Labels`; may be specified multiple times |
| `--replace-annotations` | | flag | `False` | Replace all existing `config.Labels` with the `--annotation` values |

#### Behaviour

1. Call `read_stage(layout)` and verify `config` is not `null`.
2. Record the old config digest.
3. Fetch the current config blob via `read_blob(layout, config.digest)`.
4. Apply only the fields that were explicitly passed (omitted flags keep
   their existing values from the current config JSON).
5. Re-serialise and call `add_blob(layout, new_config_bytes)`.
6. Delete the old config blob from `blobs/sha256/<old-hex>` to prevent orphaned files.
7. Update the staging file: replace `config` with the new descriptor.
8. If `manifest` is not `null` in the staging file, print a warning.
9. Print the updated config descriptor and exit 0.
10. On error, print the message and exit 1.

#### Output (plain text)

```
Updated config:
  digest: sha256:newcfg...
    size: 241
    type: application/vnd.oci.image.config.v1+json

Warning: a manifest has already been generated. Re-run 'layout generate manifest'
to reference the updated config.
```

#### Output (`--json`)

```json
{
  "digest": "sha256:newcfg...",
  "size": 241,
  "media_type": "application/vnd.oci.image.config.v1+json",
  "warning": "manifest already generated; re-run 'layout generate manifest'"
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Config updated in staging file |
| 1 | Config not yet generated, layout not initialised, or I/O error |

#### Examples

```bash
# Change only the architecture
regshape layout update config \
  --layout ./my-image \
  --architecture arm64

# Add labels to the config
regshape layout update config \
  --layout ./my-image \
  --annotation org.opencontainers.image.version=1.0.0 \
  --annotation org.opencontainers.image.vendor=Acme

# Replace all labels
regshape layout update config \
  --layout ./my-image \
  --annotation org.opencontainers.image.title="My Image" \
  --replace-annotations
```

---

### `layout status`

Show the current staging state (staged layers, generated config, generated
manifest) from `.regshape-stage.json`.

Useful to check progress through the `add layer → generate config → generate manifest`
pipeline.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an OCI Image Layout |

#### Behaviour

1. Call `read_stage(layout)`.
2. Print the staging state and exit 0.
3. On error, print the message and exit 1.

#### Output (plain text)

```
Staging state for /path/to/layout:

  Layers (2):
    [0] sha256:e3b0c44... (1048576 bytes, application/vnd.oci.image.layer.v1.tar+gzip)
    [1] sha256:f5cd3d4... ( 524288 bytes, application/vnd.oci.image.layer.v1.tar+gzip)

  Config:
    sha256:abc123... (234 bytes, application/vnd.oci.image.config.v1+json)

  Manifest:
    sha256:def456... (742 bytes, application/vnd.oci.image.manifest.v1+json)
```

If config or manifest have not been generated yet, those fields show `(not yet generated)`.

#### Output (`--json`)

The raw contents of `.regshape-stage.json`:

```json
{
  "schema_version": 1,
  "layers": [
    {"digest": "sha256:e3b0c44...", "size": 1048576, "media_type": "application/vnd.oci.image.layer.v1.tar+gzip"},
    {"digest": "sha256:f5cd3d4...", "size": 524288,  "media_type": "application/vnd.oci.image.layer.v1.tar+gzip"}
  ],
  "config":   {"digest": "sha256:abc123...", "size": 234, "media_type": "application/vnd.oci.image.config.v1+json"},
  "manifest": {"digest": "sha256:def456...", "size": 742, "media_type": "application/vnd.oci.image.manifest.v1+json"}
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Status printed |
| 1 | Layout not found or staging file missing/malformed |

#### Examples

```bash
regshape layout status --layout ./my-image
```

---

### `layout show`

Print the `index.json` of an OCI Image Layout.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an OCI Image Layout |

#### Behaviour

1. Call `read_index(layout)` from `libs/layout/operations.py`.
2. Print the `ImageIndex` serialised as pretty-printed JSON and exit 0.
3. On `LayoutError`, print the error and exit 1.

#### Output

Pretty-printed `index.json`:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:def456...",
      "size": 742,
      "annotations": {
        "org.opencontainers.image.ref.name": "latest"
      }
    }
  ]
}
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Layout not found or `index.json` malformed |

#### Examples

```bash
regshape layout show --layout ./my-image
```

---

### `layout validate`

Validate the structural and content integrity of an OCI Image Layout.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--layout` | `-l` | path | required | Root directory of an OCI Image Layout to validate |

#### Behaviour

1. Call `validate_layout(layout)` from `libs/layout/operations.py`.
2. If no error is raised, print a success message and exit 0.
3. On `LayoutError`, print the error and exit 1.

#### Output (plain text)

Success:
```
Layout at /path/to/layout is valid.
```

Failure (example):
```
Error: blob sha256:deadbeef... referenced by index.json does not exist
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Layout is structurally valid and all digests match |
| 1 | Structural violation or digest mismatch found |

#### Examples

```bash
regshape layout validate --layout ./my-image
```

---

## End-to-End Example

Build an OCI Image Layout for a two-layer container image:

```bash
# 1. Initialise the layout
regshape layout init --output ./my-image

# 2. Add the base layer (raw tar — auto-compressed with gzip)
regshape layout add layer \
  --layout ./my-image \
  --file ./rootfs.tar \
  --annotation com.example.layer.role=base
# → "Input file is not compressed. Compressing with gzip..."
# → "Accept media type application/vnd.oci.image.layer.v1.tar+gzip? [Y/n]:" → Y

# 3. Add a second layer (already gzip — used as-is)
regshape layout add layer \
  --layout ./my-image \
  --file ./patches.tar.gz

# 4. Add annotations to layer 1 after the fact
regshape layout annotate layer \
  --layout ./my-image \
  --index 1 \
  --annotation com.example.layer.role=patches

# 5. Check staging state
regshape layout status --layout ./my-image

# 6. Generate the image config
regshape layout generate config \
  --layout ./my-image \
  --architecture amd64 \
  --os linux

# 7. (Optional) Modify the config to correct the architecture
regshape layout update config \
  --layout ./my-image \
  --architecture arm64

# 8. Generate the manifest and register in index.json
regshape layout generate manifest \
  --layout ./my-image \
  --ref-name latest \
  --annotation org.opencontainers.image.version=1.0.0

# 9. (Optional) Add more manifest annotations after the fact
regshape layout annotate manifest \
  --layout ./my-image \
  --annotation org.opencontainers.image.created=2026-03-08

# 10. Show the final index.json
regshape layout show --layout ./my-image

# 11. Validate the layout
regshape layout validate --layout ./my-image
```

---

## Error Messages

| Scenario | Message |
|----------|---------|
| `--output` already initialised | `Error: <path> is already an OCI Image Layout` |
| `--layout` not an initialised layout | `Error: <path> is not an OCI Image Layout (missing oci-layout file)` |
| `--file` not found | `Error: File not found: <path>` |
| Unknown extension, no `--media-type` given | `Error: cannot infer media type for '<ext>'; use --media-type` |
| Compression error | `Error: failed to compress <path>: <reason>` |
| Layer index out of range | `Error: layer index <N> out of range (staged layers: <count>)` |
| No layers staged | `Error: no layers have been staged; run 'layout add layer' first` |
| Config not yet generated | `Error: config not generated; run 'layout generate config' first` |
| Manifest not yet generated | `Error: manifest not generated; run 'layout generate manifest' first` |
| `index.json` malformed | `Error: index.json at <path> is not a valid OCI Image Index` |
| Blob digest mismatch | `Error: digest mismatch for <digest>: expected <expected>, got <actual>` |
| Missing blob during validate | `Error: blob <digest> referenced by <manifest-digest> does not exist` |

---

## Implementation Notes

- Wire `layout` as a Click command group in `src/regshape/cli/main.py` (already done).
- Implement the command group in `src/regshape/cli/layout.py`.
- `add`, `annotate`, `generate`, and `update` are Click subgroups (`@layout.group()`).
- `layer` is a command under `add`; `layer` and `manifest` are commands under `annotate`;
  `config` and `manifest` are commands under `generate`; `config` is a command under `update`.
- Interactive prompts use `click.prompt()` with a default value and `show_default=True`;
  for optional open-ended annotation input use `click.confirm()` + a prompt loop.
- Compression detection uses `pathlib.Path(file).suffixes` for extension check,
  plus reading the first 6 bytes for magic byte verification.
- Compression in-memory: `gzip.compress(data)` for gzip; `zstandard.compress(data)` for zstd.
- All library calls are to `libs/layout/operations.py`; the CLI layer handles
  only option parsing, compression, interactive prompts, output formatting, and `sys.exit`.
- The `--json` flag and telemetry flags follow the same conventions as other command groups.

## Command Structure

```
layout
├── init
├── add
│   └── layer           (compression detection + annotations)
├── annotate
│   ├── layer           (update layer descriptor annotations in staging file)
│   └── manifest        (re-generate manifest blob with updated annotations)
├── generate
│   ├── config          (build config JSON from staged layers)
│   └── manifest        (build manifest JSON from staged config + layers)
├── update
│   └── config          (re-generate config with updated arch / os / labels)
├── status
├── show
└── validate
```

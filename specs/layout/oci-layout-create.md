# Operations: OCI Layout Creation

## Overview

This spec defines the `libs/layout/` module, which provides filesystem-level
operations for creating and managing an
[OCI Image Layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
on disk.

An OCI Image Layout is a directory tree that stores manifests, configs, and
layer blobs content-addressed under `blobs/sha256/`, with a top-level
`oci-layout` marker file and an `index.json` OCI Image Index as its entry
point. The layout can represent any OCI artifact — container images,
signatures, SBOMs, or custom artifact types.

Unlike the registry-facing modules (`libs/manifests/`, `libs/blobs/`), this
module **does not make network calls**. It operates entirely on the local
filesystem.

### High-Level Workflow

The primary workflow for building a layout is a three-phase pipeline:

```
init_layout  →  stage_layer (×N)  →  generate_config  →  generate_manifest
```

State between phases is persisted in a **staging file** (`.regshape-stage.json`)
inside the layout root. The lower-level primitives `add_blob` and `add_manifest`
remain available for advanced use.

---

## Module Structure

```
src/regshape/libs/layout/
├── __init__.py        # Package marker; exports public symbols
└── operations.py      # Public operations + private helpers
```

### `__init__.py` exports

```python
__all__ = [
    # High-level staged workflow
    'stage_layer',
    'generate_config',
    'generate_manifest',
    'read_stage',
    # Post-generation updates
    'update_layer_annotations',
    'update_config',
    'update_manifest_annotations',
    # Initialisation
    'init_layout',
    # Low-level primitives
    'add_blob',
    'add_manifest',
    # Readers / validators
    'read_index',
    'read_blob',
    'validate_layout',
]
```

---

## OCI Image Layout Specification Summary

A valid OCI Image Layout directory has the following shape:

```
<layout-dir>/
├── oci-layout               # JSON marker: {"imageLayoutVersion": "1.0.0"}
├── index.json               # OCI Image Index (top-level entry point)
├── .regshape-stage.json     # Staging state (managed by this library; not OCI spec)
└── blobs/
    └── sha256/
        ├── <hex-digest>     # manifest JSON bytes
        ├── <hex-digest>     # config JSON bytes
        └── <hex-digest>     # layer tar bytes
```

- `oci-layout` — a JSON file containing exactly `{"imageLayoutVersion": "1.0.0"}`.
- `index.json` — an OCI Image Index (`application/vnd.oci.image.index.v1+json`).
  Its `manifests` array contains `Descriptor` entries pointing to the actual
  manifests stored as blobs. Each descriptor MAY carry an
  `org.opencontainers.image.ref.name` annotation to associate a human-readable
  reference name.
- `blobs/<alg>/<hex>` — every piece of content (manifests, configs, layers) is
  stored as a flat file named by its unqualified digest hex value.  Only
  `sha256` is required; `sha512` is optional.
- `.regshape-stage.json` — a library-private staging file that tracks staged
  layers and the generated config between separate CLI invocations. Not part
  of the OCI Image Layout spec.

---

## Staging File Format

The staging file `.regshape-stage.json` is a JSON file created by `init_layout`
and updated by `stage_layer`, `generate_config`, and `generate_manifest`.

### Schema

```json
{
  "schema_version": 1,
  "layers": [
    {
      "digest": "sha256:<hex>",
      "size": 12345,
      "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
      "annotations": {}
    }
  ],
  "config": {
    "digest": "sha256:<hex>",
    "size": 234,
    "media_type": "application/vnd.oci.image.config.v1+json"
  },
  "manifest": {
    "digest": "sha256:<hex>",
    "size": 567,
    "media_type": "application/vnd.oci.image.manifest.v1+json",
    "annotations": {}
  }
}
```

Field semantics:

| Field | Initial value | Set by |
|-------|---------------|--------|
| `schema_version` | `1` | `init_layout` |
| `layers` | `[]` | `stage_layer` (appends), `update_layer_annotations` (mutates) |
| `config` | `null` | `generate_config`, `update_config` |
| `manifest` | `null` | `generate_manifest`, `update_manifest_annotations` |

Notes on the `annotations` fields:
- `layers[].annotations` — stored on each layer descriptor; embedded in the
  manifest `layers` array at `generate_manifest` time.
- `manifest.annotations` — tracks the last-known `manifest.annotations` blob
  field; updated by `update_manifest_annotations`.
- `config` has no `annotations` field in staging — config labels are embedded
  inside the config JSON blob itself.

### Lifecycle

```
init_layout            → creates staging file  (layers=[], config=null, manifest=null)
stage_layer ×N         → appends layer descriptor (with optional annotations)
update_layer_annotations → mutates a layer descriptor in staging (no blob change)
generate_config        → writes config blob, sets config field
update_config          → re-writes config blob, replaces config field
generate_manifest      → writes manifest blob, sets manifest field, registers in index.json
update_manifest_annotations → re-writes manifest blob, replaces manifest field + index.json entry
```

The staging file is not deleted after `generate_manifest`. This allows
introspection of the final state and permits re-generating the manifest
with different options. Call `init_layout` again (with a new or clean
directory) to start a fresh build.

---

## Public Operations

### `init_layout`

```python
def init_layout(path: str | Path) -> None:
```

Initialise a new OCI Image Layout directory at *path*.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | `str \| Path` | Filesystem path for the new layout root |

**Behaviour:**

1. Create *path* (and any missing parents) if it does not already exist.
2. Raise `LayoutError` if *path* already contains an `oci-layout` file
   (i.e. it is already a valid layout), to prevent accidental re-init.
3. Write `<path>/oci-layout` containing `{"imageLayoutVersion": "1.0.0"}`.
4. Create `<path>/blobs/sha256/`.
5. Write `<path>/index.json` containing an empty OCI Image Index:
   ```json
   {
     "schemaVersion": 2,
     "mediaType": "application/vnd.oci.image.index.v1+json",
     "manifests": []
   }
   ```
6. Write `<path>/.regshape-stage.json` with initial staging state:
   ```json
   {"schema_version": 1, "layers": [], "config": null, "manifest": null}
   ```

**Raises:**

- `LayoutError` — if the directory already contains an `oci-layout` marker.
- `OSError` — on filesystem permission or I/O errors (not caught; let the OS
  surface these to the caller).

---

### `stage_layer`

```python
def stage_layer(
    layout_path: str | Path,
    content: bytes,
    media_type: str,
    annotations: dict[str, str] | None = None,
) -> Descriptor:
```

Write *content* as a layer blob in the layout and append its `Descriptor` to
the staging file.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layout_path` | `str \| Path` | Root of an initialised OCI layout |
| `content` | `bytes` | Raw layer bytes (already-compressed tar as needed by caller) |
| `media_type` | `str` | Layer media type (e.g. `application/vnd.oci.image.layer.v1.tar+gzip`) |
| `annotations` | `dict[str, str] \| None` | Optional annotations to store on the layer descriptor |

**Behaviour:**

1. Validate that *layout_path* is an initialised layout; raise `LayoutError` if not.
2. Call `add_blob(layout_path, content)` to write the blob content-addressed.
3. Build a `Descriptor` with `digest`, `size`, `media_type`, and optional `annotations`.
4. Read `.regshape-stage.json`, append the descriptor to `layers`, and write
   the staging file back atomically.
5. Return the `Descriptor`.

**Note:** Compression is the caller's responsibility. The CLI layer compresses
the file in memory before calling this function. The library takes raw bytes.

**Returns:** `Descriptor` appended to the staging file.

**Raises:**

- `LayoutError` — if *layout_path* is not an initialised layout or the staging
  file is missing/malformed.
- `OSError` — on I/O errors.

---

### `generate_config`

```python
def generate_config(
    layout_path: str | Path,
    architecture: str = "amd64",
    os_name: str = "linux",
    media_type: str = "application/vnd.oci.image.config.v1+json",
    annotations: dict[str, str] | None = None,
) -> Descriptor:
```

Generate an OCI Image Config JSON from the staged layers and write it to the
blob store.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_path` | `str \| Path` | required | Root of an initialised OCI layout |
| `architecture` | `str` | `"amd64"` | Target CPU architecture (e.g. `amd64`, `arm64`, `arm`) |
| `os_name` | `str` | `"linux"` | Target OS (e.g. `linux`, `windows`) |
| `media_type` | `str` | `application/vnd.oci.image.config.v1+json` | Media type of the config blob |
| `annotations` | `dict[str, str] \| None` | `None` | Annotations embedded inside the config JSON (`config.Labels`); only used for image configs |

**Behaviour:**

1. Validate *layout_path* is an initialised layout.
2. Read the staging file; raise `LayoutError` if no layers have been staged yet.
3. Build an OCI Image Config JSON using staged layer digests as `diff_ids`:
   ```json
   {
     "architecture": "<architecture>",
     "os": "<os_name>",
     "rootfs": {
       "type": "layers",
       "diff_ids": ["sha256:<layer1-hex>", "sha256:<layer2-hex>", ...]
     }
   }
   ```
   If `annotations` is provided, merge them into `config.Labels`.
4. Serialise to UTF-8 bytes, call `add_blob(layout_path, config_bytes)`.
5. Build a `Descriptor` with `digest`, `size`, `media_type`.
6. Update `.regshape-stage.json` — set the `config` field to the descriptor.
7. Return the `Descriptor`.

**Notes:**

- For non-image artifacts (SBOMs, signatures, etc.) pass
  `media_type="application/vnd.oci.empty.v1+json"` and use empty content
  (`b"{}"`) — callers may supply a custom config path via the CLI instead.
- If the staging file already has a `config` field set, overwrite it (allows
  re-running `generate_config` with different parameters).

**Returns:** `Descriptor` of the stored config blob.

**Raises:**

- `LayoutError` — if no layers have been staged, or the layout is not initialised.
- `OSError` — on I/O errors.

---

### `generate_manifest`

```python
def generate_manifest(
    layout_path: str | Path,
    ref_name: str | None = None,
    media_type: str = "application/vnd.oci.image.manifest.v1+json",
    annotations: dict[str, str] | None = None,
) -> Descriptor:
```

Generate an OCI Image Manifest from the staged config + layers, register it
in `index.json`, and update the staging file.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_path` | `str \| Path` | required | Root of an initialised OCI layout |
| `ref_name` | `str \| None` | `None` | Human-readable reference name stored as `org.opencontainers.image.ref.name` in `index.json` |
| `media_type` | `str` | `application/vnd.oci.image.manifest.v1+json` | Manifest media type |
| `annotations` | `dict[str, str] \| None` | `None` | Annotations embedded in the manifest JSON (`manifest.annotations`) |

**Behaviour:**

1. Validate *layout_path* is an initialised layout.
2. Read the staging file; raise `LayoutError` if `config` is `null` (config
   must be generated before the manifest).
3. Build an OCI Image Manifest JSON:
   ```json
   {
     "schemaVersion": 2,
     "mediaType": "<media_type>",
     "config": { "mediaType": "<config.media_type>", "digest": "<config.digest>", "size": <config.size> },
     "layers": [
       { "mediaType": "<layer.media_type>", "digest": "<layer.digest>", "size": <layer.size> },
       ...
     ],
     "annotations": { ... }
   }
   ```
4. Serialise to UTF-8 bytes.
5. Call `add_manifest(layout_path, manifest_bytes, media_type, ref_name, annotations)`.
6. Update `.regshape-stage.json` — set the `manifest` field to the returned descriptor.
7. Return the `Descriptor` registered in `index.json`.

**Raises:**

- `LayoutError` — if the staging file has no config, has no layers, or the
  layout is not initialised.
- `OSError` — on I/O errors.

---

### `read_stage`

```python
def read_stage(layout_path: str | Path) -> dict:
```

Return the current staging state parsed from `.regshape-stage.json`.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layout_path` | `str \| Path` | Root of an initialised OCI layout |

**Returns:** `dict` with keys `schema_version`, `layers`, `config`, `manifest`.

**Raises:**

- `LayoutError` — if the staging file is missing (layout not initialised) or
  malformed JSON.

---

### `update_layer_annotations`

```python
def update_layer_annotations(
    layout_path: str | Path,
    layer_index: int,
    annotations: dict[str, str],
    replace: bool = False,
) -> Descriptor:
```

Merge or replace annotations on a staged layer descriptor without changing
the blob file.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_path` | `str \| Path` | required | Root of an initialised OCI layout |
| `layer_index` | `int` | required | 0-based index into the staged `layers` array |
| `annotations` | `dict[str, str]` | required | Annotations to merge in (or replace, if `replace=True`) |
| `replace` | `bool` | `False` | If `True`, overwrite all existing annotations; if `False`, merge |

**Behaviour:**

1. Call `read_stage(layout_path)` and bounds-check `layer_index`.
2. Merge or replace annotations on the descriptor at `layer_index`.
3. Write the staging file back atomically.
4. Return the updated `Descriptor`.

**Returns:** Updated `Descriptor` for that layer.

**Raises:**

- `LayoutError` — if the layout is not initialised, the staging file is
  missing, or `layer_index` is out of range.

---

### `update_config`

```python
def update_config(
    layout_path: str | Path,
    architecture: str | None = None,
    os_name: str | None = None,
    annotations: dict[str, str] | None = None,
    replace_annotations: bool = False,
) -> Descriptor:
```

Re-generate the OCI Image Config with updated fields and replace the staged
config descriptor.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_path` | `str \| Path` | required | Root of an initialised OCI layout |
| `architecture` | `str \| None` | `None` | New CPU architecture; `None` keeps the existing value |
| `os_name` | `str \| None` | `None` | New OS; `None` keeps the existing value |
| `annotations` | `dict[str, str] \| None` | `None` | Annotations to merge into `config.Labels` |
| `replace_annotations` | `bool` | `False` | If `True`, replace all existing `config.Labels`; if `False`, merge |

**Behaviour:**

1. Call `read_stage(layout_path)` and verify `config` is not `null`.
2. Record the old config digest from the staging file.
3. Call `read_blob(layout_path, config.digest)` to fetch the current config bytes.
4. Parse the config JSON; apply only the fields that are not `None`.
5. Re-serialise and call `add_blob(layout_path, new_config_bytes)` to write the new blob.
6. Delete `blobs/sha256/<old-config-hex>` from the filesystem to prevent orphaned blobs.
7. Update the staging file: replace `config` with the new descriptor.
8. Return the new config `Descriptor`.

**Returns:** New `Descriptor` for the config blob.

**Raises:**

- `LayoutError` — if the config has not yet been generated, or the layout is
  not initialised.

---

### `update_manifest_annotations`

```python
def update_manifest_annotations(
    layout_path: str | Path,
    annotations: dict[str, str],
    replace: bool = False,
) -> Descriptor:
```

Re-generate the manifest with updated `manifest.annotations`, write the new
blob, and update `index.json`.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_path` | `str \| Path` | required | Root of an initialised OCI layout |
| `annotations` | `dict[str, str]` | required | Annotations to merge in (or replace, if `replace=True`) |
| `replace` | `bool` | `False` | If `True`, replace all existing `manifest.annotations`; if `False`, merge |

**Behaviour:**

1. Call `read_stage(layout_path)` and verify `manifest` is not `null`.
2. Record the old manifest digest from the staging file.
3. Call `read_blob(layout_path, manifest.digest)` to fetch the current manifest bytes.
4. Parse the manifest JSON; merge or replace `manifest.annotations`.
5. Re-serialise and call `add_blob(layout_path, updated_bytes)` to write the new blob — yields a new digest.
6. Delete `blobs/sha256/<old-manifest-hex>` from the filesystem to prevent orphaned blobs.
7. Update `index.json`: replace the old manifest descriptor; preserve `media_type`
   and `org.opencontainers.image.ref.name`; update `digest` and `size`.
8. Update the staging file `manifest` field with the new descriptor.
9. Return the new `Descriptor` as registered in `index.json`.

**Returns:** New `Descriptor` registered in `index.json`.

**Raises:**

- `LayoutError` — if the manifest has not yet been generated, or the layout is
  not initialised.

---

### `add_blob` *(low-level primitive)*

```python
def add_blob(layout_path: str | Path, content: bytes) -> tuple[str, int]:
```

Write *content* to the layout's blob store and return its digest and size.
Use `stage_layer` for the primary workflow; `add_blob` is available for
advanced scenarios (e.g. uploading a pre-built config or arbitrary blob).

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layout_path` | `str \| Path` | Root of an initialised OCI layout |
| `content` | `bytes` | Raw blob bytes |

**Behaviour:**

1. Validate *layout_path* is an initialised layout.
2. Compute `digest = "sha256:" + sha256hex(content)`.
3. Compute `size = len(content)`.
4. Derive `blob_path = <layout_path>/blobs/sha256/<hex>`.
5. If *blob_path* already exists and has the expected size, skip the write
   (idempotent).
6. Otherwise write content atomically (temp file + rename).
7. Return `(digest, size)`.

**Returns:** `tuple[str, int]` — `(digest, size)`.

**Raises:**

- `LayoutError` — if *layout_path* is not an initialised layout.
- `OSError` — on I/O errors.

---

### `add_manifest` *(low-level primitive)*

```python
def add_manifest(
    layout_path: str | Path,
    manifest_bytes: bytes,
    media_type: str,
    ref_name: str | None = None,
    annotations: dict[str, str] | None = None,
) -> Descriptor:
```

Write a manifest blob and register it in `index.json`.
Use `generate_manifest` for the primary workflow; `add_manifest` is available
for registering pre-built manifests.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_path` | `str \| Path` | required | Root of an initialised OCI layout |
| `manifest_bytes` | `bytes` | required | Serialised manifest JSON bytes |
| `media_type` | `str` | required | Manifest media type |
| `ref_name` | `str \| None` | `None` | Optional reference name annotation in `index.json` |
| `annotations` | `dict[str, str] \| None` | `None` | Extra annotations on the index descriptor |

**Behaviour:**

1. Validate *layout_path* is an initialised layout.
2. Call `add_blob(layout_path, manifest_bytes)`.
3. Build a `Descriptor` with `media_type`, `digest`, `size`, and merged annotations.
4. Read `index.json`, append the descriptor, write back atomically.
5. Return the `Descriptor`.

**Returns:** `Descriptor` as registered in `index.json`.

**Raises:**

- `LayoutError` — if *layout_path* is not an initialised layout or `index.json`
  is malformed.
- `OSError` — on I/O errors.

---

### `read_index`

```python
def read_index(layout_path: str | Path) -> ImageIndex:
```

Parse and return the `index.json` of an OCI Image Layout.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layout_path` | `str \| Path` | Root of an initialised OCI layout |

**Behaviour:**

1. Read `<layout_path>/index.json`.
2. Parse with `parse_manifest()` from `libs/models/manifest.py`.
3. Validate the result is an `ImageIndex`; raise `LayoutError` if not.
4. Return the `ImageIndex`.

**Returns:** `ImageIndex`

**Raises:**

- `LayoutError` — if `index.json` is missing, not parseable as an
  `ImageIndex`, or the layout is not initialised.

---

### `read_blob`

```python
def read_blob(layout_path: str | Path, digest: str) -> bytes:
```

Read and return the raw bytes of a blob by digest, with integrity verification.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layout_path` | `str \| Path` | Root of an initialised OCI layout |
| `digest` | `str` | Content digest in `"<alg>:<hex>"` form |

**Behaviour:**

1. Parse *digest* to extract algorithm and hex string.
2. Read `<layout_path>/blobs/<alg>/<hex>`.
3. Verify the SHA-256 of the returned bytes matches the requested digest.
4. Return the bytes.

**Returns:** `bytes`

**Raises:**

- `LayoutError` — if the blob is not found or digest does not match.

---

### `validate_layout`

```python
def validate_layout(layout_path: str | Path) -> None:
```

Check that *layout_path* is a structurally valid OCI Image Layout.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layout_path` | `str \| Path` | Path to validate |

**Behaviour:**

1. Verify `oci-layout` file exists and contains `{"imageLayoutVersion": "1.0.0"}`.
2. Verify `index.json` exists and is parseable as an `ImageIndex`.
3. For every descriptor in `index.json` → `manifests`:
   a. Verify the corresponding blob file exists under `blobs/sha256/`.
   b. Recompute the digest of the blob and verify it matches the descriptor.
4. For every manifest blob, parse it as an `ImageManifest` or `ImageIndex`
   and verify all referenced blobs (config, layers) also exist and their
   digests match.
5. Raise `LayoutError` with a descriptive message at the first violation found.

**Raises:**

- `LayoutError` — describing the first structural or integrity violation.

---

## Data Models

No new persistent data models are introduced. The module reuses:

- `Descriptor` from `libs/models/descriptor.py`
- `ImageManifest` and `ImageIndex` from `libs/models/manifest.py`
- `parse_manifest()` from `libs/models/manifest.py`

### `OciLayoutMarker` (internal helper dataclass)

Used only when reading/writing the `oci-layout` file.

```python
@dataclass
class OciLayoutMarker:
    imageLayoutVersion: str = "1.0.0"
```

---

## Error Handling

`LayoutError(RegShapeError)` is defined in `libs/errors.py`:

```python
class LayoutError(RegShapeError):
    """Raised when an OCI Image Layout operation fails."""
    pass
```

| Condition | Error | Behaviour |
|-----------|-------|-----------|
| `init_layout` on already-initialised directory | `LayoutError` | Raise immediately |
| `stage_layer` / `add_blob` / `add_manifest` on non-layout directory | `LayoutError` | Raise immediately |
| `generate_config` with no staged layers | `LayoutError` | Raise with message |
| `generate_manifest` before `generate_config` | `LayoutError` | Raise with message |
| `update_config` before `generate_config` | `LayoutError` | Raise with message |
| `update_manifest_annotations` before `generate_manifest` | `LayoutError` | Raise with message |
| `update_layer_annotations` with out-of-range `layer_index` | `LayoutError` | Raise with index + count |
| `index.json` missing or malformed | `LayoutError` | Raise with path context |
| `.regshape-stage.json` missing or malformed | `LayoutError` | Raise with path context |
| Blob file not found during read or validate | `LayoutError` | Raise with digest context |
| Blob digest mismatch on read | `LayoutError` | Raise with expected vs actual |
| `index.json` concurrent write conflict | Mitigated via atomic rename | N/A |

---

## Protocol / Filesystem Flow

### Primary Workflow

```
Caller                              Filesystem
  |                                     |
  |-- init_layout(path) ------------>   | mkdir -p path/blobs/sha256/
  |                                     | write path/oci-layout
  |                                     | write path/index.json (empty index)
  |                                     | write path/.regshape-stage.json
  |                                     |
  |-- stage_layer(path, l1, mt) ---->   | sha256(l1) → digest1
  |                                     | write path/blobs/sha256/<hex1>
  |                                     | append to .regshape-stage.json layers[]
  |<- Descriptor(digest1, size1, mt) -  |
  |                                     |
  |-- stage_layer(path, l2, mt) ---->   | (same; may be multiple layers)
  |<- Descriptor(digest2, size2, mt) -  |
  |                                     |
  |-- generate_config(path, ...) ---->  | build config JSON from staged layers
  |                                     | write path/blobs/sha256/<hex-cfg>
  |                                     | set .regshape-stage.json config field
  |<- Descriptor(digest-cfg, size) --   |
  |                                     |
  |-- generate_manifest(path, ...) -->  | build manifest JSON from staged config+layers
  |                                     | write path/blobs/sha256/<hex-mfst>
  |                                     | append descriptor to index.json
  |                                     | set .regshape-stage.json manifest field
  |<- Descriptor (index entry) ------   |
  |                                     |
  |-- validate_layout(path) -------->   | check oci-layout, index.json, blobs
  |<- (raises LayoutError or ok) ----   |
```

### Low-Level / Advanced Flow

```
init_layout  →  add_blob (×N, any content)  →  add_manifest (pre-built JSON)
```

---

## Dependencies

- **Internal:**
  - `libs/models/descriptor.py` — `Descriptor`
  - `libs/models/manifest.py` — `ImageManifest`, `ImageIndex`, `parse_manifest`
  - `libs/errors.py` — `RegShapeError` (base for `LayoutError`)
- **External / stdlib:**
  - `hashlib` — SHA-256 digest computation
  - `json` — JSON serialisation
  - `gzip` — in-memory gzip compression (CLI layer only)
  - `pathlib` — `Path` objects for filesystem operations
  - `os` / `tempfile` — atomic file writes via rename
- **Optional third-party (CLI layer only):**
  - `zstandard` — zstd compression; required only if `--compress-format zstd` is used

No new third-party dependencies are required for the library module itself.

---

## Open Questions

- [ ] Should `generate_manifest` enforce that all staged layer blobs exist
  before writing the manifest? Current design: yes — `stage_layer` writes the
  blob synchronously so they always exist.
- [ ] Should calling `generate_config` a second time (before `generate_manifest`)
  overwrite the earlier config blob or raise an error? Current design: overwrite
  and re-stage config (allows iterating on config parameters).
- [ ] Should `.regshape-stage.json` be deleted after `generate_manifest`?
  Current design: no — keep it for introspection via `regshape layout status`.
- [ ] Should multi-platform image indexes be supported as entries in
  `index.json`? Current design: any valid media type is allowed.

# Data Model: Manifest

## Overview

This spec defines the data models for OCI image manifests and related types in
`src/regshape/libs/models/`. These models are the canonical in-memory
representations used throughout the library whenever manifests are pulled from
or pushed to a registry. They handle serialization to and deserialization from
the OCI JSON wire format and provide digest computation.

The manifest model family covers:

- **`Descriptor`** — the fundamental content-reference type used as a building
  block in manifests, indexes, and referrer lists
- **`Platform`** — CPU/OS platform specification embedded in index manifest
  descriptors
- **`ImageManifest`** — a single-architecture OCI image manifest (or artifact
  manifest when paired with `artifactType`)
- **`ImageIndex`** — a multi-architecture image index (or referrer response
  index)
- **`parse_manifest()`** — factory function that dispatches on `mediaType` to
  return the correct type

Media type constants live in the companion module `libs/models/mediatype.py`
and are imported where needed.

---

## Module Structure

```
src/regshape/libs/models/
├── __init__.py          # Exports: Descriptor, Platform, ImageManifest,
│                        #          ImageIndex, parse_manifest, MediaType constants
├── descriptor.py        # Descriptor, Platform
├── manifest.py          # ImageManifest, ImageIndex, parse_manifest
└── mediatype.py         # Media type string constants
```

---

## Data Models

### `Descriptor`

The fundamental building block of the OCI content model. Every piece of
content referenced in a manifest (config, layer, manifest entry in an index,
subject, referrer) is expressed as a `Descriptor`.

```python
@dataclass
class Descriptor:
    media_type: str                          # Wire key: "mediaType"
    digest: str                              # Wire key: "digest"  (e.g. "sha256:abc...")
    size: int                                # Wire key: "size"
    platform: Optional[Platform] = None      # Wire key: "platform" (index entries only)
    annotations: Optional[dict[str, str]] = None  # Wire key: "annotations"
    artifact_type: Optional[str] = None      # Wire key: "artifactType"
    urls: Optional[list[str]] = None         # Wire key: "urls"
```

**Validation rules:**

| Field | Rule | Error |
|-------|------|-------|
| `media_type` | Must be non-empty string | `ValueError` |
| `digest` | Must match `^(sha256\|sha512):[a-f0-9]+$` | `ValueError` |
| `size` | Must be >= 0 | `ValueError` |

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> dict` | Serializes to OCI wire dict; omits `None` fields |
| `from_dict` | `(cls, data: dict) -> Descriptor` | Deserializes from OCI wire dict |

**Notes:**

- `platform` is `None` on all descriptors except manifest entries inside an
  `ImageIndex`.
- `urls` is an optional list of external URLs from which the content can be
  downloaded (rarely used outside artifact scenarios).

---

### `Platform`

CPU and operating system specification embedded in `ImageIndex` manifest
descriptors.

```python
@dataclass
class Platform:
    architecture: str                          # Wire key: "architecture"
    os: str                                    # Wire key: "os"
    os_version: Optional[str] = None           # Wire key: "os.version"
    os_features: Optional[list[str]] = None    # Wire key: "os.features"
    variant: Optional[str] = None              # Wire key: "variant"
```

**Validation rules:**

| Field | Rule | Error |
|-------|------|-------|
| `architecture` | Must be non-empty string | `ValueError` |
| `os` | Must be non-empty string | `ValueError` |

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> dict` | Serializes to OCI wire dict; omits `None` fields |
| `from_dict` | `(cls, data: dict) -> Platform` | Deserializes from OCI wire dict |

**Notes:**

- The OCI spec uses `"os.version"` and `"os.features"` as the literal JSON
  keys (dot-separated). `Platform.from_dict` must handle these key names.

---

### `ImageManifest`

An OCI Image Manifest (`schemaVersion` 2). Also used for Docker V2 Manifests —
the distinction is carried by `media_type`.

```python
@dataclass
class ImageManifest:
    schema_version: int                            # Wire key: "schemaVersion" (always 2)
    media_type: str                                # Wire key: "mediaType"
    config: Descriptor                             # Wire key: "config"
    layers: list[Descriptor]                       # Wire key: "layers"
    subject: Optional[Descriptor] = None           # Wire key: "subject"
    annotations: Optional[dict[str, str]] = None   # Wire key: "annotations"
    artifact_type: Optional[str] = None            # Wire key: "artifactType"
```

**Validation rules:**

| Field | Rule | Error |
|-------|------|-------|
| `schema_version` | Must equal `2` | `ValueError` |
| `media_type` | Must be one of the known manifest media types | `ValueError` |
| `layers` | Must be a list (may be empty for artifact manifests) | `ValueError` |

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_json` | `() -> str` | Canonical JSON: `sort_keys=True`, no extra whitespace |
| `digest` | `() -> str` | `"sha256:" + sha256(to_json().encode("utf-8")).hexdigest()` |
| `from_json` | `(cls, data: str) -> ImageManifest` | Deserializes from JSON string |

**Notes on `to_json()` stability:**

`to_json()` uses `json.dumps(..., separators=(',', ':'), sort_keys=True)` to
produce a canonical wire representation. Because `digest()` is derived from
`to_json()`, identical logical manifests must produce identical digests. Do not
re-order fields or add whitespace between calls.

---

### `ImageIndex`

An OCI Image Index (`schemaVersion` 2). Used both as a multi-architecture
manifest list and as the response body of the referrers API. Also used for
Docker V2 Manifest Lists.

```python
@dataclass
class ImageIndex:
    schema_version: int                            # Wire key: "schemaVersion" (always 2)
    media_type: str                                # Wire key: "mediaType"
    manifests: list[Descriptor]                    # Wire key: "manifests"
    subject: Optional[Descriptor] = None           # Wire key: "subject"
    annotations: Optional[dict[str, str]] = None   # Wire key: "annotations"
    artifact_type: Optional[str] = None            # Wire key: "artifactType"
```

**Validation rules:**

| Field | Rule | Error |
|-------|------|-------|
| `schema_version` | Must equal `2` | `ValueError` |
| `media_type` | Must be one of the known index media types | `ValueError` |
| `manifests` | Must be a list (may be empty) | `ValueError` |

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_json` | `() -> str` | Canonical JSON: `sort_keys=True`, no extra whitespace |
| `digest` | `() -> str` | `"sha256:" + sha256(to_json().encode("utf-8")).hexdigest()` |
| `from_json` | `(cls, data: str) -> ImageIndex` | Deserializes from JSON string |

**Notes:**

- `subject` enables an index itself to be a referrer attached to another
  manifest (e.g. a multi-platform SBOM index).
- Each entry in `manifests` is a `Descriptor` with an optional `platform`
  field; OCI Image Indexes use `platform`, while referrer response indexes use
  `artifactType` instead.

---

### `parse_manifest()` factory

```python
def parse_manifest(data: str) -> ImageManifest | ImageIndex:
    """Parse a manifest JSON string into the correct Python type.

    Dispatches on the ``mediaType`` field. Supports OCI Image Manifests, OCI
    Image Indexes, Docker V2 Manifests, and Docker V2 Manifest Lists.

    :param data: Raw JSON string from a manifest GET response.
    :returns: ``ImageManifest`` or ``ImageIndex`` instance.
    :raises ManifestError: If ``mediaType`` is unknown or the JSON is malformed.
    """
```

**Dispatch table:**

| `mediaType` value | Returned type |
|---|---|
| `application/vnd.oci.image.manifest.v1+json` | `ImageManifest` |
| `application/vnd.docker.distribution.manifest.v2+json` | `ImageManifest` |
| `application/vnd.oci.image.index.v1+json` | `ImageIndex` |
| `application/vnd.docker.distribution.manifest.list.v2+json` | `ImageIndex` |
| anything else | raises `ManifestError` |

---

## JSON Wire Format

### OCI Image Manifest

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.example.sbom.v1",
  "config": {
    "mediaType": "application/vnd.oci.image.config.v1+json",
    "digest": "sha256:44136fa355ba77b9ad7b35f...",
    "size": 2
  },
  "layers": [
    {
      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:abc123...",
      "size": 5678,
      "annotations": { "org.opencontainers.image.title": "layer0" }
    }
  ],
  "subject": {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "digest": "sha256:def456...",
    "size": 1234
  },
  "annotations": { "org.opencontainers.image.created": "2026-02-25T00:00:00Z" }
}
```

### OCI Image Index

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:abc...",
      "size": 1234,
      "platform": {
        "architecture": "amd64",
        "os": "linux"
      }
    },
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:def...",
      "size": 1235,
      "platform": {
        "architecture": "arm64",
        "os": "linux",
        "variant": "v8"
      }
    }
  ],
  "annotations": {}
}
```

---

## Field-to-Wire-Key Mapping

| Python field | JSON wire key | Notes |
|---|---|---|
| `schema_version` | `schemaVersion` | |
| `media_type` | `mediaType` | |
| `artifact_type` | `artifactType` | |
| `os_version` | `os.version` | Literal dot in key |
| `os_features` | `os.features` | Literal dot in key |

All `Optional` fields are omitted from the serialized output when `None`
(i.e., `to_dict()` / `to_json()` do not emit `null` values).

---

## Error Handling

| Condition | Error Type | Behaviour |
|---|---|---|
| Unknown `mediaType` in `parse_manifest` | `ManifestError` | Raised with message including the unknown media type string |
| Malformed JSON in `parse_manifest` / `from_json` | `ManifestError` | Wraps `json.JSONDecodeError` |
| Missing required field in `from_dict` / `from_json` | `ManifestError` | Raised with field name |
| Invalid `digest` format in `Descriptor` | `ValueError` | Raised on construction |
| `schema_version != 2` | `ValueError` | Raised on construction |

`ManifestError` is a new subclass of `RegShapeError` added to `libs/errors.py`.

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `RegShapeError` base class for `ManifestError`
- `regshape.libs.models.mediatype` — media type constants used in dispatch and
  validation

**External (stdlib only):**
- `json` — serialization/deserialization
- `hashlib` — SHA-256 digest computation
- `dataclasses` — `@dataclass`, `field`
- `typing` — `Optional`

No third-party dependencies.

---

## Open Questions

- [ ] Should `Descriptor.from_dict` raise `ManifestError` (to be consistent
      with manifest parsing) or `ValueError`? Current proposal: `ValueError`
      for field-level validation (wrong type, bad format) and `ManifestError`
      when the surrounding parsing context is a manifest.
- [ ] Should `ImageManifest` validate that `config.media_type` is a known
      config media type, or leave that to a higher-level linting layer?
      Current proposal: no validation on `config.media_type` — let the registry
      reject invalid configs.

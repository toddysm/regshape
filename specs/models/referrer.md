# Data Model: Referrer

## Overview

This spec defines the data model for OCI referrers responses in
`src/regshape/libs/models/referrer.py`.

The OCI Referrers API returns an Image Index whose `manifests` array
contains descriptors for all manifests that reference a given subject
digest via their `subject` field. Each descriptor carries an
`artifactType` and optional `annotations` that describe the type
of supply-chain artifact (SBOM, signature, attestation, etc.).

The response is an OCI Image Index with `schemaVersion: 2` and
`mediaType: application/vnd.oci.image.index.v1+json`. The `manifests`
array uses standard OCI content descriptors, each of which may include
`artifactType` and `annotations`.

---

## Module Structure

```
src/regshape/libs/models/
├── __init__.py        # Updated: exports ReferrerList
├── blob.py
├── catalog.py
├── descriptor.py      # Existing: Descriptor, Platform (reused)
├── error.py
├── manifest.py
├── mediatype.py
├── referrer.py        # NEW: ReferrerList
└── tags.py
```

`ReferrerError` is added to `src/regshape/libs/errors.py`, parallel to
`TagError`, `BlobError`, and `CatalogError`.

---

## Data Models

### `ReferrerList`

Represents the response body of `GET /v2/<name>/referrers/<digest>`.

```python
@dataclass
class ReferrerList:
    manifests: list[Descriptor]  # Wire key: "manifests"
```

#### Field notes

- **`manifests`** is a list of OCI content descriptors, each representing a
  manifest that has a `subject` field pointing to the queried digest. The
  existing `Descriptor` dataclass already supports the `artifact_type` and
  `annotations` fields needed for referrer entries.
- A registry MAY return an empty `manifests` array (`[]`) when the subject
  has no referrers. `from_dict` normalises `null` or missing `"manifests"`
  to `[]`.
- Each descriptor in the `manifests` array typically includes:
  - `mediaType` — usually `application/vnd.oci.image.manifest.v1+json`
  - `digest` — digest of the referring manifest
  - `size` — size of the referring manifest
  - `artifactType` — media type describing the artifact kind
  - `annotations` — optional key-value metadata

#### Validation rules (`__post_init__`)

| Field | Rule | Error |
|-------|------|-------|
| `manifests` | Must be a `list` | `ValueError` |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> dict` | Serializes to OCI wire dict (Image Index format) |
| `to_json` | `() -> str` | Canonical JSON (`sort_keys=True`, compact separators) |
| `from_dict` | `(cls, data: object) -> ReferrerList` | Deserializes from wire dict; normalises `null`/missing manifests |
| `from_json` | `(cls, data: str) -> ReferrerList` | Deserializes from raw JSON string |
| `filter_by_artifact_type` | `(self, artifact_type: str) -> ReferrerList` | Returns a new `ReferrerList` containing only descriptors matching the given artifact type |
| `merge` | `(self, other: ReferrerList) -> ReferrerList` | Returns a new `ReferrerList` with manifests from both lists concatenated (used for pagination accumulation) |

---

## JSON Wire Format

### Normal response (with referrers)

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:a1b2c3d4e5f6...",
      "size": 1234,
      "artifactType": "application/vnd.example.sbom.v1",
      "annotations": {
        "org.opencontainers.image.created": "2025-06-15T10:30:00Z"
      }
    },
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:f6e5d4c3b2a1...",
      "size": 567,
      "artifactType": "application/vnd.cncf.notary.signature",
      "annotations": {}
    }
  ]
}
```

### Empty response (no referrers)

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": []
}
```

---

## Serialization

### `to_dict`

Always emits the full Image Index envelope:

```python
{
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [descriptor.to_dict() for descriptor in self.manifests],
}
```

### `from_dict`

1. Validate that *data* is a `dict`; raise `ReferrerError` otherwise.
2. Extract `"manifests"` key; normalise `null` / missing to `[]`.
3. Deserialize each entry via `Descriptor.from_dict()`.
4. Return `ReferrerList(manifests=descriptors)`.

The method intentionally ignores `schemaVersion` and `mediaType` from the
response body — these are fixed values for the referrers endpoint and the
OCI spec does not define meaningful variations to branch on.

### `from_json`

1. Parse JSON string via `json.loads()`.
2. Delegate to `from_dict`.
3. Wrap `json.JSONDecodeError` in `ReferrerError`.

---

## Relationships

- **`Descriptor`** (existing): Each entry in `manifests` is deserialized as
  an existing `Descriptor` instance. The `artifact_type` and `annotations`
  fields on `Descriptor` carry the referrer-specific metadata.
- **`ImageIndex`** (existing): The wire format is an OCI Image Index, but
  `ReferrerList` is a distinct type because:
  - It is read-only (registries generate it; clients never push it).
  - It does not carry `platform` data on descriptors.
  - It benefits from a `filter_by_artifact_type` method.
  - Keeping it separate avoids overloading `ImageIndex` with referrer-specific logic.

---

## Error Types

### `ReferrerError`

Added to `src/regshape/libs/errors.py`:

```python
class ReferrerError(RegShapeError):
    """
    Error caused by a malformed or unprocessable referrers response.
    """
    pass
```

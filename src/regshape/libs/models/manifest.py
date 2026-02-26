#!/usr/bin/env python3

"""
:mod:`manifest` - OCI ImageManifest and ImageIndex data models
==============================================================

    module:: manifest
    :platform: Unix, Windows
    :synopsis: Dataclasses for OCI Image Manifests, Image Indexes, and a
               factory function that dispatches on mediaType.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Union

from regshape.libs.errors import ManifestError
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.models.mediatype import (
    INDEX_MEDIA_TYPES,
    MANIFEST_MEDIA_TYPES,
)


@dataclass
class ImageManifest:
    """OCI Image Manifest (``schemaVersion`` 2).

    Also used to represent Docker V2 Manifests — the distinction is carried
    by :attr:`media_type`.

    :param schema_version: Schema version; must be ``2``.
    :param media_type: Manifest media type.
    :param config: Config :class:`~regshape.libs.models.descriptor.Descriptor`.
    :param layers: Ordered list of layer
        :class:`~regshape.libs.models.descriptor.Descriptor` objects.
    :param subject: Optional subject descriptor for referrer manifests.
    :param annotations: Optional free-form string annotations.
    :param artifact_type: Optional artifact type for OCI artifact manifests.
    """

    schema_version: int
    media_type: str
    config: Descriptor
    layers: list[Descriptor]
    subject: Optional[Descriptor] = None
    annotations: Optional[dict[str, str]] = None
    artifact_type: Optional[str] = None

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError(
                f"ImageManifest.schema_version must be 2, got {self.schema_version}"
            )
        if not self.media_type:
            raise ValueError("ImageManifest.media_type must not be empty")
        if self.media_type not in MANIFEST_MEDIA_TYPES:
            raise ValueError(
                f"ImageManifest.media_type must be one of {sorted(MANIFEST_MEDIA_TYPES)}, "
                f"got {self.media_type!r}"
            )
        if not isinstance(self.layers, list):
            raise ValueError("ImageManifest.layers must be a list")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _to_dict(self) -> dict:
        d: dict = {
            "schemaVersion": self.schema_version,
            "mediaType": self.media_type,
            "config": self.config.to_dict(),
            "layers": [layer.to_dict() for layer in self.layers],
        }
        if self.artifact_type is not None:
            d["artifactType"] = self.artifact_type
        if self.subject is not None:
            d["subject"] = self.subject.to_dict()
        if self.annotations is not None:
            d["annotations"] = self.annotations
        return d

    def to_json(self) -> str:
        """Serialize to canonical JSON string.

        Uses ``sort_keys=True`` and compact separators so that
        :meth:`digest` is deterministic.

        :returns: Canonical JSON string.
        """
        return json.dumps(self._to_dict(), separators=(",", ":"), sort_keys=True)

    def digest(self) -> str:
        """Compute the SHA-256 content digest of this manifest.

        The digest is derived from the canonical JSON representation produced
        by :meth:`to_json`.

        :returns: Digest string in ``"sha256:<hex>"`` form.
        """
        raw = self.to_json().encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    @classmethod
    def from_json(cls, data: str) -> "ImageManifest":
        """Deserialize an :class:`ImageManifest` from a JSON string.

        :param data: Raw JSON string (e.g. from a manifest GET response body).
        :returns: An :class:`ImageManifest` instance.
        :raises ManifestError: If the JSON is malformed or required fields are
            missing.
        """
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                "Failed to parse manifest JSON", str(exc)
            ) from exc
        if not isinstance(obj, dict):
            raise ManifestError(
                "Invalid manifest JSON: expected a JSON object at top level",
                f"Got {type(obj).__name__}",
            )
        return cls._from_dict(obj)

    @classmethod
    def _from_dict(cls, data: dict) -> "ImageManifest":
        """Deserialize from a parsed dict.

        :param data: Parsed JSON dict.
        :returns: An :class:`ImageManifest` instance.
        :raises ManifestError: If required fields are missing or invalid.
        """
        try:
            subject_data = data.get("subject")
            return cls(
                schema_version=data["schemaVersion"],
                media_type=data["mediaType"],
                config=Descriptor.from_dict(data["config"]),
                layers=[Descriptor.from_dict(layer) for layer in data["layers"]],
                subject=Descriptor.from_dict(subject_data) if subject_data is not None else None,
                annotations=data.get("annotations"),
                artifact_type=data.get("artifactType"),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ManifestError(
                "Invalid ImageManifest", str(exc)
            ) from exc


@dataclass
class ImageIndex:
    """OCI Image Index (``schemaVersion`` 2).

    Used as a multi-architecture manifest list and as the response body of the
    OCI referrers API. Also represents Docker V2 Manifest Lists.

    :param schema_version: Schema version; must be ``2``.
    :param media_type: Index media type.
    :param manifests: List of manifest
        :class:`~regshape.libs.models.descriptor.Descriptor` objects. Each
        entry may carry an optional :attr:`~Descriptor.platform` field.
    :param subject: Optional subject descriptor; set when the index itself is
        a referrer attached to another manifest.
    :param annotations: Optional free-form string annotations.
    :param artifact_type: Optional artifact type.
    """

    schema_version: int
    media_type: str
    manifests: list[Descriptor]
    subject: Optional[Descriptor] = None
    annotations: Optional[dict[str, str]] = None
    artifact_type: Optional[str] = None

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError(
                f"ImageIndex.schema_version must be 2, got {self.schema_version}"
            )
        if not self.media_type:
            raise ValueError("ImageIndex.media_type must not be empty")
        if self.media_type not in INDEX_MEDIA_TYPES:
            raise ValueError(
                f"ImageIndex.media_type must be one of {sorted(INDEX_MEDIA_TYPES)}, got {self.media_type!r}"
            )
        if not isinstance(self.manifests, list):
            raise ValueError("ImageIndex.manifests must be a list")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _to_dict(self) -> dict:
        d: dict = {
            "schemaVersion": self.schema_version,
            "mediaType": self.media_type,
            "manifests": [m.to_dict() for m in self.manifests],
        }
        if self.artifact_type is not None:
            d["artifactType"] = self.artifact_type
        if self.subject is not None:
            d["subject"] = self.subject.to_dict()
        if self.annotations is not None:
            d["annotations"] = self.annotations
        return d

    def to_json(self) -> str:
        """Serialize to canonical JSON string.

        Uses ``sort_keys=True`` and compact separators so that
        :meth:`digest` is deterministic.

        :returns: Canonical JSON string.
        """
        return json.dumps(self._to_dict(), separators=(",", ":"), sort_keys=True)

    def digest(self) -> str:
        """Compute the SHA-256 content digest of this index.

        The digest is derived from the canonical JSON representation produced
        by :meth:`to_json`.

        :returns: Digest string in ``"sha256:<hex>"`` form.
        """
        raw = self.to_json().encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    @classmethod
    def from_json(cls, data: str) -> "ImageIndex":
        """Deserialize an :class:`ImageIndex` from a JSON string.

        :param data: Raw JSON string (e.g. from a manifest GET response body).
        :returns: An :class:`ImageIndex` instance.
        :raises ManifestError: If the JSON is malformed or required fields are
            missing.
        """
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                "Failed to parse index JSON", str(exc)
            ) from exc
        return cls._from_dict(obj)

    @classmethod
    def _from_dict(cls, data: dict) -> "ImageIndex":
        """Deserialize from a parsed dict.

        :param data: Parsed JSON dict.
        :returns: An :class:`ImageIndex` instance.
        :raises ManifestError: If required fields are missing or invalid.
        """
        try:
            subject_data = data.get("subject")
            manifests_data = data["manifests"]
            if not isinstance(manifests_data, list):
                raise ValueError("'manifests' must be a list")
            return cls(
                schema_version=data["schemaVersion"],
                media_type=data["mediaType"],
                manifests=[Descriptor.from_dict(m) for m in manifests_data],
                subject=Descriptor.from_dict(subject_data) if subject_data is not None else None,
                annotations=data.get("annotations"),
                artifact_type=data.get("artifactType"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(
                "Invalid ImageIndex", str(exc)
            ) from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def parse_manifest(data: str) -> Union[ImageManifest, ImageIndex]:
    """Parse a manifest JSON string into the correct Python type.

    Dispatches on the ``mediaType`` field. Supports OCI Image Manifests, OCI
    Image Indexes, Docker V2 Manifests, and Docker V2 Manifest Lists.

    :param data: Raw JSON string from a manifest GET response body.
    :returns: An :class:`ImageManifest` or :class:`ImageIndex` instance.
    :raises ManifestError: If ``mediaType`` is unknown or the JSON is malformed.
    """
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ManifestError("Failed to parse manifest JSON", str(exc)) from exc

    media_type = obj.get("mediaType", "")

    if media_type in MANIFEST_MEDIA_TYPES:
        return ImageManifest._from_dict(obj)
    if media_type in INDEX_MEDIA_TYPES:
        return ImageIndex._from_dict(obj)

    raise ManifestError(
        "Unknown manifest mediaType",
        f"got {media_type!r}; expected one of {sorted(MANIFEST_MEDIA_TYPES | INDEX_MEDIA_TYPES)}",
    )

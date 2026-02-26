#!/usr/bin/env python3

"""
:mod:`descriptor` - OCI Descriptor and Platform data models
===========================================================

    module:: descriptor
    :platform: Unix, Windows
    :synopsis: Dataclasses for the OCI content Descriptor and Platform types
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import re
from dataclasses import dataclass
from typing import Optional

# Regex for valid OCI digest strings (algorithm:hex) with strict length checks.
_DIGEST_RE = re.compile(r'^(?:sha256:[a-f0-9]{64}|sha512:[a-f0-9]{128})$')


@dataclass
class Platform:
    """OCI platform specification for multi-arch manifest index entries.

    :param architecture: CPU architecture (e.g. ``amd64``, ``arm64``).
    :param os: Operating system (e.g. ``linux``, ``windows``).
    :param os_version: Optional OS version string.
    :param os_features: Optional list of required OS features.
    :param variant: Optional CPU variant (e.g. ``v8`` for arm64).
    """

    architecture: str
    os: str
    os_version: Optional[str] = None
    os_features: Optional[list[str]] = None
    variant: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.architecture:
            raise ValueError("Platform.architecture must not be empty")
        if not self.os:
            raise ValueError("Platform.os must not be empty")

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict.

        ``None`` fields are omitted. Note that ``os_version`` and
        ``os_features`` map to the literal JSON keys ``"os.version"`` and
        ``"os.features"`` respectively.

        :returns: Dict ready for JSON serialization.
        """
        d: dict = {
            "architecture": self.architecture,
            "os": self.os,
        }
        if self.os_version is not None:
            d["os.version"] = self.os_version
        if self.os_features is not None:
            d["os.features"] = self.os_features
        if self.variant is not None:
            d["variant"] = self.variant
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Platform":
        """Deserialize from an OCI wire-format dict.

        :param data: Dict parsed from JSON.
        :returns: A :class:`Platform` instance.
        :raises ValueError: If required fields are missing or the input is not a dict.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"Platform: expected a dict, got {type(data).__name__!r}"
            )
        try:
            return cls(
                architecture=data["architecture"],
                os=data["os"],
                os_version=data.get("os.version"),
                os_features=data.get("os.features"),
                variant=data.get("variant"),
            )
        except KeyError as exc:
            raise ValueError(f"Platform: missing required field {exc}") from exc


@dataclass
class Descriptor:
    """OCI content descriptor.

    The fundamental building block of the OCI content model. Every piece of
    content referenced in a manifest (config, layer, manifest entry, subject,
    referrer) is expressed as a :class:`Descriptor`.

    :param media_type: Media type of the referenced content.
    :param digest: Content digest in ``algorithm:hex`` form (e.g.
        ``sha256:abc123...``).
    :param size: Size of the referenced content in bytes.
    :param platform: Optional platform; set only on manifest entries inside an
        :class:`~regshape.libs.models.manifest.ImageIndex`.
    :param annotations: Optional free-form string annotations.
    :param artifact_type: Optional artifact media type (OCI artifact manifests).
    :param urls: Optional list of external URLs from which the content can be
        fetched.
    """

    media_type: str
    digest: str
    size: int
    platform: Optional[Platform] = None
    annotations: Optional[dict[str, str]] = None
    artifact_type: Optional[str] = None
    urls: Optional[list[str]] = None

    def __post_init__(self) -> None:
        if not self.media_type:
            raise ValueError("Descriptor.media_type must not be empty")
        if not _DIGEST_RE.match(self.digest):
            raise ValueError(
                f"Descriptor.digest has invalid format: {self.digest!r}; "
                "expected 'sha256:<hex>' or 'sha512:<hex>'"
            )
        if self.size < 0:
            raise ValueError(
                f"Descriptor.size must be >= 0, got {self.size}"
            )

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict.

        ``None`` fields are omitted so the output matches the minimal OCI wire
        representation.

        :returns: Dict ready for JSON serialization.
        """
        d: dict = {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }
        if self.platform is not None:
            d["platform"] = self.platform.to_dict()
        if self.annotations is not None:
            d["annotations"] = self.annotations
        if self.artifact_type is not None:
            d["artifactType"] = self.artifact_type
        if self.urls is not None:
            d["urls"] = self.urls
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Descriptor":
        """Deserialize from an OCI wire-format dict.

        :param data: Dict parsed from JSON.
        :returns: A :class:`Descriptor` instance.
        :raises ValueError: If required fields are missing or invalid.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"Descriptor: expected a dict, got {type(data).__name__!r}"
            )
        try:
            platform_data = data.get("platform")
            raw_size = data["size"]
            try:
                size = int(raw_size)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Descriptor: 'size' must be an integer, got {raw_size!r}"
                ) from exc
            return cls(
                media_type=data["mediaType"],
                digest=data["digest"],
                size=size,
                platform=Platform.from_dict(platform_data) if platform_data else None,
                annotations=data.get("annotations"),
                artifact_type=data.get("artifactType"),
                urls=data.get("urls"),
            )
        except KeyError as exc:
            raise ValueError(f"Descriptor: missing required field {exc}") from exc

#!/usr/bin/env python3

"""
:mod:`regshape.libs.models.referrer` - OCI ReferrerList data model
===================================================================

.. module:: regshape.libs.models.referrer
   :platform: Unix, Windows
   :synopsis: Dataclass for the OCI referrers response returned by
              ``GET /v2/<name>/referrers/<digest>``.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
from dataclasses import dataclass

from regshape.libs.errors import ReferrerError
from regshape.libs.models.descriptor import Descriptor


@dataclass
class ReferrerList:
    """OCI referrers response.

    Represents the JSON body returned by ``GET /v2/<name>/referrers/<digest>``.
    The wire format is an OCI Image Index with ``schemaVersion: 2`` and
    ``mediaType: application/vnd.oci.image.index.v1+json``.

    :param manifests: List of :class:`~regshape.libs.models.descriptor.Descriptor`
        instances, each representing a manifest that has a ``subject`` field
        pointing to the queried digest.
    """

    manifests: list[Descriptor]

    def __post_init__(self) -> None:
        if not isinstance(self.manifests, list):
            raise ValueError("ReferrerList.manifests must be a list")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict (Image Index envelope).

        Always emits ``"manifests": []`` rather than omitting the key when
        the list is empty.

        :returns: Dict ready for JSON serialization.
        """
        return {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [d.to_dict() for d in self.manifests],
        }

    def to_json(self) -> str:
        """Serialize to a canonical JSON string.

        Uses ``sort_keys=True`` and compact separators for deterministic
        output.

        :returns: Canonical JSON string.
        """
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: object) -> "ReferrerList":
        """Deserialize from an OCI wire-format dict.

        Normalises ``"manifests": null`` and a missing ``"manifests"`` key
        to an empty list.  The ``schemaVersion`` and ``mediaType`` fields
        are intentionally ignored — they are fixed values for the referrers
        endpoint.

        :param data: Dict parsed from a referrers JSON response.
        :returns: A :class:`ReferrerList` instance.
        :raises ReferrerError: If *data* is not a dict or descriptor
            deserialization fails.
        """
        if not isinstance(data, dict):
            raise ReferrerError(
                "Invalid referrers response",
                f"expected a dict, got {type(data).__name__!r}",
            )
        raw_manifests = data.get("manifests")
        if raw_manifests is None:
            manifests: list[Descriptor] = []
        else:
            if not isinstance(raw_manifests, list):
                raise ReferrerError(
                    "Invalid referrers response",
                    f"'manifests' must be a list, got {type(raw_manifests).__name__!r}",
                )
            try:
                manifests = [Descriptor.from_dict(entry) for entry in raw_manifests]
            except (ValueError, TypeError) as exc:
                raise ReferrerError(
                    "Invalid referrers response",
                    f"failed to parse descriptor: {exc}",
                ) from exc
        return cls(manifests=manifests)

    @classmethod
    def from_json(cls, data: str) -> "ReferrerList":
        """Deserialize from a raw JSON string.

        :param data: Raw JSON string from a ``GET /v2/<name>/referrers/<digest>``
            response body.
        :returns: A :class:`ReferrerList` instance.
        :raises ReferrerError: If the JSON is malformed or required fields are
            missing.
        """
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ReferrerError(
                "Failed to parse referrers JSON", str(exc)
            ) from exc
        return cls.from_dict(obj)

    # ------------------------------------------------------------------
    # Filtering and merging
    # ------------------------------------------------------------------

    def filter_by_artifact_type(self, artifact_type: str) -> "ReferrerList":
        """Return a new :class:`ReferrerList` containing only descriptors
        whose :attr:`~Descriptor.artifact_type` matches *artifact_type*.

        :param artifact_type: The artifact type media type to filter on.
        :returns: A new :class:`ReferrerList` with matching descriptors only.
        """
        return ReferrerList(
            manifests=[
                d for d in self.manifests if d.artifact_type == artifact_type
            ]
        )

    def merge(self, other: "ReferrerList") -> "ReferrerList":
        """Return a new :class:`ReferrerList` with manifests from both lists
        concatenated.

        Used during pagination to accumulate results across pages.

        :param other: Another :class:`ReferrerList` to merge with.
        :returns: A new :class:`ReferrerList` with combined manifests.
        """
        return ReferrerList(manifests=self.manifests + other.manifests)

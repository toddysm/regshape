#!/usr/bin/env python3

"""
:mod:`regshape.libs.models.tags` - OCI TagList data model
==========================================================

.. module:: regshape.libs.models.tags
   :platform: Unix, Windows
   :synopsis: Dataclass for the OCI tag-list response returned by
              ``GET /v2/<name>/tags/list``.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
from dataclasses import dataclass

from regshape.libs.errors import TagError


@dataclass
class TagList:
    """OCI tag-list response.

    Represents the JSON body returned by ``GET /v2/<name>/tags/list``.

    The OCI Distribution Spec uses *name*, *namespace*, *repository*, and
    *repo* interchangeably to refer to the repository identifier carried in
    the ``"name"`` wire key. This implementation uses :attr:`namespace` as
    the most precise match to the spec's own language ("the namespace of the
    repository").

    :param namespace: Repository namespace as returned in the ``"name"`` wire
        field (e.g. ``"myrepo/myimage"``).
    :param tags: Ordered list of tag strings. The OCI spec requires tags to be
        in lexical ("ASCIIbetical") order; this class preserves the order as
        received without re-sorting. Normalised from ``null`` to ``[]`` during
        deserialization.
    """

    namespace: str
    tags: list[str]

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("TagList.namespace must not be empty")
        if not isinstance(self.tags, list):
            raise ValueError("TagList.tags must be a list")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict.

        Always emits ``"tags": []`` rather than omitting the key when the
        list is empty, matching the wire format for a repository with no tags.

        :returns: Dict ready for JSON serialization.
        """
        return {
            "name": self.namespace,
            "tags": self.tags,
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
    def from_dict(cls, data: object) -> "TagList":
        """Deserialize from an OCI wire-format dict.

        Normalises ``"tags": null`` and a missing ``"tags"`` key to an empty
        list, as permitted by the OCI Distribution Spec for the last
        pagination page.

        :param data: Dict parsed from a tag-list JSON response. The method
            accepts ``object`` so that callers can pass arbitrary parsed JSON
            values; a :exc:`TagError` is raised if *data* is not a ``dict``.
        :returns: A :class:`TagList` instance.
        :raises TagError: If *data* is not a dict or the ``"name"`` field is
            missing.
        :raises ValueError: If field-level validation fails (e.g. empty
            namespace, non-list tags after normalisation).
        """
        if not isinstance(data, dict):
            raise TagError(
                "Invalid tag-list response",
                f"expected a dict, got {type(data).__name__!r}",
            )
        try:
            namespace = data["name"]
        except KeyError as exc:
            raise TagError(
                "Invalid tag-list response",
                f"missing required field {exc}",
            ) from exc
        # Normalise null / missing "tags" to an empty list; otherwise pass
        # through the raw value so __post_init__ can validate its type.
        if "tags" not in data or data.get("tags") is None:
            tags: list[str] = []
        else:
            tags = data["tags"]  # type: ignore[assignment]
        return cls(namespace=namespace, tags=tags)

    @classmethod
    def from_json(cls, data: str) -> "TagList":
        """Deserialize from a raw JSON string.

        :param data: Raw JSON string from a ``GET /v2/<name>/tags/list``
            response body.
        :returns: A :class:`TagList` instance.
        :raises TagError: If the JSON is malformed or required fields are
            missing.
        """
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TagError(
                "Failed to parse tag-list JSON", str(exc)
            ) from exc
        return cls.from_dict(obj)

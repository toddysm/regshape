#!/usr/bin/env python3

"""
:mod:`regshape.libs.models.catalog` - OCI RepositoryCatalog data model
=======================================================================

.. module:: regshape.libs.models.catalog
   :platform: Unix, Windows
   :synopsis: Dataclass for the OCI repository-catalog response returned by
              ``GET /v2/_catalog``.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
from dataclasses import dataclass

from regshape.libs.errors import CatalogError


@dataclass
class RepositoryCatalog:
    """OCI repository-catalog response.

    Represents the JSON body returned by ``GET /v2/_catalog``.

    The catalog endpoint is registry-scoped, not repository-scoped, so no
    namespace or name field is present in the response or in this model.

    :param repositories: Ordered list of repository name strings as returned
        by the registry (e.g. ``["library/ubuntu", "myrepo/myimage"]``). The
        order is preserved without re-sorting. Normalised from ``null`` or a
        missing key to ``[]`` during deserialization.
    """

    repositories: list[str]

    def __post_init__(self) -> None:
        if not isinstance(self.repositories, list):
            raise ValueError("RepositoryCatalog.repositories must be a list")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict.

        Always emits ``"repositories": []`` rather than omitting the key when
        the list is empty, consistent with the wire format and how
        :class:`~regshape.libs.models.tags.TagList` handles empty tag lists.

        :returns: Dict ready for JSON serialization.
        """
        return {"repositories": self.repositories}

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
    def from_dict(cls, data: object) -> "RepositoryCatalog":
        """Deserialize from an OCI wire-format dict.

        Normalises ``"repositories": null`` and a missing ``"repositories"``
        key to an empty list, as registries commonly return ``null`` on the
        last pagination page.

        :param data: Dict parsed from a catalog JSON response. The method
            accepts ``object`` so that callers can pass arbitrary parsed JSON
            values; a :exc:`CatalogError` is raised if *data* is not a
            ``dict``.
        :returns: A :class:`RepositoryCatalog` instance.
        :raises CatalogError: If *data* is not a dict.
        :raises ValueError: If field-level validation fails (e.g. non-list
            ``repositories`` after null-normalisation).
        """
        if not isinstance(data, dict):
            raise CatalogError(
                "Invalid catalog response",
                f"expected a dict, got {type(data).__name__!r}",
            )
        # Normalise null / missing "repositories" to an empty list; otherwise
        # pass through the raw value so __post_init__ can validate its type.
        if "repositories" not in data or data.get("repositories") is None:
            repositories: list[str] = []
        else:
            repositories = data["repositories"]  # type: ignore[assignment]
        return cls(repositories=repositories)

    @classmethod
    def from_json(cls, data: str) -> "RepositoryCatalog":
        """Deserialize from a raw JSON string.

        :param data: Raw JSON string from a ``GET /v2/_catalog`` response body.
        :returns: A :class:`RepositoryCatalog` instance.
        :raises CatalogError: If the JSON is malformed or the response
            structure is invalid.
        """
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CatalogError(
                "Failed to parse catalog JSON", str(exc)
            ) from exc
        return cls.from_dict(obj)

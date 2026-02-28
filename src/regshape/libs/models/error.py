#!/usr/bin/env python3

"""
:mod:`regshape.libs.models.error` - OCI error response data models
===================================================================

.. module:: regshape.libs.models.error
   :platform: Unix, Windows
   :synopsis: Dataclasses for the OCI error envelope returned by registries
              on non-2xx responses.

              ``OciErrorDetail``    -- a single entry in the ``"errors"`` array
              ``OciErrorResponse``  -- the full ``{"errors": [...]}`` envelope

              :meth:`OciErrorResponse.from_response` is the primary integration
              point for CLI code: it absorbs all parse failures and always
              returns a model instance, so callers can be written as::

                  detail = OciErrorResponse.from_response(response).first_detail()

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests


@dataclass
class OciErrorDetail:
    """A single error entry from an OCI error response body.

    :param code: OCI error code token (e.g. ``"MANIFEST_UNKNOWN"``). The spec
        defines a set of standard codes but registries may use vendor-specific
        values. Defaults to ``""`` for non-conformant responses that omit the
        field.
    :param message: Human-readable explanation of the error. Defaults to
        ``""`` when absent.
    :param detail: Opaque additional context. The OCI spec leaves the type
        intentionally open — it may be a ``dict``, ``list``, ``str``, or
        ``None``. Stored as-is; never interpreted or validated.
    """

    code: str
    message: str
    detail: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise ValueError(
                f"OciErrorDetail.code must be a str, got {type(self.code).__name__!r}"
            )
        if not isinstance(self.message, str):
            raise ValueError(
                f"OciErrorDetail.message must be a str, got {type(self.message).__name__!r}"
            )

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format(self) -> str:
        """Return a concise human-readable summary of this error.

        Returns ``"CODE: message"`` when both fields are non-empty.  Falls
        back to whichever field is non-empty, or ``""`` when both are empty.

        :returns: Formatted error string.
        """
        if self.code and self.message:
            return f"{self.code}: {self.message}"
        return self.code or self.message

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict.

        Omits the ``"detail"`` key when :attr:`detail` is ``None``.

        :returns: Dict ready for JSON serialization.
        """
        d: dict = {"code": self.code, "message": self.message}
        if self.detail is not None:
            d["detail"] = self.detail
        return d

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: object) -> "OciErrorDetail":
        """Deserialize from a wire-format dict.

        Missing or ``None`` values for ``"code"`` and ``"message"`` are
        tolerated and normalised to ``""`` to handle non-conformant
        registries.

        :param data: A dict parsed from an OCI error JSON body.
        :returns: An :class:`OciErrorDetail` instance.
        :raises ValueError: If *data* is not a ``dict``.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"OciErrorDetail.from_dict: expected a dict, got {type(data).__name__!r}"
            )
        code = data.get("code") or ""
        message = data.get("message") or ""
        detail = data.get("detail", None)
        return cls(code=code, message=message, detail=detail)


@dataclass
class OciErrorResponse:
    """Parsed OCI error response envelope.

    Represents the ``{"errors": [...]}`` JSON body that conformant registries
    return for all non-2xx responses.

    :param errors: List of :class:`OciErrorDetail` entries.  Always a list;
        never ``None``.  An empty list indicates either an empty response body,
        a non-OCI error format, or a fully malformed body.
    """

    errors: list[OciErrorDetail] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def first_detail(self) -> str:
        """Return a formatted summary of the first error.

        This is the primary extraction point used by all CLI raise-helpers::

            detail = OciErrorResponse.from_response(response).first_detail()

        :returns: ``errors[0].format()`` when :attr:`errors` is non-empty,
            ``""`` otherwise.
        """
        return self.errors[0].format() if self.errors else ""

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to an OCI wire-format dict.

        :returns: Dict ready for JSON serialization.
        """
        return {"errors": [e.to_dict() for e in self.errors]}

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
    def from_dict(cls, data: object) -> "OciErrorResponse":
        """Deserialize from a wire-format dict.

        A missing or ``null`` ``"errors"`` key normalises to an empty list.
        Malformed individual entries are skipped rather than aborting the
        entire parse so that partial error information is still surfaced.

        :param data: Dict parsed from an OCI error JSON body.
        :returns: An :class:`OciErrorResponse` instance.
        :raises ValueError: If *data* is not a ``dict``.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"OciErrorResponse.from_dict: expected a dict, got {type(data).__name__!r}"
            )
        raw_errors = data.get("errors") or []
        errors: list[OciErrorDetail] = []
        for entry in raw_errors:
            try:
                errors.append(OciErrorDetail.from_dict(entry))
            except (ValueError, TypeError):
                # Malformed entry — skip it, preserve the rest
                continue
        return cls(errors=errors)

    @classmethod
    def from_json(cls, data: str) -> "OciErrorResponse":
        """Deserialize from a raw JSON string.

        :param data: JSON string of an OCI error response body.
        :returns: An :class:`OciErrorResponse` instance.
        :raises ValueError: If *data* is not valid JSON.
        """
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OciErrorResponse.from_json: invalid JSON — {exc}"
            ) from exc
        return cls.from_dict(parsed)

    @classmethod
    def from_response(cls, response: "requests.Response") -> "OciErrorResponse":
        """Parse the OCI error body from an HTTP response.

        **Never raises.**  Returns ``OciErrorResponse(errors=[])`` for any of
        the following failure modes so that callers can be written as a single
        expression:

        .. code-block:: python

            detail = OciErrorResponse.from_response(response).first_detail()

        Failure modes that return an empty instance:

        - Response body is empty or whitespace-only.
        - Body is not valid JSON.
        - Top-level JSON value is not a ``dict``.
        - Any other unexpected exception.

        :param response: The HTTP response to inspect.
        :returns: A parsed :class:`OciErrorResponse`, or
            ``OciErrorResponse(errors=[])`` on any parse failure.
        """
        try:
            text = response.text
            if not text or not text.strip():
                return cls()
            return cls.from_json(text)
        except Exception:
            return cls()

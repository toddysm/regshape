#!/usr/bin/env python3

"""
:mod:`regshape.libs.models.blob` - OCI BlobInfo and BlobUploadSession data models
==================================================================================

.. module:: regshape.libs.models.blob
   :platform: Unix, Windows
   :synopsis: Dataclasses for OCI blob metadata (BlobInfo) and in-progress
              blob upload state (BlobUploadSession).

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from regshape.libs.errors import BlobError

# Matches "sha256:<hex>" or "sha512:<hex>"
_DIGEST_RE = re.compile(r"^(sha256|sha512):[a-f0-9]+$")


@dataclass
class BlobInfo:
    """Metadata for an existing OCI blob.

    Derived from the response headers of a ``HEAD`` or ``GET`` request to
    ``/v2/<name>/blobs/<digest>``.  There is no JSON body to parse — all
    fields come from HTTP headers.

    :param digest: SHA-256 (or SHA-512) content digest, taken from the
        ``Docker-Content-Digest`` response header.  Must match
        ``(sha256|sha512):[a-f0-9]+``.
    :param content_type: MIME type of the blob, taken from ``Content-Type``.
        Must be a non-empty string.
    :param size: Byte length of the blob, taken from ``Content-Length``.
        Defaults to ``0`` when the header is absent or non-numeric.  Must
        be ``>= 0``.
    """

    digest: str
    content_type: str
    size: int

    def __post_init__(self) -> None:
        if not _DIGEST_RE.match(self.digest):
            raise ValueError(
                f"BlobInfo.digest must match '(sha256|sha512):[a-f0-9]+', "
                f"got {self.digest!r}"
            )
        if not self.content_type:
            raise ValueError("BlobInfo.content_type must not be empty")
        if self.size < 0:
            raise ValueError(
                f"BlobInfo.size must be >= 0, got {self.size}"
            )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to a plain dict.

        :returns: Dict with ``"digest"``, ``"content_type"``, and ``"size"``
            keys.
        """
        return {
            "digest": self.digest,
            "content_type": self.content_type,
            "size": self.size,
        }


@dataclass
class BlobUploadSession:
    """State for an in-progress OCI blob upload.

    Derived from the ``Location`` header returned by the initiating
    ``POST /v2/<name>/blobs/uploads/`` call.  Used internally by
    :func:`~regshape.libs.blobs.operations.upload_blob` and
    :func:`~regshape.libs.blobs.operations.upload_blob_chunked`
    to track the upload path and byte offset across the multi-step
    POST → (PATCH*) → PUT call sequence.

    :param upload_path: Path component of the ``Location`` URL
        (e.g. ``"/v2/repo/blobs/uploads/abc-123"``). Always a clean
        ``/v2/...`` path regardless of whether the registry returned an
        absolute URL or a relative path in the ``Location`` header.
    :param session_id: UUID at the end of *upload_path*; the final non-empty
        path segment.
    :param offset: Current byte offset into the upload. Starts at ``0`` and
        is advanced by the domain layer after each successful PATCH.
    """

    upload_path: str
    session_id: str
    offset: int = field(default=0)

    def __post_init__(self) -> None:
        if not self.upload_path or not self.upload_path.startswith("/"):
            raise ValueError(
                "BlobUploadSession.upload_path must be a non-empty string "
                f"starting with '/', got {self.upload_path!r}"
            )
        if not self.session_id:
            raise ValueError(
                "BlobUploadSession.session_id must not be empty"
            )
        if self.offset < 0:
            raise ValueError(
                f"BlobUploadSession.offset must be >= 0, got {self.offset}"
            )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_location(cls, location: str) -> "BlobUploadSession":
        """Parse a ``Location`` header value into an upload session.

        Accepts both absolute URLs and relative paths::

            https://registry.example.com/v2/repo/blobs/uploads/abc-123
            /v2/repo/blobs/uploads/abc-123

        ``urllib.parse.urlparse`` is used to extract the path component so
        that the stored :attr:`upload_path` is always a clean ``/v2/...``
        string regardless of the registry's chosen response style.

        :param location: Raw ``Location`` header value from a POST response.
        :returns: A :class:`BlobUploadSession` with ``offset=0``.
        :raises BlobError: If *location* is empty, the parsed path does not
            start with ``/v2/``, or the UUID segment is absent.
        """
        if not location:
            raise BlobError(
                "Failed to parse upload session from Location header",
                "Location header is empty",
            )
        parsed = urlparse(location)
        path = parsed.path
        if not path.startswith("/v2/"):
            raise BlobError(
                "Failed to parse upload session from Location header",
                f"expected path starting with '/v2/', got {path!r}",
            )
        # The session ID is the last non-empty segment of the path.
        # Require at least two non-empty path components beyond the leading
        # slash (i.e. '/v2/x/uuid' minimum) so that bare '/v2/' or '/v2/x'
        # are rejected as invalid upload-session paths.
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3:
            raise BlobError(
                "Failed to parse upload session from Location header",
                f"upload path is too short to contain a session ID: {path!r}",
            )
        segment = parts[-1]
        clean_path = parsed.path.rstrip("/")
        upload_url = clean_path + ("?" + parsed.query if parsed.query else "")
        return cls(upload_path=upload_url, session_id=segment)

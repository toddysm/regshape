#!/usr/bin/env python3

"""
Tests for :mod:`regshape.libs.models.blob`.

Covers :class:`BlobInfo` and :class:`BlobUploadSession` construction,
validation, serialization, and the ``from_location`` factory method.
"""

import pytest

from regshape.libs.errors import BlobError
from regshape.libs.models.blob import BlobInfo, BlobUploadSession


VALID_DIGEST = "sha256:" + "a" * 64
VALID_CONTENT_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
VALID_LOCATION_ABS = "https://registry.example.com/v2/myrepo/blobs/uploads/abc-123"
VALID_LOCATION_REL = "/v2/myrepo/blobs/uploads/abc-123"


# ===========================================================================
# BlobInfo
# ===========================================================================


class TestBlobInfo:

    # --- Valid construction ---

    def test_valid_sha256(self):
        info = BlobInfo(
            digest=VALID_DIGEST,
            content_type=VALID_CONTENT_TYPE,
            size=1024,
        )
        assert info.digest == VALID_DIGEST
        assert info.content_type == VALID_CONTENT_TYPE
        assert info.size == 1024

    def test_valid_sha512(self):
        digest = "sha512:" + "b" * 128
        info = BlobInfo(digest=digest, content_type="application/octet-stream", size=0)
        assert info.digest == digest

    def test_valid_zero_size(self):
        info = BlobInfo(digest=VALID_DIGEST, content_type="application/octet-stream", size=0)
        assert info.size == 0

    # --- Digest validation ---

    def test_invalid_digest_no_algorithm(self):
        with pytest.raises(ValueError, match="digest"):
            BlobInfo(digest="abc123", content_type="application/octet-stream", size=0)

    def test_invalid_digest_unknown_algorithm(self):
        with pytest.raises(ValueError, match="digest"):
            BlobInfo(digest="md5:abc123", content_type="application/octet-stream", size=0)

    def test_invalid_digest_uppercase_hex(self):
        with pytest.raises(ValueError, match="digest"):
            BlobInfo(digest="sha256:" + "A" * 64, content_type="application/octet-stream", size=0)

    def test_invalid_digest_empty(self):
        with pytest.raises(ValueError, match="digest"):
            BlobInfo(digest="", content_type="application/octet-stream", size=0)

    # --- content_type validation ---

    def test_empty_content_type(self):
        with pytest.raises(ValueError, match="content_type"):
            BlobInfo(digest=VALID_DIGEST, content_type="", size=0)

    # --- size validation ---

    def test_negative_size(self):
        with pytest.raises(ValueError, match="size"):
            BlobInfo(digest=VALID_DIGEST, content_type="application/octet-stream", size=-1)

    # --- to_dict ---

    def test_to_dict_keys(self):
        info = BlobInfo(digest=VALID_DIGEST, content_type=VALID_CONTENT_TYPE, size=4096)
        d = info.to_dict()
        assert set(d.keys()) == {"digest", "content_type", "size"}

    def test_to_dict_values(self):
        info = BlobInfo(digest=VALID_DIGEST, content_type=VALID_CONTENT_TYPE, size=4096)
        d = info.to_dict()
        assert d["digest"] == VALID_DIGEST
        assert d["content_type"] == VALID_CONTENT_TYPE
        assert d["size"] == 4096

    def test_to_dict_zero_size(self):
        info = BlobInfo(digest=VALID_DIGEST, content_type="application/octet-stream", size=0)
        assert info.to_dict()["size"] == 0


# ===========================================================================
# BlobUploadSession
# ===========================================================================


class TestBlobUploadSession:

    # --- Valid direct construction ---

    def test_valid_construction(self):
        session = BlobUploadSession(
            upload_path="/v2/myrepo/blobs/uploads/abc-123",
            session_id="abc-123",
        )
        assert session.upload_path == "/v2/myrepo/blobs/uploads/abc-123"
        assert session.session_id == "abc-123"
        assert session.offset == 0

    def test_non_zero_offset(self):
        session = BlobUploadSession(
            upload_path="/v2/repo/blobs/uploads/sid",
            session_id="sid",
            offset=1024,
        )
        assert session.offset == 1024

    # --- __post_init__ validation ---

    def test_empty_upload_path(self):
        with pytest.raises(ValueError, match="upload_path"):
            BlobUploadSession(upload_path="", session_id="abc")

    def test_upload_path_no_leading_slash(self):
        with pytest.raises(ValueError, match="upload_path"):
            BlobUploadSession(upload_path="v2/repo/blobs/uploads/abc", session_id="abc")

    def test_empty_session_id(self):
        with pytest.raises(ValueError, match="session_id"):
            BlobUploadSession(upload_path="/v2/repo/blobs/uploads/abc", session_id="")

    def test_negative_offset(self):
        with pytest.raises(ValueError, match="offset"):
            BlobUploadSession(
                upload_path="/v2/repo/blobs/uploads/abc",
                session_id="abc",
                offset=-1,
            )

    # --- from_location: relative path ---

    def test_from_location_relative_path(self):
        session = BlobUploadSession.from_location(VALID_LOCATION_REL)
        assert session.upload_path == "/v2/myrepo/blobs/uploads/abc-123"
        assert session.session_id == "abc-123"
        assert session.offset == 0

    # --- from_location: absolute URL ---

    def test_from_location_absolute_url(self):
        session = BlobUploadSession.from_location(VALID_LOCATION_ABS)
        assert session.upload_path == "/v2/myrepo/blobs/uploads/abc-123"
        assert session.session_id == "abc-123"
        assert session.offset == 0

    def test_from_location_absolute_url_with_query(self):
        """Query string from the Location header must be preserved in upload_path."""
        url = "https://registry.example.com/v2/repo/blobs/uploads/uuid-1?state=xyz"
        session = BlobUploadSession.from_location(url)
        assert session.upload_path == "/v2/repo/blobs/uploads/uuid-1?state=xyz"
        assert "?" in session.upload_path
        assert session.session_id == "uuid-1"

    def test_from_location_relative_path_with_query(self):
        """Query string on a relative Location path must also be preserved."""
        url = "/v2/repo/blobs/uploads/abc-123?_state=tok&ttl=300"
        session = BlobUploadSession.from_location(url)
        assert session.upload_path == "/v2/repo/blobs/uploads/abc-123?_state=tok&ttl=300"
        assert session.session_id == "abc-123"

    def test_from_location_no_query_has_no_question_mark(self):
        """When the Location has no query string, upload_path must not contain '?'."""
        session = BlobUploadSession.from_location("/v2/repo/blobs/uploads/abc-123")
        assert "?" not in session.upload_path

    def test_from_location_strips_trailing_slash(self):
        session = BlobUploadSession.from_location("/v2/myrepo/blobs/uploads/abc-123/")
        assert session.session_id == "abc-123"

    # --- from_location: error cases ---

    def test_from_location_empty_string(self):
        with pytest.raises(BlobError):
            BlobUploadSession.from_location("")

    def test_from_location_path_not_v2(self):
        with pytest.raises(BlobError, match="'/v2/'"):
            BlobUploadSession.from_location("/v3/repo/blobs/uploads/abc")

    def test_from_location_absolute_url_not_v2(self):
        with pytest.raises(BlobError):
            BlobUploadSession.from_location("https://registry.example.com/uploads/abc")

    def test_from_location_only_v2_root(self):
        """'/v2/' alone has no session ID segment."""
        with pytest.raises(BlobError):
            BlobUploadSession.from_location("/v2/")

    # --- offset mutation ---

    def test_offset_can_be_updated(self):
        session = BlobUploadSession.from_location(VALID_LOCATION_REL)
        session.offset += 65536
        assert session.offset == 65536

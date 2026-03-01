#!/usr/bin/env python3

"""
Tests for :mod:`regshape.libs.blobs.operations`.

Exercises the domain functions directly (without the CLI layer).  The
:class:`~regshape.libs.transport.RegistryClient` and HTTP responses are
replaced with lightweight mocks so no network I/O is performed.
"""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from regshape.libs.blobs.operations import get_blob
from regshape.libs.errors import BlobError
from regshape.libs.models.blob import BlobInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
CONTENT = b"hello blob"


def _sha256_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha512_of(data: bytes) -> str:
    return "sha512:" + hashlib.sha512(data).hexdigest()


def _make_client(registry: str = REGISTRY) -> MagicMock:
    """Return a minimal mock RegistryClient."""
    client = MagicMock()
    client.config.registry = registry
    return client


def _make_response(
    content: bytes,
    status_code: int = 200,
    digest: str | None = None,
    content_type: str = "application/octet-stream",
) -> MagicMock:
    """Return a mock streaming response whose iter_content yields *content*."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(content)),
    }
    if digest:
        response.headers["Docker-Content-Digest"] = digest
    # iter_content yields the whole payload in one chunk
    response.iter_content.return_value = iter([content])
    return response


# ===========================================================================
# get_blob — algorithm detection
# ===========================================================================


class TestGetBlobAlgorithmDetection:

    def test_sha256_accepted(self):
        digest = _sha256_of(CONTENT)
        client = _make_client()
        client.get.return_value = _make_response(CONTENT, digest=digest)

        info = get_blob(client=client, repo=REPO, digest=digest)

        assert info.digest == digest

    def test_sha512_accepted(self):
        digest = _sha512_of(CONTENT)
        client = _make_client()
        client.get.return_value = _make_response(CONTENT, digest=digest)

        info = get_blob(client=client, repo=REPO, digest=digest)

        assert info.digest == digest

    def test_unsupported_algorithm_raises_before_http(self):
        """MD5 is not supported; BlobError must be raised before any HTTP call."""
        client = _make_client()
        bad_digest = "md5:" + "a" * 32

        with pytest.raises(BlobError, match="Unsupported digest algorithm"):
            get_blob(client=client, repo=REPO, digest=bad_digest)

        client.get.assert_not_called()

    def test_missing_separator_raises_before_http(self):
        """A digest with no ':' has no parseable algorithm prefix."""
        client = _make_client()

        with pytest.raises(BlobError, match="Unsupported digest algorithm"):
            get_blob(client=client, repo=REPO, digest="sha256abc" + "a" * 64)

        client.get.assert_not_called()

    def test_empty_digest_raises_before_http(self):
        client = _make_client()

        with pytest.raises(BlobError, match="Unsupported digest algorithm"):
            get_blob(client=client, repo=REPO, digest="")

        client.get.assert_not_called()

    def test_unsupported_algorithm_error_lists_supported(self):
        """Error detail should name the supported algorithms."""
        client = _make_client()

        with pytest.raises(BlobError) as exc_info:
            get_blob(client=client, repo=REPO, digest="md5:" + "a" * 32)

        assert "sha256" in str(exc_info.value)
        assert "sha512" in str(exc_info.value)


# ===========================================================================
# get_blob — digest verification
# ===========================================================================


class TestGetBlobDigestVerification:

    def test_sha256_mismatch_raises(self):
        wrong_digest = "sha256:" + "b" * 64
        client = _make_client()
        client.get.return_value = _make_response(CONTENT)

        with pytest.raises(BlobError, match="Digest mismatch"):
            get_blob(client=client, repo=REPO, digest=wrong_digest)

    def test_sha512_mismatch_raises(self):
        wrong_digest = "sha512:" + "b" * 128
        client = _make_client()
        client.get.return_value = _make_response(CONTENT)

        with pytest.raises(BlobError, match="Digest mismatch"):
            get_blob(client=client, repo=REPO, digest=wrong_digest)

    def test_sha256_mismatch_error_shows_expected_and_computed(self):
        expected = "sha256:" + "b" * 64
        client = _make_client()
        client.get.return_value = _make_response(CONTENT)

        with pytest.raises(BlobError) as exc_info:
            get_blob(client=client, repo=REPO, digest=expected)

        assert expected in str(exc_info.value)
        assert _sha256_of(CONTENT) in str(exc_info.value)

    def test_sha512_mismatch_computed_digest_has_sha512_prefix(self):
        """Computed digest in the error must use the sha512: prefix, not sha256:."""
        expected = "sha512:" + "b" * 128
        client = _make_client()
        client.get.return_value = _make_response(CONTENT)

        with pytest.raises(BlobError) as exc_info:
            get_blob(client=client, repo=REPO, digest=expected)

        # The error should mention the sha512: computed value, not a sha256: one
        error_text = str(exc_info.value)
        assert "sha512:" in error_text
        assert "sha256:" not in error_text

    def test_output_file_removed_on_mismatch(self, tmp_path):
        output = tmp_path / "blob.bin"
        wrong_digest = "sha256:" + "b" * 64
        client = _make_client()
        client.get.return_value = _make_response(CONTENT)

        with pytest.raises(BlobError, match="Digest mismatch"):
            get_blob(
                client=client,
                repo=REPO,
                digest=wrong_digest,
                output_path=str(output),
            )

        assert not output.exists(), "partial output file should be deleted on mismatch"

    def test_sha512_output_file_written_on_success(self, tmp_path):
        digest = _sha512_of(CONTENT)
        output = tmp_path / "blob.bin"
        client = _make_client()
        client.get.return_value = _make_response(CONTENT, digest=digest)

        get_blob(
            client=client,
            repo=REPO,
            digest=digest,
            output_path=str(output),
        )

        assert output.read_bytes() == CONTENT


# ===========================================================================
# get_blob — returned BlobInfo
# ===========================================================================


class TestGetBlobReturnValue:

    def test_returns_blob_info(self):
        digest = _sha256_of(CONTENT)
        client = _make_client()
        client.get.return_value = _make_response(
            CONTENT,
            digest=digest,
            content_type="application/vnd.oci.image.layer.v1.tar+gzip",
        )

        info = get_blob(client=client, repo=REPO, digest=digest)

        assert isinstance(info, BlobInfo)
        assert info.digest == digest
        assert info.size == len(CONTENT)
        assert info.content_type == "application/vnd.oci.image.layer.v1.tar+gzip"

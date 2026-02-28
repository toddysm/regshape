#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.manifests.operations`."""

import json

import pytest
import requests
from unittest.mock import MagicMock

from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.manifests.operations import (
    _raise_for_manifest_error,
    delete_manifest,
    get_manifest,
    head_manifest,
    push_manifest,
)
from regshape.libs.transport import RegistryClient, TransportConfig


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
TAG = "latest"
DIGEST = "sha256:abc123"
OCI_MANIFEST_JSON = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"})
CONTENT_TYPE = "application/vnd.oci.image.manifest.v1+json"


def _mock_client() -> MagicMock:
    """Return a MagicMock that looks like a RegistryClient."""
    client = MagicMock(spec=RegistryClient)
    config = MagicMock(spec=TransportConfig)
    config.registry = REGISTRY
    client.config = config
    return client


def _make_response(
    status_code: int,
    body: str = "{}",
    content_type: str = "",
    digest: str = "",
    content_length: str = "",
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    headers: dict = {}
    if content_type:
        headers["Content-Type"] = content_type
    if digest:
        headers["Docker-Content-Digest"] = digest
    if content_length:
        headers["Content-Length"] = content_length
    resp.headers = headers
    return resp


# ===========================================================================
# get_manifest
# ===========================================================================

class TestGetManifest:

    def test_returns_body_content_type_digest(self):
        client = _mock_client()
        client.get.return_value = _make_response(
            200,
            body=OCI_MANIFEST_JSON,
            content_type=CONTENT_TYPE,
            digest=DIGEST,
        )
        body, ct, dig = get_manifest(client, REPO, TAG, CONTENT_TYPE)
        assert body == OCI_MANIFEST_JSON
        assert ct == CONTENT_TYPE
        assert dig == DIGEST

    def test_get_called_with_correct_path(self):
        client = _mock_client()
        client.get.return_value = _make_response(200)
        get_manifest(client, REPO, TAG, CONTENT_TYPE)
        client.get.assert_called_once()
        assert client.get.call_args[0][0] == f"/v2/{REPO}/manifests/{TAG}"

    def test_accept_header_forwarded(self):
        client = _mock_client()
        client.get.return_value = _make_response(200)
        accept = "application/json"
        get_manifest(client, REPO, TAG, accept)
        headers = client.get.call_args[1].get("headers") or client.get.call_args[0][1]
        assert headers.get("Accept") == accept

    def test_digest_reference(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, digest=DIGEST)
        _, _, dig = get_manifest(client, REPO, DIGEST, CONTENT_TYPE)
        assert dig == DIGEST

    def test_404_raises_manifest_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(404)
        with pytest.raises(ManifestError, match="not found"):
            get_manifest(client, REPO, TAG, CONTENT_TYPE)

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(401)
        with pytest.raises(AuthError):
            get_manifest(client, REPO, TAG, CONTENT_TYPE)

    def test_500_raises_manifest_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(500, body="internal error")
        with pytest.raises(ManifestError, match="Registry error"):
            get_manifest(client, REPO, TAG, CONTENT_TYPE)

    def test_missing_headers_return_empty_strings(self):
        client = _mock_client()
        resp = _make_response(200, body=OCI_MANIFEST_JSON)
        # headers dict has no Content-Type or Docker-Content-Digest
        resp.headers = {}
        client.get.return_value = resp
        body, ct, dig = get_manifest(client, REPO, TAG, CONTENT_TYPE)
        assert ct == ""
        assert dig == ""


# ===========================================================================
# head_manifest
# ===========================================================================

class TestHeadManifest:

    def test_returns_digest_media_type_size(self):
        client = _mock_client()
        client.head.return_value = _make_response(
            200,
            content_type=CONTENT_TYPE,
            digest=DIGEST,
            content_length="12345",
        )
        dig, mt, sz = head_manifest(client, REPO, TAG, CONTENT_TYPE)
        assert dig == DIGEST
        assert mt == CONTENT_TYPE
        assert sz == 12345

    def test_head_called_with_correct_path(self):
        client = _mock_client()
        client.head.return_value = _make_response(200)
        head_manifest(client, REPO, TAG, CONTENT_TYPE)
        assert client.head.call_args[0][0] == f"/v2/{REPO}/manifests/{TAG}"

    def test_missing_content_length_returns_zero(self):
        client = _mock_client()
        resp = _make_response(200, digest=DIGEST)
        resp.headers = {"Docker-Content-Digest": DIGEST}
        client.head.return_value = resp
        _, _, sz = head_manifest(client, REPO, TAG, CONTENT_TYPE)
        assert sz == 0

    def test_invalid_content_length_returns_zero(self):
        client = _mock_client()
        resp = _make_response(200)
        resp.headers = {"Content-Length": "not-a-number"}
        client.head.return_value = resp
        _, _, sz = head_manifest(client, REPO, TAG, CONTENT_TYPE)
        assert sz == 0

    def test_404_raises_manifest_error(self):
        client = _mock_client()
        client.head.return_value = _make_response(404)
        with pytest.raises(ManifestError, match="not found"):
            head_manifest(client, REPO, TAG, CONTENT_TYPE)

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.head.return_value = _make_response(401)
        with pytest.raises(AuthError):
            head_manifest(client, REPO, TAG, CONTENT_TYPE)


# ===========================================================================
# push_manifest
# ===========================================================================

class TestPushManifest:

    def test_returns_digest_from_response_header(self):
        client = _mock_client()
        client.put.return_value = _make_response(201, digest=DIGEST)
        result = push_manifest(client, REPO, TAG, OCI_MANIFEST_JSON.encode(), CONTENT_TYPE)
        assert result == DIGEST

    def test_put_called_with_correct_path(self):
        client = _mock_client()
        client.put.return_value = _make_response(201)
        push_manifest(client, REPO, TAG, b"{}", CONTENT_TYPE)
        assert client.put.call_args[0][0] == f"/v2/{REPO}/manifests/{TAG}"

    def test_content_type_header_forwarded(self):
        client = _mock_client()
        client.put.return_value = _make_response(201)
        push_manifest(client, REPO, TAG, b"{}", CONTENT_TYPE)
        headers = client.put.call_args[1].get("headers") or client.put.call_args[0][1]
        assert headers.get("Content-Type") == CONTENT_TYPE

    def test_body_forwarded_as_data(self):
        client = _mock_client()
        client.put.return_value = _make_response(201)
        body_bytes = b'{"schemaVersion": 2}'
        push_manifest(client, REPO, TAG, body_bytes, CONTENT_TYPE)
        assert client.put.call_args[1].get("data") == body_bytes

    def test_missing_digest_header_returns_empty_string(self):
        client = _mock_client()
        resp = _make_response(201)
        resp.headers = {}
        client.put.return_value = resp
        result = push_manifest(client, REPO, TAG, b"{}", CONTENT_TYPE)
        assert result == ""

    def test_non_2xx_raises_manifest_error(self):
        client = _mock_client()
        client.put.return_value = _make_response(400, body='{"errors":[{"code":"INVALID","message":"bad"}]}')
        with pytest.raises(ManifestError):
            push_manifest(client, REPO, TAG, b"{}", CONTENT_TYPE)


# ===========================================================================
# delete_manifest
# ===========================================================================

class TestDeleteManifest:

    def test_succeeds_silently_on_202(self):
        client = _mock_client()
        client.delete.return_value = _make_response(202)
        delete_manifest(client, REPO, DIGEST)  # should not raise

    def test_delete_called_with_correct_path(self):
        client = _mock_client()
        client.delete.return_value = _make_response(202)
        delete_manifest(client, REPO, DIGEST)
        assert client.delete.call_args[0][0] == f"/v2/{REPO}/manifests/{DIGEST}"

    def test_404_raises_manifest_error(self):
        client = _mock_client()
        client.delete.return_value = _make_response(404)
        with pytest.raises(ManifestError, match="not found"):
            delete_manifest(client, REPO, DIGEST)

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.delete.return_value = _make_response(401)
        with pytest.raises(AuthError):
            delete_manifest(client, REPO, DIGEST)

    def test_500_raises_manifest_error(self):
        client = _mock_client()
        client.delete.return_value = _make_response(500)
        with pytest.raises(ManifestError):
            delete_manifest(client, REPO, DIGEST)


# ===========================================================================
# _raise_for_manifest_error — direct tests
# ===========================================================================

class TestRaiseForManifestError:

    def test_2xx_is_silent(self):
        for code in (200, 201, 202, 204):
            resp = _make_response(code)
            _raise_for_manifest_error(resp, REGISTRY, REPO, TAG)  # no exception

    def test_404_raises_manifest_error_with_ref_in_message(self):
        resp = _make_response(404)
        with pytest.raises(ManifestError, match=REPO):
            _raise_for_manifest_error(resp, REGISTRY, REPO, TAG)

    def test_401_raises_auth_error_with_registry_in_message(self):
        resp = _make_response(401)
        with pytest.raises(AuthError, match=REGISTRY):
            _raise_for_manifest_error(resp, REGISTRY, REPO, TAG)

    def test_oci_error_detail_used_when_present(self):
        body = json.dumps({"errors": [{"code": "UNAUTHORIZED", "message": "token required"}]})
        resp = _make_response(401, body=body)
        with pytest.raises(AuthError, match="token required"):
            _raise_for_manifest_error(resp, REGISTRY, REPO, TAG)

    def test_fallback_to_response_text_when_no_oci_error(self):
        resp = _make_response(503, body="Service Unavailable")
        with pytest.raises(ManifestError, match="Service Unavailable"):
            _raise_for_manifest_error(resp, REGISTRY, REPO, TAG)

#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.manifests.operations`."""

import hashlib
import json
from unittest.mock import MagicMock

import pytest
import requests

from regshape.libs.errors import AuthError, ManifestError, ReferenceValidationError
from regshape.libs.manifests import DEFAULT_MANIFEST_ACCEPT, resolve_digest
from regshape.libs.manifests.operations import (
    _raise_for_manifest_error,
    delete_manifest,
    get_manifest,
    head_manifest,
    push_manifest,
)
from regshape.libs.models.mediatype import ALL_MANIFEST_MEDIA_TYPES, OCI_IMAGE_MANIFEST
from regshape.libs.transport import RegistryClient, TransportConfig

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
TAG = "latest"
DIGEST = "sha256:" + "a" * 64
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

@pytest.mark.parametrize("operation", [get_manifest, head_manifest])
@pytest.mark.parametrize("options", [{}, {"accept": None}])
def test_manifest_reads_use_library_accept_default(operation, options):
    """Omission and explicit None negotiate all supported manifest media types."""
    client = _mock_client()
    client.get.return_value = client.head.return_value = _make_response(200)
    operation(client, REPO, TAG, **options)
    request = client.get if operation is get_manifest else client.head
    request.assert_called_once_with(
        f"/v2/{REPO}/manifests/{TAG}", headers={"Accept": DEFAULT_MANIFEST_ACCEPT},
    )
    assert request.call_args.kwargs["headers"]["Accept"].split(",") == sorted(ALL_MANIFEST_MEDIA_TYPES)


@pytest.mark.parametrize("operation", [get_manifest, head_manifest])
@pytest.mark.parametrize("accept", ["application/x.conformance", ""])
def test_manifest_reads_preserve_explicit_accept(operation, accept):
    """Break-mode overrides, including an empty header, are never defaulted."""
    client = _mock_client()
    client.get.return_value = client.head.return_value = _make_response(200)
    operation(client, REPO, TAG, accept=accept)
    request = client.get if operation is get_manifest else client.head
    assert request.call_args.kwargs["headers"] == {"Accept": accept}


class TestResolveDigest:
    """Resolve tags through real HEAD wiring while retaining permissive digest passthrough."""

    @pytest.mark.parametrize("reference", [
        DIGEST, "sha512:" + "b" * 128, "md5:abc", "sha256:short",
    ])
    def test_digest_shaped_tokens_pass_through_without_http(self, reference):
        """Shape classification must not reinterpret unsupported/malformed digests as tags."""
        client = _mock_client()
        assert resolve_digest(client, REPO, reference) == reference
        assert client.method_calls == []

    @pytest.mark.parametrize("options,expected_accept", [
        ({}, DEFAULT_MANIFEST_ACCEPT),
        ({"accept": None}, DEFAULT_MANIFEST_ACCEPT),
        ({"accept": "application/x.test"}, "application/x.test"),
        ({"accept": ""}, ""),
    ])
    def test_tag_uses_one_head_and_preserves_negotiation(self, options, expected_accept):
        """Default negotiation belongs to HEAD; explicit overrides are unchanged."""
        client = _mock_client()
        client.head.return_value = _make_response(200, digest=DIGEST)
        assert resolve_digest(client, REPO, TAG, **options) == DIGEST
        client.head.assert_called_once_with(
            f"/v2/{REPO}/manifests/{TAG}", headers={"Accept": expected_accept},
        )
        client.get.assert_not_called()

    @pytest.mark.parametrize("header", [None, "", " ", "\t \r\n"])
    def test_missing_or_blank_digest_raises_existing_error(self, header):
        """A successful HEAD without a usable identity must never become an empty subject."""
        client = _mock_client()
        response = _make_response(200)
        if header is not None:
            response.headers["Docker-Content-Digest"] = header
        client.head.return_value = response
        with pytest.raises(ManifestError) as caught:
            resolve_digest(client, REPO, TAG)
        assert str(caught.value) == str(ManifestError(
            f"Cannot resolve tag {TAG!r}", "HEAD response is missing Docker-Content-Digest",
        ))
        client.get.assert_not_called()

    def test_nonblank_response_header_is_not_normalized(self):
        """Resolution reports the registry's value, without pretending to verify its digest."""
        client = _mock_client()
        header = f" {DIGEST} "
        client.head.return_value = _make_response(200, digest=header)
        assert resolve_digest(client, REPO, TAG) == header

    @pytest.mark.parametrize("status,error", [(401, AuthError), (404, ManifestError), (500, ManifestError)])
    def test_head_registry_errors_propagate(self, status, error):
        """Keep the existing authentication/domain error mapping from HEAD."""
        client = _mock_client()
        client.head.return_value = _make_response(status)
        with pytest.raises(error):
            resolve_digest(client, REPO, TAG)

    def test_transport_error_propagates_unchanged(self):
        """Transport exceptions keep their identity instead of becoming a missing-header error."""
        client = _mock_client()
        error = requests.exceptions.Timeout("resolution timed out")
        client.head.side_effect = error
        with pytest.raises(requests.exceptions.Timeout) as caught:
            resolve_digest(client, REPO, TAG)
        assert caught.value is error


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

    @pytest.mark.parametrize("options", [{}, {"content_type": None}])
    @pytest.mark.parametrize("body,expected", [
        (b'{"mediaType":"application/x.example.manifest"}', "application/x.example.manifest"),
        (b'{"schemaVersion":2}', OCI_IMAGE_MANIFEST),
        (b"not JSON", OCI_IMAGE_MANIFEST), (b"[]", OCI_IMAGE_MANIFEST),
        (b"null", OCI_IMAGE_MANIFEST), (b"42", OCI_IMAGE_MANIFEST),
        (b'"text"', OCI_IMAGE_MANIFEST), (b"\xff", OCI_IMAGE_MANIFEST),
    ])
    def test_infers_content_type_without_rewriting_body(self, body, expected, options):
        """SDK callers get the former CLI inference/fallback without transforming bytes."""
        client = _mock_client()
        client.put.return_value = _make_response(201)
        push_manifest(client, REPO, TAG, body, **options)
        client.put.assert_called_once_with(
            f"/v2/{REPO}/manifests/{TAG}", headers={"Content-Type": expected}, data=body,
        )

    @pytest.mark.parametrize("content_type", ["application/x.conformance", ""])
    def test_explicit_content_type_bypasses_body_inference(self, content_type):
        """Explicit overrides are sent verbatim even for an undecodable body."""
        client = _mock_client()
        client.put.return_value = _make_response(201)
        push_manifest(client, REPO, TAG, b"\xff", content_type=content_type)
        assert client.put.call_args.kwargs["headers"] == {"Content-Type": content_type}

    def test_inferred_type_is_available_to_strict_publication(self):
        """Resolve the default before strict input validation, retaining digest checks."""
        client = _mock_client()
        body = b'{"schemaVersion":2}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        response = _make_response(201, digest=digest)
        response.request = requests.Request(
            "PUT", f"https://{REGISTRY}/v2/{REPO}/manifests/{TAG}", data=body,
        ).prepare()
        client.put.return_value = response
        assert push_manifest(client, REPO, TAG, body, expected_digest=digest) == digest
        assert client.put.call_args.kwargs["headers"] == {"Content-Type": OCI_IMAGE_MANIFEST}

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

    @pytest.mark.parametrize("reference", [
        "latest", "sha384:" + "a" * 96, "sha256:abc123",
        "sha256:" + "z" * 64, "sha512:" + "a" * 64,
    ])
    def test_invalid_digest_rejected_before_http(self, reference):
        """Default guards reject tags and invalid content addresses without DELETE."""
        client = _mock_client()
        with pytest.raises(ReferenceValidationError, match="manifest delete requires a digest"):
            delete_manifest(client, REPO, reference)
        client.delete.assert_not_called()

    @pytest.mark.parametrize("reference", ["latest", "sha384:abc", "sha256:xyz"])
    def test_strict_false_sends_invalid_reference_verbatim(self, reference):
        """Opted-in conformance callers can deliberately send wrong-kind inputs."""
        client = _mock_client()
        client.delete.return_value = _make_response(202)
        delete_manifest(client, REPO, reference, strict=False)
        client.delete.assert_called_once_with(f"/v2/{REPO}/manifests/{reference}")

    def test_sha512_digest_is_supported(self):
        """A complete SHA-512 digest is accepted without weakening the default guard."""
        client = _mock_client()
        client.delete.return_value = _make_response(202)
        digest = "sha512:" + "a" * 128
        delete_manifest(client, REPO, digest)
        client.delete.assert_called_once_with(f"/v2/{REPO}/manifests/{digest}")

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

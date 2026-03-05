#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.referrers.operations`."""

import json

import pytest
import requests
from unittest.mock import MagicMock, PropertyMock

from regshape.libs.errors import AuthError, ReferrerError
from regshape.libs.referrers.operations import (
    _parse_next_url,
    _raise_for_list_error,
    list_referrers,
    list_referrers_all,
)
from regshape.libs.transport import RegistryClient, TransportConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
DIGEST = "sha256:" + "a" * 64
SBOM_TYPE = "application/vnd.example.sbom.v1"
SIG_TYPE = "application/vnd.cncf.notary.signature"
MANIFEST_MT = "application/vnd.oci.image.manifest.v1+json"
INDEX_MT = "application/vnd.oci.image.index.v1+json"


def _descriptor_dict(digest: str = DIGEST, artifact_type: str = SBOM_TYPE, size: int = 1234) -> dict:
    return {
        "mediaType": MANIFEST_MT,
        "digest": digest,
        "size": size,
        "artifactType": artifact_type,
    }


def _referrer_response_json(manifests: list[dict] | None = None) -> str:
    return json.dumps({
        "schemaVersion": 2,
        "mediaType": INDEX_MT,
        "manifests": manifests if manifests is not None else [],
    })


def _mock_client() -> MagicMock:
    client = MagicMock(spec=RegistryClient)
    config = MagicMock(spec=TransportConfig)
    config.registry = REGISTRY
    client.config = config
    return client


def _make_response(
    status_code: int,
    body: str = "{}",
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    resp.headers = headers or {}
    return resp


# ===========================================================================
# list_referrers
# ===========================================================================

class TestListReferrers:

    def test_returns_referrer_list(self):
        body = _referrer_response_json([_descriptor_dict()])
        client = _mock_client()
        client.get.return_value = _make_response(200, body=body)
        result = list_referrers(client, REPO, DIGEST)
        assert len(result.manifests) == 1
        assert result.manifests[0].digest == DIGEST

    def test_get_called_with_correct_path(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=_referrer_response_json())
        list_referrers(client, REPO, DIGEST)
        client.get.assert_called_once()
        assert client.get.call_args[0][0] == f"/v2/{REPO}/referrers/{DIGEST}"

    def test_no_params_by_default(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=_referrer_response_json())
        list_referrers(client, REPO, DIGEST)
        call_kwargs = client.get.call_args[1]
        assert call_kwargs.get("params") is None

    def test_artifact_type_forwarded_as_param(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body=_referrer_response_json())
        list_referrers(client, REPO, DIGEST, artifact_type=SBOM_TYPE)
        params = client.get.call_args[1].get("params")
        assert params == {"artifactType": SBOM_TYPE}

    def test_server_side_filtering_trusted(self):
        """When OCI-Filters-Applied contains artifactType, trust the result."""
        body = _referrer_response_json([_descriptor_dict(artifact_type=SBOM_TYPE)])
        resp = _make_response(200, body=body, headers={"OCI-Filters-Applied": "artifactType"})
        client = _mock_client()
        client.get.return_value = resp
        result = list_referrers(client, REPO, DIGEST, artifact_type=SBOM_TYPE)
        assert len(result.manifests) == 1

    def test_client_side_filtering_when_no_header(self):
        """When OCI-Filters-Applied is absent, apply client-side filtering."""
        descs = [_descriptor_dict(artifact_type=SBOM_TYPE), _descriptor_dict(artifact_type=SIG_TYPE)]
        body = _referrer_response_json(descs)
        resp = _make_response(200, body=body)
        client = _mock_client()
        client.get.return_value = resp
        result = list_referrers(client, REPO, DIGEST, artifact_type=SBOM_TYPE)
        assert len(result.manifests) == 1
        assert result.manifests[0].artifact_type == SBOM_TYPE

    def test_no_client_side_filtering_without_artifact_type(self):
        """Without artifact_type, no filtering is applied."""
        descs = [_descriptor_dict(artifact_type=SBOM_TYPE), _descriptor_dict(artifact_type=SIG_TYPE)]
        body = _referrer_response_json(descs)
        client = _mock_client()
        client.get.return_value = _make_response(200, body=body)
        result = list_referrers(client, REPO, DIGEST)
        assert len(result.manifests) == 2

    def test_404_raises_referrer_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(404)
        with pytest.raises(ReferrerError, match="Manifest not found"):
            list_referrers(client, REPO, DIGEST)

    def test_401_raises_auth_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(401)
        with pytest.raises(AuthError):
            list_referrers(client, REPO, DIGEST)

    def test_500_raises_referrer_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(500)
        with pytest.raises(ReferrerError):
            list_referrers(client, REPO, DIGEST)

    def test_invalid_json_raises_referrer_error(self):
        client = _mock_client()
        client.get.return_value = _make_response(200, body="not json")
        with pytest.raises(ReferrerError, match="Failed to parse"):
            list_referrers(client, REPO, DIGEST)


# ===========================================================================
# list_referrers_all
# ===========================================================================

DIGEST_PAGE2 = "sha256:" + "c" * 64


class TestListReferrersAll:

    def test_single_page_no_link(self):
        body = _referrer_response_json([_descriptor_dict()])
        resp = _make_response(200, body=body)
        client = _mock_client()
        client.get.return_value = resp
        # last_response is used by list_referrers_all to check Link header
        type(client).last_response = PropertyMock(return_value=resp)
        result = list_referrers_all(client, REPO, DIGEST)
        assert len(result.manifests) == 1

    def test_follows_link_header_for_second_page(self):
        page1_body = _referrer_response_json([_descriptor_dict(DIGEST)])
        page2_body = _referrer_response_json([_descriptor_dict(DIGEST_PAGE2)])

        resp1 = _make_response(200, body=page1_body,
                               headers={"Link": '</v2/repo/referrers/d?last=x>; rel="next"'})
        resp2 = _make_response(200, body=page2_body)

        client = _mock_client()
        client.get.side_effect = [resp1, resp2]
        type(client).last_response = PropertyMock(side_effect=[resp1, resp2])

        result = list_referrers_all(client, REPO, DIGEST)
        assert len(result.manifests) == 2
        assert result.manifests[0].digest == DIGEST
        assert result.manifests[1].digest == DIGEST_PAGE2

    def test_stops_when_no_link_header(self):
        body = _referrer_response_json([_descriptor_dict()])
        resp = _make_response(200, body=body)
        client = _mock_client()
        client.get.return_value = resp
        type(client).last_response = PropertyMock(return_value=resp)
        list_referrers_all(client, REPO, DIGEST)
        # Only the first page should be fetched
        assert client.get.call_count == 1

    def test_client_side_filtering_applied_to_subsequent_pages(self):
        """When the server does NOT set OCI-Filters-Applied, pages 2+
        fetched via bare GET must still be client-side filtered."""
        # Page 1: mixed types, no OCI-Filters-Applied header.
        page1_body = _referrer_response_json([
            _descriptor_dict(DIGEST, artifact_type=SBOM_TYPE),
            _descriptor_dict(DIGEST, artifact_type=SIG_TYPE),
        ])
        resp1 = _make_response(
            200, body=page1_body,
            headers={"Link": '</v2/repo/referrers/d?last=x>; rel="next"'},
        )

        # Page 2: also mixed types, no OCI-Filters-Applied.
        page2_body = _referrer_response_json([
            _descriptor_dict(DIGEST_PAGE2, artifact_type=SBOM_TYPE),
            _descriptor_dict(DIGEST_PAGE2, artifact_type=SIG_TYPE),
        ])
        resp2 = _make_response(200, body=page2_body)

        client = _mock_client()
        client.get.side_effect = [resp1, resp2]
        type(client).last_response = PropertyMock(side_effect=[resp1, resp2])

        result = list_referrers_all(client, REPO, DIGEST, artifact_type=SBOM_TYPE)

        # Only SBOM_TYPE entries should remain from both pages.
        assert len(result.manifests) == 2
        assert all(m.artifact_type == SBOM_TYPE for m in result.manifests)


# ===========================================================================
# _raise_for_list_error — direct tests
# ===========================================================================

class TestRaiseForListError:

    def test_2xx_is_silent(self):
        for code in (200, 201, 202):
            _raise_for_list_error(_make_response(code), REGISTRY, REPO, DIGEST)

    def test_404_raises_referrer_error_with_digest_in_message(self):
        with pytest.raises(ReferrerError, match=DIGEST):
            _raise_for_list_error(_make_response(404), REGISTRY, REPO, DIGEST)

    def test_401_raises_auth_error_with_registry_in_message(self):
        with pytest.raises(AuthError, match=REGISTRY):
            _raise_for_list_error(_make_response(401), REGISTRY, REPO, DIGEST)

    def test_oci_error_detail_used_when_present(self):
        body = json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message": "manifest not found"}]})
        resp = _make_response(404, body=body)
        with pytest.raises(ReferrerError, match="manifest not found"):
            _raise_for_list_error(resp, REGISTRY, REPO, DIGEST)

    def test_503_raises_referrer_error(self):
        with pytest.raises(ReferrerError, match="Registry error"):
            _raise_for_list_error(_make_response(503), REGISTRY, REPO, DIGEST)


# ===========================================================================
# _parse_next_url
# ===========================================================================

class TestParseNextUrl:

    def test_extracts_url_from_link_header(self):
        headers = {"Link": '</v2/repo/referrers/sha?last=abc&n=10>; rel="next"'}
        assert _parse_next_url(headers) == "/v2/repo/referrers/sha?last=abc&n=10"

    def test_returns_none_when_no_link(self):
        assert _parse_next_url({}) is None

    def test_returns_none_when_no_next_rel(self):
        headers = {"Link": '</v2/repo/stuff>; rel="prev"'}
        assert _parse_next_url(headers) is None

    def test_handles_lowercase_link(self):
        headers = {"link": '</v2/repo/referrers/sha?last=abc>; rel="next"'}
        assert _parse_next_url(headers) == "/v2/repo/referrers/sha?last=abc"

    def test_handles_multiple_relations(self):
        headers = {"Link": '</prev>; rel="prev", </next?last=x>; rel="next"'}
        assert _parse_next_url(headers) == "/next?last=x"

    def test_empty_link_header(self):
        assert _parse_next_url({"Link": ""}) is None

#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.referrer`."""

import json

import pytest
import requests
from click.testing import CliRunner
from unittest.mock import patch

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, ReferrerError
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.models.referrer import ReferrerList


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY = "acr.example.io"
REPO = "myrepo/myimage"
DIGEST = "sha256:" + "a" * 64
SBOM_TYPE = "application/vnd.example.sbom.v1"
SIG_TYPE = "application/vnd.cncf.notary.signature"
MANIFEST_MT = "application/vnd.oci.image.manifest.v1+json"
IMAGE_REF = f"{REGISTRY}/{REPO}@{DIGEST}"
TAG_REF = f"{REGISTRY}/{REPO}:v1.0"


def _referrer_list(manifests: list[Descriptor] | None = None) -> ReferrerList:
    return ReferrerList(manifests=manifests or [])


def _descriptor(digest: str = DIGEST, artifact_type: str = SBOM_TYPE, size: int = 1234) -> Descriptor:
    return Descriptor(
        media_type=MANIFEST_MT,
        digest=digest,
        size=size,
        artifact_type=artifact_type,
    )


def _runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# TestReferrerListCommand
# ---------------------------------------------------------------------------

class TestReferrerListCommand:

    def test_list_prints_one_referrer_per_line(self):
        digest2 = "sha256:" + "b" * 64
        rl = _referrer_list([_descriptor(), _descriptor(digest2, SIG_TYPE, 567)])
        with patch("regshape.cli.referrer.list_referrers", return_value=rl):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
            ])
        assert result.exit_code == 0
        assert DIGEST in result.output
        assert SBOM_TYPE in result.output
        assert "1234" in result.output
        assert digest2 in result.output

    def test_list_json_flag(self):
        rl = _referrer_list([_descriptor()])
        with patch("regshape.cli.referrer.list_referrers", return_value=rl):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF, "--json",
            ])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["schemaVersion"] == 2
        assert len(parsed["manifests"]) == 1

    def test_list_empty_referrers(self):
        rl = _referrer_list([])
        with patch("regshape.cli.referrer.list_referrers", return_value=rl):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
            ])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_tag_reference_rejected_with_exit_code_2(self):
        result = _runner().invoke(regshape, [
            "referrer", "list", "-i", TAG_REF,
        ])
        assert result.exit_code == 2
        assert "digest reference" in result.output

    def test_passes_artifact_type(self):
        rl = _referrer_list([_descriptor()])
        with patch("regshape.cli.referrer.list_referrers", return_value=rl) as mock_fn:
            _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
                "--artifact-type", SBOM_TYPE,
            ])
        assert mock_fn.call_args[1]["artifact_type"] == SBOM_TYPE

    def test_all_flag_calls_list_referrers_all(self):
        rl = _referrer_list([_descriptor()])
        with patch("regshape.cli.referrer.list_referrers_all", return_value=rl) as mock_fn:
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF, "--all",
            ])
        assert result.exit_code == 0
        mock_fn.assert_called_once()

    def test_without_all_flag_calls_list_referrers(self):
        rl = _referrer_list([_descriptor()])
        with patch("regshape.cli.referrer.list_referrers", return_value=rl) as mock_fn:
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
            ])
        assert result.exit_code == 0
        mock_fn.assert_called_once()

    def test_auth_error_exits_1(self):
        with patch("regshape.cli.referrer.list_referrers", side_effect=AuthError("auth failed")):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
            ])
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_referrer_error_exits_1(self):
        with patch("regshape.cli.referrer.list_referrers",
                   side_effect=ReferrerError("not found", "404")):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
            ])
        assert result.exit_code == 1

    def test_missing_image_ref_exits_nonzero(self):
        result = _runner().invoke(regshape, [
            "referrer", "list",
        ])
        assert result.exit_code != 0

    def test_output_flag_writes_to_file(self, tmp_path):
        rl = _referrer_list([_descriptor()])
        outfile = tmp_path / "out.txt"
        with patch("regshape.cli.referrer.list_referrers", return_value=rl):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
                "--output", str(outfile),
            ])
        assert result.exit_code == 0
        content = outfile.read_text()
        assert DIGEST in content

    def test_request_exception_exits_1(self):
        with patch("regshape.cli.referrer.list_referrers",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", IMAGE_REF,
            ])
        assert result.exit_code == 1
        assert "Error" in result.output

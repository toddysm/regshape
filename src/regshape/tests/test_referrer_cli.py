#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.referrer`."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.errors import AuthError, ManifestError, ReferrerError
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.models.mediatype import ALL_MANIFEST_MEDIA_TYPES
from regshape.libs.models.referrer import ReferrerList
from regshape.libs.transport import TransportConfig

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


def _response(
    status_code: int,
    body: str = "",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Build an in-memory registry response for CLI/library integration tests."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode("utf-8")
    response.headers.update(headers or {})
    return response


class TestReferrerReferenceResolution:
    """Exercise tag resolution and digest passthrough without registry access."""

    @pytest.fixture
    def client(self) -> Iterator[MagicMock]:
        """Isolate transport construction so tests never read stored credentials."""
        with patch("regshape.cli.referrer.RegistryClient", autospec=True) as factory:
            client = factory.return_value
            client.config = TransportConfig(registry=REGISTRY)
            yield client

    @pytest.mark.parametrize(
        ("image_ref", "tag"),
        [(TAG_REF, "v1.0"), (f"{REGISTRY}/{REPO}", "latest")],
    )
    def test_tag_resolves_once_before_listing(
        self, client: MagicMock, image_ref: str, tag: str
    ) -> None:
        """Resolve explicit and default tags with the existing manifest helper."""
        referrers = _referrer_list([_descriptor()])
        with (
            patch("regshape.cli.referrer.head_manifest",
                  return_value=(DIGEST, MANIFEST_MT, 123)) as head,
            patch("regshape.cli.referrer.list_referrers",
                  return_value=referrers) as list_fn,
        ):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", image_ref,
            ])

        assert result.exit_code == 0, result.output
        head.assert_called_once_with(
            client=client, repo=REPO, reference=tag,
            accept=",".join(sorted(ALL_MANIFEST_MEDIA_TYPES)),
        )
        list_fn.assert_called_once_with(
            client=client, repo=REPO, digest=DIGEST, artifact_type=None,
        )
        assert DIGEST in result.output

    @pytest.mark.parametrize("digest", [DIGEST, "sha512:" + "b" * 128])
    @pytest.mark.parametrize("fetch_all", [False, True])
    def test_digest_bypasses_manifest_lookup(
        self, client: MagicMock, digest: str, fetch_all: bool
    ) -> None:
        """Keep both supported digest forms unchanged without a HEAD request."""
        operation = "list_referrers_all" if fetch_all else "list_referrers"
        args = ["referrer", "list", "-i", f"{REGISTRY}/{REPO}@{digest}"]
        if fetch_all:
            args.append("--all")
        with (
            patch("regshape.cli.referrer.head_manifest") as head,
            patch(f"regshape.cli.referrer.{operation}",
                  return_value=_referrer_list()) as list_fn,
        ):
            result = _runner().invoke(regshape, args)

        assert result.exit_code == 0, result.output
        head.assert_not_called()
        list_fn.assert_called_once_with(
            client=client, repo=REPO, digest=digest, artifact_type=None,
        )

    def test_unsupported_digest_is_not_resolved_as_a_tag(self, client: MagicMock) -> None:
        """Preserve usage errors for unsupported explicit digest references."""
        with patch("regshape.cli.referrer.head_manifest") as head:
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", f"{REGISTRY}/{REPO}@md5:abc",
            ])
        assert result.exit_code == 2
        assert "digest references must use sha256 or sha512" in result.stderr
        head.assert_not_called()
        client.head.assert_not_called()

    @pytest.mark.parametrize("fetch_all", [False, True])
    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("write_file", [False, True])
    def test_tag_preserves_filter_pagination_and_output_options(
        self,
        client: MagicMock,
        tmp_path: Path,
        fetch_all: bool,
        as_json: bool,
        write_file: bool,
    ) -> None:
        """Apply every output/pagination combination to the resolved subject digest."""
        referrers = _referrer_list([_descriptor()])
        outfile = tmp_path / "referrers.txt"
        operation = "list_referrers_all" if fetch_all else "list_referrers"
        args = ["referrer", "list", "-i", TAG_REF, "--artifact-type", SBOM_TYPE]
        if fetch_all:
            args.append("--all")
        if as_json:
            args.append("--json")
        if write_file:
            args.extend(["--output", str(outfile)])
        with (
            patch("regshape.cli.referrer.head_manifest",
                  return_value=(DIGEST, MANIFEST_MT, 123)) as head,
            patch(f"regshape.cli.referrer.{operation}",
                  return_value=referrers) as list_fn,
        ):
            result = _runner().invoke(regshape, args)

        assert result.exit_code == 0, result.output
        head.assert_called_once()
        list_fn.assert_called_once_with(
            client=client, repo=REPO, digest=DIGEST, artifact_type=SBOM_TYPE,
        )
        content = outfile.read_text() if write_file else result.stdout
        if write_file:
            assert result.stdout == ""
        if as_json:
            assert json.loads(content) == referrers.to_dict()
        elif write_file:
            assert content == f"{DIGEST} {SBOM_TYPE} 1234\n"
        else:
            assert content.splitlines()[-1].split() == [DIGEST, SBOM_TYPE, "1234"]

    @pytest.mark.parametrize("error", [
        ManifestError("Manifest not found"),
        AuthError("Authentication failed"),
        requests.exceptions.Timeout("Tag lookup timed out"),
    ])
    def test_resolution_failure_stops_before_listing_or_writing(
        self, client: MagicMock, tmp_path: Path, error: Exception
    ) -> None:
        """Report lookup failures on stderr without touching an existing output file."""
        outfile = tmp_path / "referrers.json"
        outfile.write_text("unchanged\n")
        with (
            patch("regshape.cli.referrer.head_manifest", side_effect=error),
            patch("regshape.cli.referrer.list_referrers") as list_fn,
            patch("regshape.cli.referrer.list_referrers_all") as list_all,
        ):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", TAG_REF, "--all", "--json",
                "--output", str(outfile),
            ])

        assert result.exit_code == 1
        assert result.stdout == ""
        assert TAG_REF in result.stderr
        assert str(error) in result.stderr
        list_fn.assert_not_called()
        list_all.assert_not_called()
        assert outfile.read_text() == "unchanged\n"

    @pytest.mark.parametrize("digest", ["", " "])
    def test_missing_digest_header_stops_before_listing(
        self, client: MagicMock, digest: str
    ) -> None:
        """Reject successful HEAD responses that cannot identify a subject digest."""
        with (
            patch("regshape.cli.referrer.head_manifest",
                  return_value=(digest, MANIFEST_MT, 123)),
            patch("regshape.cli.referrer.list_referrers") as list_fn,
        ):
            result = _runner().invoke(regshape, [
                "referrer", "list", "-i", TAG_REF,
            ])
        assert result.exit_code == 1
        assert "Docker-Content-Digest" in result.stderr
        assert TAG_REF in result.stderr
        list_fn.assert_not_called()

    @pytest.mark.parametrize(
        ("status_code", "exit_code", "message"),
        [(200, 0, ""), (404, 1, "Manifest not found"),
         (401, 1, "Authentication failed"), (500, 1, "Registry error")],
    )
    def test_cli_wires_manifest_head_to_referrer_get(
        self, client: MagicMock, status_code: int, exit_code: int, message: str
    ) -> None:
        """Exercise real library operations and their HTTP error mapping from the CLI."""
        referrers = _referrer_list([_descriptor()])
        client.head.return_value = _response(
            status_code, headers={"Docker-Content-Digest": DIGEST},
        )
        client.get.return_value = _response(200, referrers.to_json())

        result = _runner().invoke(regshape, [
            "referrer", "list", "-i", TAG_REF, "--json",
        ])

        assert result.exit_code == exit_code, result.output
        expected_calls = [
            call.head(
                f"/v2/{REPO}/manifests/v1.0",
                headers={"Accept": ",".join(sorted(ALL_MANIFEST_MEDIA_TYPES))},
            ),
        ]
        if exit_code == 0:
            assert json.loads(result.stdout) == referrers.to_dict()
            expected_calls.append(call.get(f"/v2/{REPO}/referrers/{DIGEST}", params=None))
        else:
            assert message in result.stderr
            assert TAG_REF in result.stderr
            client.get.assert_not_called()
        assert client.method_calls == expected_calls

    def test_help_describes_tag_and_digest_support(self) -> None:
        """Expose the newly accepted tag form in the command help."""
        result = _runner().invoke(regshape, ["referrer", "list", "--help"])
        assert result.exit_code == 0
        assert "registry/repo:tag" in result.output
        assert "registry/repo@sha256:abc..." in result.output
        assert "Tag-only references are rejected" not in result.output

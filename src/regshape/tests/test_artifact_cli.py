"""Production artifact-option parity through real command/library paths and mocked HTTP."""

import io
import json
from collections.abc import Iterator
from itertools import product
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import click
import pytest
import requests
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.decorators import TelemetryConfig, configure_telemetry
from regshape.libs.models.mediatype import OCI_IMAGE_INDEX, OCI_IMAGE_MANIFEST
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - register shared fixture
from regshape.tests.test_manifest_cli import _INDEX_JSON, _MANIFEST_JSON

REGISTRY = "registry.example.invalid:5443"
REPO = "team/package"
BASE = f"{REGISTRY}/{REPO}"
DIGEST = "sha256:" + "a" * 64
ALIASES = ("--artifact", "-a", "--image-ref", "-i")
COMMANDS = (
    ("manifest", "get"),
    ("manifest", "info"),
    ("manifest", "descriptor"),
    ("manifest", "put"),
    ("manifest", "delete"),
    ("tag", "list"),
    ("tag", "delete"),
    ("referrer", "list"),
)
_artifact = json.loads(_MANIFEST_JSON)
_artifact["artifactType"] = "application/vnd.example.package"
_artifact["config"]["mediaType"] = "application/vnd.oci.empty.v1+json"
_artifact["layers"][0]["mediaType"] = "application/octet-stream"
ARTIFACT = json.dumps(_artifact)


@pytest.fixture(autouse=True)
def reset_telemetry() -> Iterator[None]:
    """Keep global telemetry state independent of neighboring test modules."""
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(TelemetryConfig())


def _reference(command: tuple[str, str]) -> str:
    """Select an existing valid target for each command's unchanged restrictions."""
    if command in (("manifest", "delete"), ("referrer", "list")):
        return f"{BASE}@{DIGEST}"
    return BASE if command == ("tag", "list") else BASE + ":v1"


def _arguments(
    command: tuple[str, str], alias: str, reference: str | None = None
) -> list[str]:
    """Supply normal target/input flags without introducing new command semantics."""
    result = [*command, alias, _reference(command) if reference is None else reference]
    if command == ("manifest", "put"):
        result.append("--stdin")
    return result


def _response(method: str, url: str, **kwargs: object) -> requests.Response:
    """Handle only expected registry endpoints; no real sockets or credentials are used."""
    response = requests.Response()
    response.url = url
    response.status_code = 200
    path = urlsplit(url).path
    response.headers["Content-Type"] = OCI_IMAGE_MANIFEST
    response.headers["Docker-Content-Digest"] = DIGEST
    if "/manifests/" in path:
        assert method in ("GET", "HEAD", "PUT", "DELETE")
        response._content = ARTIFACT.encode() if method == "GET" else b""
        response.headers["Content-Length"] = str(len(ARTIFACT.encode()))
        if method == "PUT":
            assert kwargs["data"] == ARTIFACT.encode()
            response.status_code = 201
        elif method == "DELETE":
            response.status_code = 202
    elif path == f"/v2/{REPO}/tags/list":
        assert method == "GET"
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"name": REPO, "tags": ["v1", "v2"]}).encode()
    elif path == f"/v2/{REPO}/referrers/{DIGEST}":
        assert method == "GET"
        response.headers["Content-Type"] = OCI_IMAGE_INDEX
        response._content = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_INDEX,
                "manifests": [],
            }
        ).encode()
    else:
        raise AssertionError(f"Unexpected synthetic endpoint: {method} {path}")
    response._content_consumed = True
    return response


@pytest.fixture
def network() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Mock only credential lookup and the HTTP boundary, keeping production operations real."""
    with (
        patch(
            "regshape.libs.transport.client.resolve_credentials",
            return_value=(None, None),
        ) as credentials,
        patch(
            "regshape.libs.decorators.call_details.requests.request",
            side_effect=_response,
        ) as http,
    ):
        yield credentials, http


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("insecure", [False, True])
def test_command_aliases_preserve_results_and_requests(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    command: tuple[str, str],
    alias: str,
    insecure: bool,
) -> None:
    """All eight commands use equivalent requests/output through each of the four spellings."""
    credentials, http = network
    prefix = ["--insecure"] if insecure else []
    baseline = runner.invoke(
        regshape, prefix + _arguments(command, "--image-ref"), input=ARTIFACT
    )
    assert baseline.exit_code == 0, baseline.output
    original_requests = list(http.call_args_list)
    original_credentials = list(credentials.call_args_list)
    assert len(original_requests) == len(original_credentials) == 1
    http.reset_mock()
    credentials.reset_mock()
    result = runner.invoke(
        regshape, prefix + _arguments(command, alias), input=ARTIFACT
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == baseline.stdout
    assert result.stderr == baseline.stderr == ""
    assert http.call_args_list == original_requests
    assert credentials.call_args_list == original_credentials
    method, url = http.call_args.args
    assert url.startswith(
        ("http://" if insecure else "https://") + REGISTRY + f"/v2/{REPO}/"
    )
    expected_method = {
        "info": "HEAD",
        "descriptor": "HEAD",
        "put": "PUT",
        "delete": "DELETE",
    }.get(command[1], "GET")
    assert method == expected_method
    if command == ("manifest", "get"):
        assert (
            json.loads(result.stdout)["artifactType"]
            == "application/vnd.example.package"
        )


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("first,second", list(product(ALIASES, repeat=2)))
def test_conflicts_precede_credentials_and_network(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    command: tuple[str, str],
    first: str,
    second: str,
) -> None:
    """Different repeats, including only legacy flags, cannot reach any registry side effect."""
    credentials, http = network
    args = _arguments(command, first) + [second, BASE + ":different"]
    result = runner.invoke(regshape, args, input=ARTIFACT)
    assert result.exit_code == 2 and result.stdout == ""
    assert (
        "Error: Conflicting reference options. Supply one artifact reference."
        in result.stderr
    )
    assert BASE not in result.stderr
    credentials.assert_not_called()
    http.assert_not_called()


@pytest.mark.parametrize("command", COMMANDS)
def test_identical_mixed_aliases_execute_once(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    command: tuple[str, str],
) -> None:
    """Matching new/old target values remain one operation, including manifest/tag mutations."""
    credentials, http = network
    result = runner.invoke(
        regshape,
        _arguments(command, "-a") + ["-i", _reference(command)],
        input=ARTIFACT,
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert credentials.call_count == http.call_count == 1


@pytest.mark.parametrize("command", COMMANDS)
def test_help_and_missing_target_have_no_side_effects(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    command: tuple[str, str],
) -> None:
    """Real help names only legacy aliases as deprecated and retains normal required-option errors."""
    credentials, http = network
    result = runner.invoke(regshape, [*command, "--help"], terminal_width=120)
    assert result.exit_code == 0 and result.stderr == ""
    text = " ".join(result.stdout.split())
    for alias in ALIASES:
        assert alias in text
    assert "Legacy --image-ref / -i: Deprecated in 0.2; use --artifact / -a." in text
    assert ("REPOSITORY" if command == ("tag", "list") else "ARTIFACT") in text
    missing = runner.invoke(regshape, list(command), input=ARTIFACT)
    assert missing.exit_code == 2 and missing.stdout == ""
    credentials.assert_not_called()
    http.assert_not_called()


@pytest.mark.parametrize(
    "command,reference,exit_code",
    [
        (("manifest", "delete"), BASE + ":v1", 2),
        (("tag", "delete"), BASE + "@" + DIGEST, 2),
        (("referrer", "list"), BASE + "@unsupported:abc", 2),
        (("manifest", "get"), "team/package:v1", 1),
    ],
)
@pytest.mark.parametrize("alias", ALIASES)
def test_existing_target_restrictions_remain(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    command: tuple[str, str],
    reference: str,
    exit_code: int,
    alias: str,
) -> None:
    """Renaming must not loosen digest-only/tag-only or embedded-registry validation."""
    credentials, http = network
    result = runner.invoke(regshape, _arguments(command, alias, reference))
    assert result.exit_code == exit_code and result.stdout == ""
    assert "Error" in result.stderr
    credentials.assert_not_called()
    http.assert_not_called()


@pytest.mark.parametrize("reference", [BASE, BASE + ":v1", BASE + "@" + DIGEST])
@pytest.mark.parametrize("all_pages", [False, True])
def test_tag_list_still_ignores_target_suffix(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    reference: str,
    all_pages: bool,
) -> None:
    """Repository listing keeps its existing pagination parameters and suffix behavior."""
    _, http = network
    args = ["tag", "list", "-a", reference, "--n", "5", "--last", "v0"]
    result = runner.invoke(regshape, args + (["--all"] if all_pages else []))
    assert result.exit_code == 0 and result.stdout == "v1\nv2\n"
    assert http.call_count == 1
    suffix = "?n=5&last=v0" if all_pages else ""
    assert http.call_args.args == (
        "GET",
        f"https://{REGISTRY}/v2/{REPO}/tags/list{suffix}",
    )
    assert http.call_args.kwargs["params"] == (
        None if all_pages else {"n": 5, "last": "v0"}
    )
    if all_pages:
        assert http.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("reference,tag", [(BASE, "latest"), (BASE + ":v1", "v1")])
@pytest.mark.parametrize("all_pages", [False, True])
def test_referrer_tag_resolution_and_filter_unchanged(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    reference: str,
    tag: str,
    all_pages: bool,
) -> None:
    """Tag inputs still make one HEAD then the digest-bound referrer query with its filter."""
    _, http = network
    args = [
        "referrer",
        "list",
        "-a",
        reference,
        "--artifact-type",
        "application/vnd.example.package",
        "--json",
    ]
    result = runner.invoke(regshape, args + (["--all"] if all_pages else []))
    assert result.exit_code == 0 and json.loads(result.stdout)["manifests"] == []
    assert [call.args for call in http.call_args_list] == [
        ("HEAD", f"https://{REGISTRY}/v2/{REPO}/manifests/{tag}"),
        ("GET", f"https://{REGISTRY}/v2/{REPO}/referrers/{DIGEST}"),
    ]
    assert http.call_args.kwargs["params"] == {
        "artifactType": "application/vnd.example.package"
    }


@pytest.mark.parametrize("extra", [[], ["--raw"], ["--part", "layers"]])
@pytest.mark.parametrize("alias", ["--artifact", "-i"])
def test_manifest_file_and_structured_telemetry_stay_separate(
    tmp_path: Path,
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    extra: list[str],
    alias: str,
) -> None:
    """Manifest content, telemetry and log files retain their existing channels and schemas."""
    _, http = network
    output, log = tmp_path / "manifest.json", tmp_path / "telemetry.log"

    def configure_capture(config: TelemetryConfig) -> None:
        """Avoid Click 8.1's unflushed stderr capture while retaining real telemetry writes."""
        assert isinstance(config.output, io.TextIOWrapper)
        config.output.reconfigure(write_through=True)
        configure_telemetry(config)

    with patch(
        "regshape.libs.decorators.configure_telemetry", side_effect=configure_capture
    ):
        result = runner.invoke(
            regshape,
            [
                "--log-file",
                str(log),
                "manifest",
                "get",
                alias,
                BASE + ":v1",
                "--output",
                str(output),
                "--accept",
                OCI_IMAGE_MANIFEST,
                "--time-scenarios",
                "--telemetry-format",
                "json",
                *extra,
            ],
        )
    assert result.exit_code == 0, result.output
    assert result.stdout == "" and http.call_count == 1
    document = json.loads(output.read_text())
    assert document == (
        _artifact["layers"] if extra == ["--part", "layers"] else _artifact
    )
    if extra == ["--raw"]:
        assert output.read_text() == ARTIFACT + "\n"
    assert http.call_args.kwargs["headers"]["Accept"] == OCI_IMAGE_MANIFEST
    assert result.stderr and log.read_text()
    for text in (result.stderr, log.read_text()):
        assert "Deprecated" not in text and "\x1b" not in text
        assert all(isinstance(json.loads(line), dict) for line in text.splitlines())


@pytest.mark.parametrize("alias", ["--artifact", "-i"])
def test_manifest_index_and_raw_modes_remain_supported(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    alias: str,
) -> None:
    """An index response is still parsed as an index, not converted into an image manifest."""
    _, http = network
    response = _response("GET", f"https://{REGISTRY}/v2/{REPO}/manifests/v1")
    response.headers["Content-Type"] = OCI_IMAGE_INDEX
    response._content = _INDEX_JSON.encode()
    http.side_effect = None
    http.return_value = response
    result = runner.invoke(regshape, ["manifest", "get", alias, BASE + ":v1"])
    assert result.exit_code == 0 and json.loads(result.stdout) == json.loads(
        _INDEX_JSON
    )


@pytest.mark.parametrize("alias", ["--artifact", "--image-ref"])
def test_manifest_put_file_and_content_type_override(
    tmp_path: Path,
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    alias: str,
) -> None:
    """File-backed uploads retain exact payload bytes and explicit content negotiation."""
    _, http = network
    path = tmp_path / "manifest.json"
    path.write_text(ARTIFACT)
    content_type = "application/vnd.docker.distribution.manifest.v2+json"
    result = runner.invoke(
        regshape,
        [
            "manifest",
            "put",
            alias,
            BASE + ":v1",
            "--file",
            str(path),
            "--content-type",
            content_type,
        ],
    )
    assert result.exit_code == 0 and result.stdout == f"Pushed: {DIGEST}\n"
    assert http.call_count == 1 and http.call_args.kwargs["data"] == ARTIFACT.encode()
    assert http.call_args.kwargs["headers"]["Content-Type"] == content_type


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("failure", ["network", "auth"])
def test_operational_errors_are_identical_for_new_and_legacy_aliases(
    runner: CliRunner,
    network: tuple[MagicMock, MagicMock],
    command: tuple[str, str],
    failure: str,
) -> None:
    """Failures after selection preserve command error messages and do not become warnings/success."""
    _, http = network
    if failure == "network":
        http.side_effect = requests.exceptions.ConnectionError(
            "synthetic network failure"
        )
    else:
        http.side_effect = None
        response = requests.Response()
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="synthetic-registry"'
        response._content = b""
        response._content_consumed = True
        http.return_value = response
    old = runner.invoke(regshape, _arguments(command, "-i"), input=ARTIFACT)
    http.reset_mock()
    new = runner.invoke(regshape, _arguments(command, "-a"), input=ARTIFACT)
    assert old.exit_code == new.exit_code == 1
    assert old.stdout == new.stdout == ""
    assert old.stderr == new.stderr and "Error" in new.stderr
    assert http.call_count == 1


def test_unaffected_option_surfaces(runner: CliRunner) -> None:
    """Local Docker/image/index selectors and split blob targets keep their existing meanings."""
    for args, required in [
        (["docker", "export"], ("--image", "-i")),
        (["docker", "push"], ("--image", "-i", "--dest")),
        (["layout", "annotate", "layer"], ("--index", "-i")),
        (["blob", "get"], ("--repo", "--digest", "--no-progress")),
        (["layout", "push"], ("--dest", "--no-progress")),
    ]:
        result = runner.invoke(regshape, [*args, "--help"])
        assert result.exit_code == 0
        assert all(option in result.stdout for option in required)
        assert (
            "--artifact " not in result.stdout
            and "Deprecated in 0.2" not in result.stdout
        )
    for group_name in ("manifest", "tag", "referrer"):
        for command in regshape.commands[group_name].commands.values():
            options = [
                param for param in command.params if isinstance(param, click.Option)
            ]
            reference = next(param for param in options if "--artifact" in param.opts)
            assert reference.name == "image_ref" and not getattr(
                reference, "deprecated", False
            )

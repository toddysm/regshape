"""Byte-exact directory/archive push through actual commands, clients and operations.

The fake registry owns uploaded objects and rejects dangling document edges;
only the HTTP socket boundary and credential resolution are replaced. Native
storage tests skip unsupported hosts, while explicit fail-closed guards run there.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests

from regshape.cli.main import regshape
from regshape.libs.artifacts import ArtifactFormat, pull_artifact
from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST, REF_NAME
from regshape.libs.errors import ArtifactError, AuthError, BlobError, ManifestError
from regshape.libs.layout.operations import (
    LayoutPushPlan,
    execute_layout_push,
    push_layout,
)
from regshape.libs.manifests import push_manifest
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.tests.artifact_fixtures import (
    StreamResponse,
    address,
    document,
    edge,
    write_layout,
)
from regshape.tests.test_docker_operations import _make_single_platform_tar
from regshape.tests.test_layout_push import _runner


@pytest.fixture(autouse=True)
def native_push(request: pytest.FixtureRequest) -> None:
    """Skip native snapshot cases on Windows, never the explicit guards or pure acknowledgement tests."""
    independent = request.node.name.startswith(("test_ack_", "test_unsupported_platform"))
    if not independent and sys.platform not in ("linux", "darwin"):
        pytest.skip("Strict push requires Linux/Darwin native safe snapshots")


def _graph(kind: str, algorithm: str) -> tuple[bytes, dict[str, bytes]]:
    """Build raw, all-platform subject/shared/nested closures using the chosen digest algorithm."""
    config, layer = b'{"opaque":"config"}', b"\x00not a tar payload\xff"

    def descriptor(raw: bytes, media_type: str = "application/octet-stream", **extra: object) -> dict:
        """Address fixture bytes without rewriting them or coercing descriptor metadata."""
        return edge(raw, media_type, digest=address(raw, algorithm), **extra)

    subject = document(children=[])
    first = document(
        config=descriptor(config), layers=[descriptor(layer)],
        subject=descriptor(subject, OCI_INDEX) if kind in ("subject", "nested") else None,
    )
    second = document(config=descriptor(config), layers=[descriptor(layer), descriptor(config)])
    objects = {address(raw, algorithm): raw for raw in (config, layer, first)}
    root = first
    if kind in ("subject", "nested"):
        objects[address(subject, algorithm)] = subject
    if kind in ("index", "nested", "shared"):
        children = [descriptor(first, OCI_MANIFEST, platform={"os": "linux", "architecture": "amd64"})]
        if kind in ("shared", "nested"):
            children.append(descriptor(second, OCI_MANIFEST, platform={"os": "windows", "architecture": "arm64"}))
            objects[address(second, algorithm)] = second
        root = document(children=children)
        objects[address(root, algorithm)] = root
        if kind == "nested":
            root = document(children=[descriptor(root, OCI_INDEX), descriptor(first, OCI_MANIFEST)],
                            subject=descriptor(subject, OCI_INDEX))
    objects[address(root, algorithm)] = root
    return root, objects


class ObjectRegistry:
    """Network-free registry with distinct source/destination stores and dependency enforcement."""

    def __init__(self, root: bytes, objects: dict[str, bytes], algorithm: str = "sha256") -> None:
        """Install the source closure; destination begins empty and records every response/write."""
        self.root = root
        self.source = objects
        self.algorithm = algorithm
        self.objects: dict[str, bytes] = {}
        self.references: dict[str, str] = {}
        self.documents: list[tuple[str, bytes]] = []
        self.buffers: dict[str, bytearray] = {}
        self.responses: list[StreamResponse] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_reference: str | None = None
        self.lose_reference: str | None = None
        self.head_status: int | None = None

    def response(self, status: int, raw: bytes = b"", **headers: str) -> StreamResponse:
        """Return a real Requests/urllib3 response and retain close evidence."""
        response = StreamResponse(raw, status=status, headers=headers)
        self.responses.append(response)
        return response

    def respond(self, method: str, url: str, *, data=None, params=None, **kwargs) -> StreamResponse:
        """Serve raw gets and both uploads, rejecting documents with unpublished dependencies."""
        path = unquote(urlsplit(url).path)
        reference = path.rsplit("/", 1)[-1]
        self.calls.append((method, path))
        if method == "GET":
            raw = self.root if reference == "moving" else self.source[reference]
            return self.response(200, raw, **{"Docker-Content-Digest": address(raw, self.algorithm)})
        if method == "HEAD":
            status = self.head_status or (200 if reference in self.objects else 404)
            return self.response(status, **{
                "Content-Length": str(len(self.objects.get(reference, b""))),
                "Docker-Content-Digest": reference,
            })
        if method == "POST":
            session = str(len(self.buffers))
            self.buffers[session] = bytearray()
            return self.response(202, Location=f"/v2/dest/blobs/uploads/{session}")
        if method == "PATCH":
            self.buffers[reference].extend(data)
            return self.response(202)
        assert method == "PUT"
        if "/manifests/" in path:
            assert isinstance(data, bytes)
            if reference == self.fail_reference:
                return self.response(500)
            body = json.loads(data)
            dependencies = body.get("manifests", []) + body.get("layers", [])
            if "config" in body:
                dependencies.append(body["config"])
            if "subject" in body:
                dependencies.append(body["subject"])
            for dependency in dependencies:
                assert self.objects[dependency["digest"]] is not None, "dangling document edge"
                if dependency["mediaType"] in (OCI_INDEX, OCI_MANIFEST):
                    assert dependency["digest"] in self.references, "document not acknowledged by digest"
            digest = address(data, self.algorithm)
            if ":" in reference:
                assert address(data, reference.split(":")[0]) == reference
                digest = reference
            self.objects[digest] = data
            self.references[reference] = digest
            self.documents.append((reference, data))
            if reference == self.lose_reference:
                raise requests.ConnectionError("synthetic response lost after commit")
            response = self.response(201, **{"Docker-Content-Digest": digest})
            response.request = requests.Request(method, url, data=data).prepare()
            return response
        query = dict(parse_qsl(urlsplit(url).query))
        query.update(dict(params or {}))
        digest = query["digest"]
        if hasattr(data, "read"):
            chunks = []
            while chunk := data.read(13):
                chunks.append(chunk)
            raw = b"".join(chunks)
        else:
            assert data == b""
            raw = bytes(self.buffers[reference])
        assert address(raw, digest.split(":")[0]) == digest
        self.objects[digest] = raw
        return self.response(201, **{"Docker-Content-Digest": digest})


def _client() -> RegistryClient:
    """Construct an actual registry client; tests replace credentials and HTTP, not operations."""
    return RegistryClient(TransportConfig(registry="registry.example"))


@pytest.mark.parametrize("kind", ["simple", "index", "nested", "subject", "shared"])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize("chunked", [False, True])
def test_pull_push_command_round_trip(
    tmp_path: Path, kind: str, algorithm: str, archive: bool, tagged: bool, chunked: bool,
) -> None:
    """Actual pull/CLI push preserve every raw byte/root identity into an empty registry."""
    root, objects = _graph(kind, algorithm)
    registry = ObjectRegistry(root, objects, algorithm)
    source = tmp_path / ("layout.tgz" if archive else "layout")
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)) as credentials,
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        pulled = pull_artifact(
            _client(), "source", "moving" if tagged else address(root, algorithm), source,
            output_format=ArtifactFormat.OCI_TAR_GZIP if archive else ArtifactFormat.OCI,
        )
        command = ["layout", "push", "--path", str(source), "--dest", "registry.example/dest", "--json"]
        if chunked:
            command += ["--chunked", "--chunk-size", "13"]
        credentials.reset_mock()
        calls_before_dry_run = len(registry.calls)
        dry = _runner().invoke(regshape, [*command, "--dry-run"])
        assert dry.exit_code == 0, dry.output
        credentials.assert_not_called()
        assert len(registry.calls) == calls_before_dry_run
        result = _runner().invoke(regshape, command)
    assert result.exit_code == 0, (result.output, result.exception)
    actual, planned = json.loads(result.output), json.loads(dry.output)
    assert [(item["digest"], item["reference"]) for item in actual["manifests"]] == [
        (item["digest"], item["reference"]) for item in planned["manifests"]
    ]
    assert registry.objects == objects
    assert pulled.root.digest == address(root, algorithm)
    root_reference = "moving" if tagged else address(root, algorithm)
    assert registry.references[root_reference] == pulled.root.digest
    assert registry.documents[-1] == (root_reference, root)
    assert len({(address(raw, algorithm), ref) for ref, raw in registry.documents}) == len(registry.documents)
    assert actual["summary"]["manifests_pushed"] == len(registry.documents)
    assert actual["summary"]["blobs_uploaded"] == 2
    assert actual["summary"]["bytes_uploaded"] == sum(
        len(raw) for digest, raw in objects.items() if not raw.startswith(b"{\n")
    )
    assert all(item["blobs"] == [] for item in actual["manifests"] if item["media_type"] == OCI_INDEX)
    assert all(response.close_calls == 1 for response in registry.responses)


def _local(tmp_path: Path, kind: str = "nested") -> tuple[Path, bytes, dict[str, bytes]]:
    """Create a native source closure without using the production snapshot writer."""
    root, objects = _graph(kind, "sha256")
    source = tmp_path / "layout"
    write_layout(source, root, objects, root_type=json.loads(root)["mediaType"])
    return source, root, objects


@pytest.mark.parametrize("damage", ["missing", "corrupt", "size"])
@pytest.mark.parametrize("dry", [False, True])
def test_local_damage_precedes_all_credentials_and_network(tmp_path: Path, damage: str, dry: bool) -> None:
    """Strict validation checks deep payloads before constructing any client, even during dry-run."""
    source, _, objects = _local(tmp_path)
    digest = next(digest for digest, raw in objects.items() if raw.startswith(b"\x00"))
    payload = source / "blobs" / digest.replace(":", "/")
    if damage == "missing":
        payload.unlink()
    else:
        payload.write_bytes(b"x" * (len(objects[digest]) + (damage == "size")))
    with (
        patch("regshape.cli.layout.RegistryClient") as client,
        patch("regshape.libs.transport.client.resolve_credentials") as credentials,
        patch("regshape.libs.decorators.call_details.requests.request") as network,
    ):
        command = ["layout", "push", "--path", str(source), "--dest", "registry.example/dest", "--json"]
        result = _runner().invoke(regshape, command + (["--dry-run"] if dry else []))
    assert result.exit_code == 1
    assert not result.stdout
    client.assert_not_called()
    credentials.assert_not_called()
    network.assert_not_called()


@pytest.mark.parametrize("tag", ["", "a/b", "a:b", "a?b", "a#b", "a%b", " a", "a\nb", "é", "x" * 129, 1, None])
@pytest.mark.parametrize("override", [None, "valid"])
def test_invalid_source_tags_fail_even_with_override(tmp_path: Path, tag, override: str | None) -> None:
    """Validate every present original wrapper ref-name without sanitizing or ignoring overrides."""
    source, _, _ = _local(tmp_path)
    wrapper = json.loads((source / "index.json").read_bytes())
    wrapper["manifests"][0]["annotations"] = {REF_NAME: tag}
    (source / "index.json").write_text(json.dumps(wrapper))
    client = MagicMock()
    with pytest.raises(ArtifactError):
        push_layout(source, client, "dest", tag_override=override)
    assert not client.mock_calls


@pytest.mark.parametrize("destination", [
    "registry.example/dest@sha256:" + "a" * 64, "registry.example/dest:",
    "registry.example/dest:bad?query", "registry.example/dest:bad#fragment",
    "registry.example/dest:bad%20", "registry.example/dest:\ncontrol",
    "registry.example/dest:é", "registry.example/dest:" + "x" * 129,
])
@pytest.mark.parametrize("dry", [False, True])
def test_invalid_destinations_precede_client(tmp_path: Path, destination: str, dry: bool) -> None:
    """Digest destinations and invalid literal tags fail as usage errors without credentials."""
    source, _, _ = _local(tmp_path)
    with patch("regshape.cli.layout.RegistryClient") as client:
        result = _runner().invoke(regshape, [
            "layout", "push", "--path", str(source), "--dest", destination,
            *(["--dry-run"] if dry else []),
        ])
    assert result.exit_code == 2
    client.assert_not_called()


def test_root_aliases_require_early_digest_and_final_tag(tmp_path: Path) -> None:
    """A top-level document also used as a child is acknowledged by digest before any parent/tag."""
    source, root, objects = _local(tmp_path, "shared")
    wrapper = json.loads((source / "index.json").read_bytes())
    child = dict(json.loads(root)["manifests"][0])
    child["annotations"] = {REF_NAME: "child"}
    wrapper["manifests"] += [child, child, {**child, "annotations": {REF_NAME: "alias"}}]
    wrapper["manifests"][0]["annotations"] = {REF_NAME: "root"}
    (source / "index.json").write_text(json.dumps(wrapper))
    registry = ObjectRegistry(root, objects)
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        result = push_layout(source, _client(), "dest")
    references = [reference for reference, _ in registry.documents]
    assert references[0] == child["digest"]
    assert references[-3:] == ["root", "child", "alias"]
    assert references.count("child") == 1
    assert result.manifests_pushed == len(references) == 5


def test_single_nested_root_accepts_override_and_snapshot_is_private(tmp_path: Path) -> None:
    """One top-level index may override its tag; later mutations of originals cannot affect uploads."""
    source, root, objects = _local(tmp_path)
    registry = ObjectRegistry(root, objects)
    with LayoutPushPlan(source, "override") as plan:
        for path in (source / "blobs").glob("*/*"):
            path.write_bytes(b"mutated original")
        (source / "index.json").unlink()
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        ):
            result = execute_layout_push(plan, _client(), "dest")
    assert registry.objects == objects and registry.documents[-1] == ("override", root)
    assert result.manifests[-1].reference == "override"
    with pytest.raises(RuntimeError, match="context"):
        execute_layout_push(plan, MagicMock(), "dest")


@pytest.mark.parametrize("status", [None, 401, 403, 429, 500])
def test_only_genuine_404_permits_upload(tmp_path: Path, status: int | None) -> None:
    """Unclassified BlobError is not evidence that content is absent."""
    source, _, _ = _local(tmp_path)
    error = BlobError("synthetic", status_code=status)
    with (
        patch("regshape.libs.blobs.head_blob", side_effect=error),
        patch("regshape.libs.blobs.upload_blob_stream") as upload,
        patch("regshape.libs.manifests.push_manifest") as manifest,
        pytest.raises(BlobError) as caught,
    ):
        push_layout(source, MagicMock(), "dest")
    assert caught.value is error
    upload.assert_not_called()
    manifest.assert_not_called()


@pytest.mark.parametrize("status", [202, 204, 301, 401, 403, 429, 500])
def test_head_response_must_confirm_presence(tmp_path: Path, status: int) -> None:
    """Actual client/HEAD errors close their responses and never start an upload session."""
    source, root, objects = _local(tmp_path)
    registry = ObjectRegistry(root, objects)
    registry.head_status = status
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        pytest.raises((AuthError, BlobError)),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example", enable_middleware=False))
        push_layout(source, client, "dest")
    assert [method for method, _ in registry.calls] == ["HEAD"]
    assert all(response.close_calls == 1 for response in registry.responses)


@pytest.mark.parametrize("lost", [False, True])
def test_partial_state_and_uncertain_root_never_rollback(tmp_path: Path, lost: bool) -> None:
    """Definite child failure leaves no root; lost final response explicitly admits possible commit."""
    source, root, objects = _local(tmp_path)
    registry = ObjectRegistry(root, objects)
    if lost:
        registry.lose_reference = "final"
    else:
        registry.fail_reference = json.loads(root)["manifests"][0]["digest"]
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        pytest.raises(ManifestError) as caught,
    ):
        push_layout(source, _client(), "dest", tag_override="final")
    assert registry.objects
    assert not any(method == "DELETE" for method, _ in registry.calls)
    assert ("final" in registry.references) is lost
    if lost:
        assert "outcome uncertain" in str(caught.value)
        assert isinstance(caught.value.__cause__, requests.ConnectionError)
    assert all(response.close_calls == 1 for response in registry.responses)


@pytest.mark.parametrize("name", [
    "max-manifest-bytes", "max-objects", "max-depth", "max-total-bytes",
    "max-archive-bytes", "max-archive-input-bytes",
])
def test_cli_limit_types_precede_client(tmp_path: Path, name: str) -> None:
    """Every exposed safety cap rejects zero through Click's positive integer contract."""
    source, _, _ = _local(tmp_path)
    with patch("regshape.cli.layout.RegistryClient") as client:
        result = _runner().invoke(regshape, [
            "layout", "push", "--path", str(source), "--dest", "registry.example/dest",
            "--" + name, "0",
        ])
    assert result.exit_code == 2
    client.assert_not_called()


@pytest.mark.parametrize("dry", [False, True])
def test_manifest_limit_used_by_real_and_dry_run(tmp_path: Path, dry: bool) -> None:
    """A positive but insufficient budget fails the shared plan before client construction."""
    source, _, _ = _local(tmp_path)
    with patch("regshape.cli.layout.RegistryClient") as client:
        result = _runner().invoke(regshape, [
            "layout", "push", "--path", str(source), "--dest", "registry.example/dest",
            "--max-manifest-bytes", "1", *(["--dry-run"] if dry else []),
        ])
    assert result.exit_code == 1
    client.assert_not_called()


def test_unsupported_platform_guard(tmp_path: Path) -> None:
    """Missing no-follow handle-relative primitives fail before any client/credential access."""
    source = tmp_path / "layout"
    source.mkdir()
    with (
        patch("regshape.libs.artifacts.storage.os.supports_dir_fd", set()),
        patch("regshape.cli.layout.RegistryClient") as client,
    ):
        for dry in (False, True):
            result = _runner().invoke(regshape, [
                "layout", "push", "--path", str(source), "--dest", "registry.example/dest",
                *(["--dry-run"] if dry else []),
            ])
            assert result.exit_code == 1 and "unsupported" in result.stderr
    client.assert_not_called()


@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
@pytest.mark.parametrize("report", ["same", "other", "missing", "empty", "wrong", "invalid"])
@pytest.mark.parametrize("status", [200, 201, 202, 204, 400])
def test_ack_status_digest_and_response_ownership(algorithm: str, report: str, status: int) -> None:
    """Strict manifest PUT verifies exact bytes, status and either digest algorithm, then closes once."""
    raw = b'{ "schemaVersion": 2, "arbitrary": true }\n'
    expected = address(raw, algorithm)
    reported = {
        "same": expected, "other": address(raw, "sha512" if algorithm == "sha256" else "sha256"),
        "missing": None, "empty": "", "wrong": address(b"wrong", algorithm), "invalid": "sha256:no",
    }[report]
    response = StreamResponse(b"", status=status, headers=(
        {} if reported is None else {"Docker-Content-Digest": reported}
    ))
    response.request = requests.Request("PUT", "https://registry.example/v2/dest/manifests/tag", data=raw).prepare()
    client = MagicMock()
    client.put.return_value = response
    if status == 201 and report in ("same", "other", "missing"):
        assert push_manifest(client, "dest", "tag", raw, OCI_MANIFEST, expected_digest=expected) == expected
    else:
        with pytest.raises(ManifestError):
            push_manifest(client, "dest", "tag", raw, OCI_MANIFEST, expected_digest=expected)
    assert response.close_calls == 1
    assert client.put.call_args.kwargs["data"] is raw


def test_ack_local_mismatch_and_legacy_behavior() -> None:
    """Expected/digest-reference mismatches reject before PUT; omitted argument preserves 2xx/header."""
    client = MagicMock()
    raw = b"raw"
    for reference, expected in (("tag", address(b"bad")), (address(b"bad", "sha512"), address(raw))):
        with pytest.raises(ManifestError):
            push_manifest(client, "dest", reference, raw, OCI_MANIFEST, expected_digest=expected)
    client.put.assert_not_called()
    response = StreamResponse(b"", status=202, headers={"Docker-Content-Digest": "legacy"})
    client.put.return_value = response
    assert push_manifest(client, "dest", "tag", raw, OCI_MANIFEST) == "legacy"
    assert response.close_calls == 1


def test_ack_cross_algorithm_digest_reference() -> None:
    """A requested SHA-512 address and expected SHA-256 identity can verify the same submitted bytes."""
    client = MagicMock()
    raw = b"raw"
    response = StreamResponse(b"", status=201)
    response.request = requests.Request("PUT", "https://registry.example/v2/dest/manifests/tag", data=raw).prepare()
    client.put.return_value = response
    assert push_manifest(
        client, "dest", address(raw, "sha512"), raw, OCI_MANIFEST, expected_digest=address(raw),
    ) == address(raw)
    assert response.close_calls == 1


def test_ack_primary_failure_survives_close_failure() -> None:
    """Malformed acknowledgement remains primary if owned response cleanup also fails."""
    response = StreamResponse(b"", status=202)
    client = MagicMock()
    client.put.return_value = response
    with patch.object(response, "close", side_effect=OSError("secondary")), pytest.raises(ManifestError):
        push_manifest(client, "dest", "tag", b"raw", OCI_MANIFEST, expected_digest=address(b"raw"))


def test_ack_transport_and_sole_close_failures_propagate() -> None:
    """Transport identity is unchanged, and an otherwise successful PUT cannot swallow a close error."""
    client = MagicMock()
    error = requests.ConnectionError("synthetic transport")
    client.put.side_effect = error
    with pytest.raises(requests.ConnectionError) as caught:
        push_manifest(client, "dest", "tag", b"raw", OCI_MANIFEST, expected_digest=address(b"raw"))
    assert caught.value is error
    client.put.side_effect = None
    response = StreamResponse(b"", status=201)
    response.request = requests.Request("PUT", "https://registry.example/v2/dest/manifests/tag", data=b"raw").prepare()
    client.put.return_value = response
    cleanup = OSError("close failed")
    with patch.object(response, "close", side_effect=cleanup), pytest.raises(OSError) as caught:
        push_manifest(client, "dest", "tag", b"raw", OCI_MANIFEST, expected_digest=address(b"raw"))
    assert caught.value is cleanup


@pytest.mark.parametrize("name", ["max-archive-bytes", "max-archive-input-bytes"])
@pytest.mark.parametrize("dry", [False, True])
def test_archive_cli_limits_precede_credentials(tmp_path: Path, name: str, dry: bool) -> None:
    """Both archive budgets are independently wired to real and dry-run preflight."""
    root, objects = _graph("nested", "sha256")
    registry = ObjectRegistry(root, objects)
    source = tmp_path / "layout.gz"
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        pull_artifact(_client(), "source", "moving", source, output_format=ArtifactFormat.OCI_TAR_GZIP)
    with patch("regshape.cli.layout.RegistryClient") as client:
        result = _runner().invoke(regshape, [
            "layout", "push", "--path", str(source), "--dest", "registry.example/dest",
            "--" + name, "1", *(["--dry-run"] if dry else []),
        ])
    assert result.exit_code == 1 and name.replace("-", "_") in result.stderr
    client.assert_not_called()


@pytest.mark.parametrize("dry", [False, True])
def test_invalid_annotation_command_override_has_no_client(tmp_path: Path, dry: bool) -> None:
    """Even a valid override cannot hide invalid original metadata from command preflight."""
    source, _, _ = _local(tmp_path)
    wrapper = json.loads((source / "index.json").read_bytes())
    wrapper["manifests"][0]["annotations"] = {REF_NAME: "bad#fragment"}
    (source / "index.json").write_text(json.dumps(wrapper))
    with (
        patch("regshape.cli.layout.RegistryClient") as client,
        patch("regshape.libs.transport.client.resolve_credentials") as credentials,
    ):
        result = _runner().invoke(regshape, [
            "layout", "push", "--path", str(source), "--dest", "registry.example/dest:valid",
            *(["--dry-run"] if dry else []),
        ])
    assert result.exit_code == 1
    client.assert_not_called()
    credentials.assert_not_called()


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("tag", [None, "explicit"])
def test_docker_command_real_conversion_and_push(chunked: bool, tag: str | None) -> None:
    """Mock only the Docker daemon/socket boundaries; actual export converts and passes strict planning."""
    registry = ObjectRegistry(b"", {})
    tar_bytes = _make_single_platform_tar()
    with (
        patch("regshape.libs.docker.operations.docker_sdk") as docker,
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        docker.from_env.return_value.images.get.return_value.save.return_value = iter([tar_bytes])
        result = _runner().invoke(regshape, [
            "docker", "push", "--image", "testimage:latest",
            "--dest", "registry.example/dest" + (f":{tag}" if tag else ""),
            "--json", *(["--chunked", "--chunk-size", "13"] if chunked else []),
        ])
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.output)["manifests_pushed"] == 1
    assert registry.documents[-1][0] == (tag or "latest")
    assert all(response.close_calls == 1 for response in registry.responses)


def test_invalid_rotated_upload_location_closes_without_commit(tmp_path: Path) -> None:
    """A malformed PATCH session URL aborts explicitly rather than silently committing a stale session."""
    source, root, objects = _local(tmp_path)
    registry = ObjectRegistry(root, objects)

    def respond(method: str, url: str, **kwargs) -> StreamResponse:
        """Corrupt only the rotated upload URL after accepting the first chunk."""
        response = registry.respond(method, url, **kwargs)
        if method == "PATCH":
            response.headers["Location"] = "/not-an-upload-session"
        return response

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
        pytest.raises(BlobError, match="Location"),
    ):
        push_layout(source, _client(), "dest", chunked=True, chunk_size=13)
    assert not any(method == "PUT" for method, _ in registry.calls)
    assert all(response.close_calls == 1 for response in registry.responses)


@pytest.mark.parametrize("redirect", [301, 302, 303, 307, 308])
def test_ack_actual_requests_redirects_preserve_method_and_payload(redirect: int) -> None:
    """Actual Requests redirect handling must not turn a verified publication into a success-shaped GET."""
    raw = b'{ "schemaVersion": 2 }\n'
    responses = []
    sent = []

    def send(adapter, request: requests.PreparedRequest, **kwargs) -> StreamResponse:
        """Replace only the socket adapter; Requests still builds and follows each redirect."""
        sent.append(request)
        response = StreamResponse(b"", status=redirect if len(sent) == 1 else 201, headers=(
            {"Location": "https://cdn.example/manifest?signature=synthetic"} if len(sent) == 1 else {}
        ))
        response.request = request
        response.url = request.url
        responses.append(response)
        return response

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=send),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example", enable_middleware=False))
        if redirect in (307, 308):
            assert push_manifest(
                client, "dest", "tag", raw, OCI_MANIFEST, expected_digest=address(raw),
            ) == address(raw)
        else:
            with pytest.raises(ManifestError, match="verified payload"):
                push_manifest(client, "dest", "tag", raw, OCI_MANIFEST, expected_digest=address(raw))
    assert len(sent) == 2 and sent[0].method == "PUT" and sent[0].body == raw
    assert (sent[-1].method == "PUT" and sent[-1].body == raw) is (redirect in (307, 308))
    assert all(response.close_calls == 1 for response in responses)


@pytest.mark.parametrize("request_kind", ["missing", "get", "changed-body"])
def test_ack_requires_final_prepared_request_evidence(request_kind: str) -> None:
    """Even a matching digest header cannot acknowledge a request that omitted the verified PUT."""
    raw = b"raw"
    response = StreamResponse(b"", status=201, headers={"Docker-Content-Digest": address(raw)})
    if request_kind != "missing":
        response.request = requests.Request(
            "GET" if request_kind == "get" else "PUT",
            "https://registry.example/v2/dest/manifests/tag",
            data=raw if request_kind == "get" else b"different",
        ).prepare()
    client = MagicMock()
    client.put.return_value = response
    with pytest.raises(ManifestError, match="verified payload"):
        push_manifest(client, "dest", "tag", raw, OCI_MANIFEST, expected_digest=address(raw))
    assert response.close_calls == 1

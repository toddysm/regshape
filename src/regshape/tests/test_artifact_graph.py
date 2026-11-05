"""Graph, raw-identity, progress and resource-boundary regressions for OCI directory pull."""

import base64
import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.artifacts import (
    ArtifactError,
    ArtifactFormat,
    ArtifactLimits,
    ArtifactProgress,
    pull_artifact,
)
from regshape.libs.artifacts.graph import (
    DOCKER_INDEX,
    DOCKER_MANIFEST,
    OCI_INDEX,
    OCI_MANIFEST,
    REF_NAME,
    Edge,
    strict_json,
    verify_graph,
)
from regshape.libs.artifacts.storage import ContentStore, SafeDirectory
from regshape.libs.errors import BlobError, ManifestError
from regshape.libs.layout import validate_layout
from regshape.tests.artifact_fixtures import (
    Registry,
    address,
    document,
    edge,
    nested_registry,
    write_layout,
)


@pytest.mark.parametrize("index", [False, True])
@pytest.mark.parametrize("tag", [False, True])
def test_four_root_cases(tmp_path: Path, index: bool, tag: bool) -> None:
    """All tag/digest x index/manifest cases retain exact root/config/layer bytes."""
    config, layer = b"config", b"layer"
    manifest = document(config=edge(config), layers=[edge(layer)])
    root = document(children=[edge(manifest, OCI_MANIFEST)]) if index else manifest
    objects = {address(raw): raw for raw in (config, layer, manifest, root)}
    registry = Registry(root, objects)
    events = []
    result = pull_artifact(
        registry, "repo", "moving" if tag else address(root), tmp_path / "out",
        progress_callback=events.append,
    )
    assert result.root.digest == address(root)
    assert result.bytes_stored == sum(map(len, objects.values()))
    assert result.bytes_downloaded == result.bytes_stored
    assert result.manifests == 1 and result.indexes == int(index) and result.blobs == 2
    assert result.downloaded_objects == len(objects)
    assert result.resolved_reference == "registry.example/repo@" + address(root)
    for digest, raw in objects.items():
        assert (tmp_path / "out" / "blobs" / digest.replace(":", "/")).read_bytes() == raw
    wrapper = json.loads((tmp_path / "out" / "index.json").read_bytes())
    assert len(wrapper["manifests"]) == 1
    assert wrapper["manifests"][0]["digest"] == address(root)
    assert wrapper["manifests"][0].get("annotations") == ({REF_NAME: "moving"} if tag else None)
    assert not list(tmp_path.glob(".regshape*"))
    assert all(response.close_calls == 1 for response in registry.responses)
    assert events[0].digest is None and events[0].kind == "unknown"
    assert events[-1].phase == "stored"
    root_events = [event for event in events if event.transfer_id == events[0].transfer_id]
    assert root_events[-1].digest == address(root)
    assert len({event.transfer_id for event in events}) == len(objects)
    assert {event.role for event in events} >= {None, "config", "layer"}
    validate_layout(tmp_path / "out", recursive=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.bytes_stored = 0


def test_subject_first_nested_shared_and_moving_tag(tmp_path: Path) -> None:
    """Traverse subjects first/all platforms, fetch shared bytes once, and never rediscover the tag."""
    registry, root, objects = nested_registry()
    registry.after_get = lambda path: setattr(registry, "root", document(children=[]))
    events = []
    result = pull_artifact(registry, "repo", "moving", tmp_path / "out", progress_callback=events.append)
    assert result.indexes == 3 and result.manifests == 2 and result.blobs == 2
    assert result.reused_references == 5
    assert len(registry.calls) == len(objects)
    root_body = json.loads(root)
    assert registry.calls[1].endswith(root_body["subject"]["digest"])
    assert registry.calls[2].endswith(root_body["manifests"][0]["digest"])
    assert len([path for path in registry.calls if path.endswith("/moving")]) == 1
    assert not any("referrer" in path or "tags" in path for path in registry.calls)
    assert len([event for event in events if event.phase == "stored"]) == len(objects)
    config_digest = json.loads(objects[root_body["manifests"][1]["digest"]])["config"]["digest"]
    assert {event.role for event in events if event.digest == config_digest} == {"config"}
    validate_layout(tmp_path / "out", recursive=True)


@pytest.mark.parametrize("index_root", [True, False])
def test_nested_subject_manifests_before_referring_content(tmp_path: Path, index_root: bool) -> None:
    """Manifest and index roots copy nested subject documents before their own config/layers."""
    config = b"subject config"
    tail = document(config=edge(config))
    middle = document(children=[], subject=edge(tail, OCI_MANIFEST))
    root = document(
        children=[] if index_root else None, config=edge(b"root config"),
        subject=edge(middle, OCI_INDEX),
    )
    objects = {address(raw): raw for raw in (config, tail, middle, b"root config")}
    registry = Registry(root, objects)
    pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert [path.rsplit("/", 1)[-1] for path in registry.calls[:4]] == [
        "moving", address(middle), address(tail), address(config),
    ]
    validate_layout(tmp_path / "out", recursive=True)


def test_unaffordable_marker_fails_preflight(tmp_path: Path) -> None:
    """A marker cap that cannot fit generated metadata fails before staging or a root request."""
    registry = Registry(document(children=[]))
    with pytest.raises(ArtifactError, match="max_layout_marker_bytes"):
        pull_artifact(
            registry, "repo", "moving", tmp_path / "out",
            limits=ArtifactLimits(max_layout_marker_bytes=1),
        )
    assert not registry.calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize("root_type", [OCI_INDEX, DOCKER_INDEX, OCI_MANIFEST, DOCKER_MANIFEST])
def test_empty_documents_and_docker_types(tmp_path: Path, root_type: str) -> None:
    """Empty indexes/layer lists are valid; manifests still retain their required config."""
    config = b""
    root = document(
        children=[] if root_type in (OCI_INDEX, DOCKER_INDEX) else None,
        config=edge(config), media_type=root_type,
    )
    result = pull_artifact(Registry(root, {address(config): config}), "repo", "moving", tmp_path / "out")
    assert result.root.media_type == root_type
    validate_layout(tmp_path / "out", recursive=True)


def test_sha512_and_different_reported_root_algorithm(tmp_path: Path) -> None:
    """SHA-512 paths and two root digest algorithms are verified against the same entity."""
    config = b"sha512 config"
    root = document(config=edge(config, digest=address(config, "sha512")))
    registry = Registry(root, {address(root, "sha512"): root, address(config, "sha512"): config})
    registry.headers_by_reference[address(root, "sha512")] = {"Docker-Content-Digest": address(root)}
    result = pull_artifact(registry, "repo", address(root, "sha512"), tmp_path / "out")
    assert result.root.digest == address(root, "sha512")
    assert (tmp_path / "out" / "blobs" / "sha512" / address(config, "sha512").split(":")[1]).exists()
    validate_layout(tmp_path / "out", recursive=True)


@pytest.mark.parametrize("kind", ["blob", "manifest", "index"])
def test_embedded_objects_never_download(tmp_path: Path, kind: str) -> None:
    """Strict embedded data supplies local objects and recursively expands document content."""
    config = b"embedded config"
    embedded_config = edge(config, data=base64.b64encode(config).decode())
    if kind == "blob":
        root = document(config=embedded_config, layers=[embedded_config])
        extra_count = 1
    else:
        child = document(config=embedded_config) if kind == "manifest" else document(children=[])
        root = document(children=[edge(
            child, OCI_MANIFEST if kind == "manifest" else OCI_INDEX,
            data=base64.b64encode(child).decode(),
        )])
        extra_count = 2 if kind == "manifest" else 1
    registry = Registry(root)
    events = []
    result = pull_artifact(registry, "repo", "moving", tmp_path / "out", progress_callback=events.append)
    assert len(registry.calls) == 1
    assert result.embedded_objects == extra_count
    assert result.downloaded_objects == 1
    assert result.bytes_downloaded == len(root)
    assert len({event.transfer_id for event in events}) == extra_count + 1
    assert any(event.source == "embedded" and event.phase == "stored" for event in events)
    validate_layout(tmp_path / "out", recursive=True)


@pytest.mark.parametrize("mutation", [
    {"size": True}, {"size": "2"}, {"size": -1},
    {"digest": "sha256:../../escape"}, {"digest": "md5:" + "a" * 32},
    {"mediaType": ""}, {"data": False}, {"urls": "https://external.example/a"},
])
def test_bad_descriptor_fails_without_blob_access(tmp_path: Path, mutation: dict) -> None:
    """Strict edge fields are never coerced, used as paths or fetched on malformed input."""
    root = document(config=edge(b"{}", **mutation))
    registry = Registry(root)
    with pytest.raises(ArtifactError):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert len(registry.calls) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_data", ["%%%=", "YQ==\n", "YQ=", "YR==", "Yg==", "YQ===="])
def test_bad_embedded_data_never_falls_back_to_network(tmp_path: Path, bad_data: str) -> None:
    """Invalid/contradictory inline data fails instead of fetching a different representation."""
    root = document(config=edge(b"a", data=bad_data))
    registry = Registry(root, {address(b"a"): b"a"})
    with pytest.raises(ArtifactError):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert len(registry.calls) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mutation", [{"size": 6}, {"mediaType": "different/type"}, {"data": "%%%%"}])
def test_every_duplicate_edge_is_checked(tmp_path: Path, mutation: dict) -> None:
    """Deduplication does not hide a second size/type/inline-data contradiction."""
    config = b"shared"
    root = document(config=edge(config), layers=[edge(config, **mutation)])
    if mutation == {"size": 6}:
        root = document(config=edge(config), layers=[edge(config, size=7)])
    registry = Registry(root, {address(config): config})
    with pytest.raises(ArtifactError):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert len(registry.calls) == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("root", [
    b'{"schemaVersion":2,"schemaVersion":2,"mediaType":"' + OCI_INDEX.encode() + b'","manifests":[]}',
    b'{"schemaVersion":true,"mediaType":"' + OCI_INDEX.encode() + b'","manifests":[]}',
    b'{"schemaVersion":2,"manifests":[]}',
    b'{"schemaVersion":2,"mediaType":"unsupported","manifests":[]}',
    b'{"schemaVersion":2,"mediaType":"' + OCI_INDEX.encode() + b'","manifests":[],"layers":[]}',
    b'{"schemaVersion":2,"mediaType":"' + OCI_MANIFEST.encode() + b'","layers":[]}',
    b'{"schemaVersion":2,"mediaType":"' + OCI_INDEX.encode() + b'","manifests":[],"extra":NaN}',
    b"\xff", b"[]",
])
def test_strict_root_json_shape_failures(tmp_path: Path, root: bytes) -> None:
    """Unsupported, duplicate, non-UTF8 or contradictory roots leave no success-shaped layout."""
    with pytest.raises(ArtifactError):
        pull_artifact(Registry(root), "repo", "moving", tmp_path / "out")
    assert not list(tmp_path.iterdir())


def test_declaration_resolution_and_conflicts(tmp_path: Path) -> None:
    """A recognized header can fill a missing body type, but cannot contradict a present type."""
    root = b'{"schemaVersion":2,"manifests":[]}'
    registry = Registry(root)
    registry.root_headers = {"Content-Type": OCI_INDEX + "; charset=latin-1"}
    pull_artifact(registry, "repo", "moving", tmp_path / "good")
    assert (tmp_path / "good" / "blobs" / address(root).replace(":", "/")).read_bytes() == root
    bad = Registry(document(children=[]))
    bad.root_headers = {"Content-Type": OCI_MANIFEST}
    with pytest.raises(ArtifactError, match="Conflicting"):
        pull_artifact(bad, "repo", "moving", tmp_path / "bad")


@pytest.mark.parametrize("subject_type", [OCI_INDEX, OCI_MANIFEST, "application/unsupported"])
def test_missing_or_unsupported_subject_is_required(tmp_path: Path, subject_type: str) -> None:
    """A missing explicit subject fails full closure; unsupported types fail before fetching."""
    root = document(children=[], subject=edge(b"missing", subject_type))
    registry = Registry(root)
    with pytest.raises((ArtifactError, ManifestError)):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert not list(tmp_path.iterdir())
    assert not any("referrers" in call for call in registry.calls)


def test_external_urls_are_not_followed(tmp_path: Path) -> None:
    """External-only required data is an error, never a request to descriptor URLs."""
    root = document(config=edge(b"missing", urls=["https://external.example/payload"]))
    registry = Registry(root)
    with pytest.raises(BlobError):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert len(registry.calls) == 2
    assert "/blobs/" in registry.calls[-1]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field,bad", [
    (field.name, value) for field in dataclasses.fields(ArtifactLimits)
    for value in (0, -1, True, 1.5, "1")
])
def test_limits_reject_nonpositive_or_coerced_values(field: str, bad: object) -> None:
    """All policy fields, including reserved archive caps, reject boolean/coerced/invalid values."""
    with pytest.raises(ValueError, match=field):
        ArtifactLimits(**{field: bad})


def test_json_depth_boundary_and_string_escaping() -> None:
    """String braces and escaped quotes do not affect the predecoder depth budget."""
    raw = b'{"a":[{"text":"[[{\\\\\\""}]}'
    strict_json(raw, ArtifactLimits(max_json_depth=3), len(raw))
    with pytest.raises(ArtifactError, match="max_json_depth"):
        strict_json(raw, ArtifactLimits(max_json_depth=2), len(raw))
    with pytest.raises(ArtifactError, match="byte cap"):
        strict_json(raw, ArtifactLimits(), len(raw) - 1)


@pytest.mark.parametrize("field", ["max_objects", "max_edges", "max_depth", "max_total_bytes", "max_manifest_bytes"])
def test_graph_cap_boundary_and_plus_one(tmp_path: Path, field: str) -> None:
    """Exact graph budgets pass; exceeding each independent cap by one fails."""
    config = b"config"
    child = document(config=edge(config), layers=[edge(config)])
    root = document(children=[edge(child, OCI_MANIFEST)])
    objects = {address(raw): raw for raw in (config, child, root)}
    source = tmp_path / "layout"
    write_layout(source, root, objects)
    limits = {
        "max_objects": 3, "max_edges": 4, "max_depth": 2,
        "max_total_bytes": sum(map(len, objects.values())),
        "max_manifest_bytes": max(map(len, (root, child, (source / "index.json").read_bytes()))),
    }
    validate_layout(source, recursive=True, limits=ArtifactLimits(**{field: limits[field]}))
    with pytest.raises(ArtifactError):
        validate_layout(source, recursive=True, limits=ArtifactLimits(**{field: limits[field] - 1}))


def test_total_reserved_before_next_download(tmp_path: Path) -> None:
    """Descriptor sizes reserve the remaining total before network/body writes begin."""
    root = document(config=edge(b"x" * 1024))
    registry = Registry(root)
    with pytest.raises(ArtifactError, match="max_total_bytes"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out", limits=ArtifactLimits(max_total_bytes=len(root)))
    assert len(registry.calls) == 1


def test_cycle_and_deep_iterative_graph(tmp_path: Path) -> None:
    """Synthetic identity fixtures exercise cycles and >Python-recursion-depth graph traversal."""
    count = 1100
    digests = ["sha256:" + f"{index:064x}" for index in range(count)]
    raw = {
        digest: document(children=[{
            "mediaType": OCI_INDEX, "digest": digests[index + 1], "size": 1,
        }] if index + 1 < count else [])
        for index, digest in enumerate(digests)
    }
    root = Edge(digests[0], 1, OCI_INDEX, True)
    with SafeDirectory.open(tmp_path) as directory:
        store = ContentStore(directory)
        with patch.object(store, "verify", side_effect=lambda digest, *a, **k: raw[digest]):
            graph = verify_graph((root,), store, ArtifactLimits(max_depth=count))
            assert len(graph.objects) == count
            raw[digests[-1]] = document(children=[{
                "mediaType": OCI_INDEX, "digest": digests[0], "size": 1,
            }])
            with pytest.raises(ArtifactError, match="Cycle"):
                verify_graph((root,), store, ArtifactLimits(max_depth=count))


def test_completed_subtree_reuse_cannot_bypass_depth(tmp_path: Path) -> None:
    """A subtree first seen shallow still fails when a deeper incoming path exceeds depth."""
    leaf = document(children=[])
    subtree = document(children=[edge(leaf, OCI_INDEX)])
    deep = document(children=[edge(subtree, OCI_INDEX)])
    root = document(children=[edge(subtree, OCI_INDEX), edge(deep, OCI_INDEX)])
    objects = {address(raw): raw for raw in (leaf, subtree, deep)}
    registry = Registry(root, objects)
    with pytest.raises(ArtifactError, match="max_depth"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out", limits=ArtifactLimits(max_depth=2))


@pytest.mark.parametrize("changes", [
    {"bytes_completed": -1}, {"bytes_completed": True}, {"bytes_completed": None},
    {"total_bytes": 1, "bytes_completed": 2}, {"total_bytes": False},
    {"attempt": 0}, {"attempt": None}, {"kind": "root"}, {"phase": "completed"},
    {"source": "remote"}, {"digest": "bad"}, {"role": "manifest"}, {"transfer_id": ""},
])
def test_progress_contract_rejects_bad_values(changes: dict) -> None:
    """Public artifact observations fail explicitly before an invalid renderer event escapes."""
    values = {
        "transfer_id": "id", "kind": "unknown", "phase": "preparing", "digest": None,
        "role": None, "bytes_completed": 0, "total_bytes": None,
    }
    with pytest.raises(ValueError):
        ArtifactProgress(**(values | changes))


def test_recursive_cli_and_shallow_limit_rejection(tmp_path: Path) -> None:
    """CLI opts into strict graph verification while retaining shallow invocation compatibility."""
    root = document(children=[])
    path = tmp_path / "layout"
    write_layout(path, root, {})
    runner = CliRunner()
    success = runner.invoke(regshape, ["layout", "validate", "--path", str(path), "--recursive"])
    assert success.exit_code == 0, success.output
    usage = runner.invoke(regshape, ["layout", "validate", "--path", str(path), "--max-depth", "2"])
    assert usage.exit_code == 2 and "--recursive" in usage.output
    failed = runner.invoke(regshape, [
        "layout", "validate", "--path", str(path), "--recursive", "--max-total-bytes", "1",
    ])
    assert failed.exit_code == 1 and "valid." not in failed.output
    with pytest.raises(ValueError, match="recursive"):
        validate_layout(path, limits=ArtifactLimits())


@pytest.mark.parametrize("reference", ["bad?query", "bad#fragment", "bad/tag", "bad%2f", "", "a" * 129])
def test_source_reference_preflight(tmp_path: Path, reference: str) -> None:
    """Invalid source tags fail before creating staging or accessing the registry."""
    registry = Registry(document(children=[]))
    with pytest.raises(ValueError):
        pull_artifact(registry, "repo", reference, tmp_path / "out")
    assert not registry.calls and not list(tmp_path.iterdir())


def test_archive_is_explicitly_out_of_scope(tmp_path: Path) -> None:
    """Reserved archive format fails before effects rather than emitting a mislabeled directory."""
    registry = Registry(document(children=[]))
    with pytest.raises(ArtifactError, match="Only OCI"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out", output_format=ArtifactFormat.OCI_TAR_GZIP)
    assert not registry.calls and not list(tmp_path.iterdir())

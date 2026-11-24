"""Offline SDK inspection contracts, exact view shapes, safety and independent caps.

Synthetic local bytes model opaque content without a registry, daemon, payload
verification or extraction. Every explicit budget is tested at its inclusive
boundary and at the first unit over it.
"""

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST, REF_NAME
from regshape.libs.artifacts.storage import SafeDirectory, require_readonly_filesystem
from regshape.libs.errors import ArtifactError
from regshape.libs.layout import (
    IndexNode,
    InspectionLimits,
    LayoutInspectionError,
    ManifestNode,
    inspect_layout,
    inspection,
)
from regshape.tests.artifact_fixtures import address, edge


@pytest.fixture(autouse=True)
def supported_inspection(request: pytest.FixtureRequest) -> None:
    """Skip native reads on unsupported systems, but always exercise platform/argument guards."""
    if (
        "unsupported_platform" in request.node.name
        or "invalid_arguments" in request.node.name
    ):
        return
    if sys.platform not in ("linux", "darwin"):
        pytest.skip("Read-only inspection intentionally supports Linux/macOS only")
    try:
        require_readonly_filesystem()
    except ArtifactError:
        pytest.skip("Safe read-only primitives are unavailable")


def _raw(value: dict) -> bytes:
    """Serialize fixture bytes identically to the pinned tiny design example."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def _index(manifests: list[dict], **fields: object) -> bytes:
    """Make a source-ordered index with deliberate optional/negative overrides."""
    return _raw(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests, **fields}
    )


def _manifest(
    config: bytes = b"{}", layers: list[dict] | None = None, **fields: object
) -> bytes:
    """Make the approved one-content tiny fixture unless overrides specify another shape."""
    return _raw(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "artifactType": "application/vnd.example.payload",
            "config": edge(config, "application/vnd.oci.empty.v1+json"),
            "layers": [edge(config)] if layers is None else layers,
            **fields,
        }
    )


def _layout(tmp_path: Path, index: bytes, objects: dict[str, bytes]) -> Path:
    """Write only synthetic source fixtures, independent of production safe-open helpers."""
    path = tmp_path.resolve() / "layout"
    path.mkdir()
    (path / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    (path / "index.json").write_bytes(index)
    for digest, raw in objects.items():
        algorithm, encoded = digest.split(":")
        target = path / "blobs" / algorithm / encoded
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return path


def _tiny(tmp_path: Path, **index_fields: object) -> tuple[Path, bytes, bytes]:
    """Return the design's byte-exact manifest/opaque shared-object layout."""
    payload = b"{}"
    manifest = _manifest(payload)
    path = _layout(
        tmp_path,
        _index([edge(manifest, OCI_MANIFEST)], **index_fields),
        {
            address(manifest): manifest,
            address(payload): payload,
        },
    )
    return path, manifest, payload


def _validate(value: dict) -> None:
    """Validate the exact expanded branch of the approved version-1 output schema."""
    schema_path = Path(__file__).parents[3] / "specs/layout/layout-inspect.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["oneOf"] = [{"$ref": "#/$defs/expanded"}]
    Draft202012Validator(schema).validate(value)


def _nodes(root):
    """Yield the visible occurrence preorder, including repeated targets."""
    stack = [root]
    while stack:
        item = stack.pop()
        yield item
        stack.extend(reversed(getattr(item, "children", ()) or ()))


def test_tiny_exact_serialization_and_immutable_independent_copy(
    tmp_path: Path,
) -> None:
    """Emit the design example, not hidden provenance/counters or decoded opaque fields."""
    path, manifest, payload = _tiny(tmp_path)
    result = inspect_layout(path)
    document_digest, payload_digest = address(manifest), address(payload)
    assert len(manifest) == 420
    assert (
        document_digest
        == "sha256:f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f"
    )
    expected = {
        "kind": "layout",
        "location": str(path),
        "imageLayoutVersion": "1.0.0",
        "children": [
            {
                "kind": "index",
                "location": "index.json",
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": [{"digest": document_digest}],
                "children": [
                    {
                        "kind": "manifest",
                        "digest": document_digest,
                        "location": "blobs/" + document_digest.replace(":", "/"),
                        "schemaVersion": 2,
                        "mediaType": OCI_MANIFEST,
                        "artifactType": "application/vnd.example.payload",
                        "config": {"digest": payload_digest},
                        "layers": [{"digest": payload_digest}],
                        "children": [
                            {
                                "kind": "config",
                                "digest": payload_digest,
                                "location": "blobs/" + payload_digest.replace(":", "/"),
                            },
                            {
                                "kind": "content",
                                "position": 0,
                                "digest": payload_digest,
                                "location": "blobs/" + payload_digest.replace(":", "/"),
                            },
                        ],
                    }
                ],
            }
        ],
    }
    assert result.complete and result.diagnostics == ()
    assert result.to_dict() == expected
    assert result.to_json() == json.dumps(expected, indent=2, ensure_ascii=True) + "\n"
    _validate(result.to_dict())
    result.to_dict()["children"].clear()
    assert result.to_dict() == expected
    with pytest.raises(FrozenInstanceError):
        result.root.location = "changed"


def test_nested_multi_root_repeat_provenance_and_shared_source_fields(
    tmp_path: Path,
) -> None:
    """Retain every index occurrence and its incoming metadata without multiplying storage facts."""
    payload = b"{}"
    manifest = _manifest(annotations={"b": "", "a": "value"})
    nested = _index([edge(manifest, OCI_MANIFEST)], annotations={})
    index = _index(
        [
            edge(
                nested,
                OCI_INDEX,
                annotations={REF_NAME: "first"},
                platform={
                    "os": "linux",
                    "architecture": "amd64",
                    "extension": {"null": None},
                },
            ),
            edge(
                nested,
                OCI_INDEX,
                annotations={REF_NAME: "second"},
                platform={"os": "linux", "architecture": "arm64"},
            ),
            edge(manifest, OCI_MANIFEST),
        ],
        subject=edge(b"external", OCI_MANIFEST),
        annotations={},
    )
    path = _layout(
        tmp_path,
        index,
        {address(value): value for value in (payload, manifest, nested)},
    )
    result = inspect_layout(path)
    entry = result.root.children[0]
    first, second, standalone = entry.children
    assert result.complete
    assert isinstance(first, IndexNode) and isinstance(second, IndexNode)
    assert first.incoming.annotations[REF_NAME] == "first"
    assert second.incoming.annotations[REF_NAME] == "second"
    assert str(first.incoming.source) == "index.json#/manifests/0"
    assert first.annotations is second.annotations
    assert first.manifests[0] is second.manifests[0]
    assert first.children[0].annotations is second.children[0].annotations
    assert standalone.subject is None
    assert entry.subject.digest == address(b"external")
    assert len(result._storage.sizes) == 3
    assert result._storage.object_references == 6
    assert result._storage.subject_references == 1
    assert len(list(_nodes(result.root))) == 13
    _validate(result.to_dict())
    with pytest.raises(TypeError):
        first.incoming.platform["os"] = "changed"
    with pytest.raises(TypeError):
        first.incoming.platform["extension"]["null"] = "changed"


def test_missing_shared_payload_has_exact_occurrence_errors(tmp_path: Path) -> None:
    """One missing address produces both source-local errors, never empty successful metadata."""
    path, manifest, payload = _tiny(tmp_path)
    (path / "blobs" / address(payload).replace(":", "/")).unlink()
    result = inspect_layout(path)
    assert not result.complete
    children = result.to_dict()["children"][0]["children"][0]["children"]
    for position, source in enumerate(("/config", "/layers/0")):
        assert children[position]["errors"] == [
            {
                "code": "MISSING_OBJECT",
                "message": "Referenced file is missing",
                "source": "blobs/" + address(manifest).replace(":", "/") + "#" + source,
            }
        ]
    assert [str(item.source) for item in result.diagnostics] == [
        "blobs/" + address(manifest).replace(":", "/") + "#" + suffix
        for suffix in ("/config", "/layers/0")
    ]
    _validate(result.to_dict())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schemaVersion":2,"schemaVersion":2,"manifests":[]}',
        b'{"schemaVersion":2,"manifests":[],"ignored":NaN}',
        b'{"schemaVersion":2,"manifests":[],"ignored":1e999}',
        b'{"schemaVersion":2,"manifests":[],"ignored":"\\ud800"}',
        b'{"schemaVersion":2,"manifests":[],"ignored":"\xff"}',
        b'{"schemaVersion":true,"manifests":[]}',
        b'{"schemaVersion":2,"manifests":[],"annotations":null}',
        b'{"schemaVersion":2,"manifests":[],"mediaType":null}',
    ],
)
def test_invalid_root_strict_metadata_has_no_result(tmp_path: Path, raw: bytes) -> None:
    """Malformed intrinsic/root input raises typed no-tree diagnostics, never coercion."""
    path = _layout(tmp_path, raw, {})
    with pytest.raises(LayoutInspectionError) as caught:
        inspect_layout(path)
    assert caught.value.diagnostics[0].code == "INVALID_LAYOUT"
    assert caught.value.diagnostics[-1].code == "MALFORMED_DOCUMENT"


def test_invalid_marker_has_specific_no_tree_diagnostic(tmp_path: Path) -> None:
    """A missing marker names that required file rather than returning an arbitrary tree."""
    path = _layout(tmp_path, _index([]), {})
    (path / "oci-layout").unlink()
    with pytest.raises(LayoutInspectionError) as caught:
        inspect_layout(path)
    assert [(item.code, item.message) for item in caught.value.diagnostics] == [
        ("INVALID_LAYOUT", "Required layout marker oci-layout is missing"),
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest", "sha256:../../outside"),
        ("digest", "sha256:" + "a" * 64 + "\n"),
        ("size", True),
        ("size", None),
        ("annotations", None),
        ("platform", {"os": "linux"}),
        ("platform", {"os": "linux", "architecture": "amd64", "variant": None}),
        ("platform", {"os": "linux", "architecture": "amd64", "os.features": [None]}),
        ("data", None),
        ("urls", [None]),
    ],
)
def test_invalid_descriptor_slot_keeps_position_and_trustworthy_siblings(
    tmp_path: Path, field: str, value: object
) -> None:
    """Validate slots independently; malformed data never drops/renumbers the next descriptor."""
    manifest = _manifest(layers=[])
    bad = edge(manifest, OCI_MANIFEST, **{field: value})
    path = _layout(
        tmp_path,
        _index([bad, edge(manifest, OCI_MANIFEST)]),
        {
            address(manifest): manifest,
            address(b"{}"): b"{}",
        },
    )
    result = inspect_layout(path)
    entry = result.root.children[0]
    assert not result.complete
    assert entry.manifests[0].errors[0].code == "INVALID_DESCRIPTOR"
    assert str(entry.manifests[1].source) == "index.json#/manifests/1"
    assert entry.children[-1].source_position == 1
    assert isinstance(entry.children[-1], ManifestNode)
    _validate(result.to_dict())


def test_present_empty_and_absent_fields_are_distinct(tmp_path: Path) -> None:
    """Preserve allowed empty strings/maps/arrays, while omitting absent optional keys."""
    raw = _raw({"schemaVersion": 2, "manifests": [], "annotations": {"empty": ""}})
    path = _layout(tmp_path, raw, {})
    result = inspect_layout(path)
    entry = result.to_dict()["children"][0]
    assert entry == {
        "kind": "index",
        "location": "index.json",
        "schemaVersion": 2,
        "annotations": {"empty": ""},
        "manifests": [],
        "children": [],
    }
    assert result.complete
    _validate(result.to_dict())


def test_size_type_and_artifact_conflicts_preserve_intrinsic_and_incoming_fields(
    tmp_path: Path,
) -> None:
    """A conflicting descriptor never overwrites cached target fields for later occurrences."""
    raw = _manifest(layers=[])
    path = _layout(
        tmp_path,
        _index(
            [
                edge(
                    raw,
                    OCI_INDEX,
                    size=len(raw) + 1,
                    artifactType="application/vnd.conflict",
                ),
                edge(raw, OCI_MANIFEST),
            ]
        ),
        {address(raw): raw, address(b"{}"): b"{}"},
    )
    result = inspect_layout(path)
    bad, good = result.root.children[0].children
    assert bad.kind == good.kind == "manifest"
    assert bad.media_type == good.media_type == OCI_MANIFEST
    assert len(bad.errors) == 3 and good.errors == ()
    assert {item.code for item in bad.errors} == {"CONFLICTING_DESCRIPTOR"}
    _validate(result.to_dict())


def test_unsupported_type_is_not_opened_and_malformed_document_keeps_identity(
    tmp_path: Path,
) -> None:
    """Unknown required types are not guessed; malformed supported metadata has no trusted fields."""
    malformed = b'{"schemaVersion":2,"config":null}'
    unknown = b"opaque unknown metadata"
    path = _layout(
        tmp_path,
        _index(
            [edge(unknown, "application/vnd.unknown"), edge(malformed, OCI_MANIFEST)]
        ),
        {
            address(unknown): unknown,
            address(malformed): malformed,
        },
    )
    original = SafeDirectory.read_file

    def read(directory, name):
        """Reject any attempt to parse the declared unsupported object."""
        assert name != address(unknown).split(":")[1]
        return original(directory, name)

    with patch.object(SafeDirectory, "read_file", read):
        result = inspect_layout(path)
    first, second = result.root.children[0].children
    assert first.kind == "unknown" and first.errors[0].code == "UNSUPPORTED_TYPE"
    assert second.kind == "manifest" and second.schema_version is None
    assert second.errors[0].code == "MALFORMED_DOCUMENT"
    _validate(result.to_dict())


def test_containment_cycle_is_not_sibling_reuse_or_subject_following(
    tmp_path: Path,
) -> None:
    """Ancestor address cycles terminate one branch while repeated siblings still expand."""
    digest = "sha256:" + "a" * 64
    raw = _index(
        [{"mediaType": OCI_INDEX, "digest": digest, "size": 0}],
        subject=edge(b"external", OCI_INDEX),
    )
    parsed = json.loads(raw)
    parsed["manifests"][0]["size"] = len(raw)
    raw = _raw(parsed)
    parsed["manifests"][0]["size"] = len(raw)
    raw = _raw(parsed)
    path = _layout(
        tmp_path,
        _index([{"mediaType": OCI_INDEX, "digest": digest, "size": len(raw)}] * 2),
        {digest: raw},
    )
    result = inspect_layout(path)
    first, second = result.root.children[0].children
    assert first.children[0].errors[-1].code == "CYCLE"
    assert second.children[0].errors[-1].code == "CYCLE"
    assert first.subject.digest == address(b"external")
    assert len(result._storage.sizes) == 1
    _validate(result.to_dict())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_sha512_prefix_uses_unique_reachable_full_metadata_candidates(
    tmp_path: Path, count: int
) -> None:
    """A prefix with zero/two candidates stays unresolved; exactly one uses its actual suffix."""
    prefix = "a" * 64
    candidates = ["sha512:" + prefix + suffix * 64 for suffix in ("b", "c")][:count]
    tagged = (
        _index(
            [], subject={"mediaType": OCI_MANIFEST, "digest": candidates[0], "size": 0}
        )
        if candidates
        else _index([])
    )
    subjects = [
        {"mediaType": OCI_MANIFEST, "digest": item, "size": 0} for item in candidates
    ]
    entry = _index(
        [edge(tagged, OCI_INDEX, annotations={REF_NAME: "sha512-" + prefix})],
        **({"subject": subjects[-1]} if subjects else {}),
    )
    path = _layout(tmp_path, entry, {address(tagged): tagged})
    result = inspect_layout(path)
    association = result.root.children[0].children[0].referrer_list
    assert association.status == ("resolved" if count == 1 else "unresolved")
    assert association.digest == (candidates[0] if count == 1 else None)
    assert result.complete
    assert "association" not in result.to_json()
    assert len(result._storage.sizes) == 1


@pytest.mark.parametrize("slot", ["manifests", "config", "layers", "subject"])
@pytest.mark.parametrize("trusted_candidate", [False, True])
def test_sha512_candidates_exclude_intrinsically_malformed_documents(
    tmp_path: Path, slot: str, trusted_candidate: bool
) -> None:
    """Rejected metadata cannot resolve a prefix or make valid evidence ambiguous."""
    prefix = "a" * 64
    trusted_digest = "sha512:" + prefix + "b" * 64
    untrusted = {
        "mediaType": OCI_MANIFEST,
        "digest": "sha512:" + prefix + "c" * 64,
        "size": 0,
    }
    parsed = json.loads(_index([]) if slot == "manifests" else _manifest(layers=[]))
    parsed["schemaVersion"] = 1
    parsed[slot] = [untrusted] if slot in ("manifests", "layers") else untrusted
    malformed = _raw(parsed)
    tagged = _index([])
    entry = _index(
        [
            edge(tagged, OCI_INDEX, annotations={REF_NAME: "sha512-" + prefix}),
            edge(malformed, OCI_INDEX if slot == "manifests" else OCI_MANIFEST),
        ],
        **(
            {
                "subject": {
                    "mediaType": OCI_MANIFEST,
                    "digest": trusted_digest,
                    "size": 0,
                }
            }
            if trusted_candidate
            else {}
        ),
    )
    path = _layout(
        tmp_path, entry, {address(value): value for value in (tagged, malformed)}
    )
    result = inspect_layout(path)
    list_node, malformed_node = result.root.children[0].children
    evidence = list_node.referrer_list
    assert evidence.status == ("resolved" if trusted_candidate else "unresolved")
    assert evidence.digest == (trusted_digest if trusted_candidate else None)
    assert not result.complete
    assert malformed_node.errors[0].code == "MALFORMED_DOCUMENT"
    assert len(result._storage.sizes) == 2
    _validate(result.to_dict())


@pytest.mark.parametrize("slot", ["subject", "config"])
def test_sha512_candidates_exclude_list_valued_scalar_slots(
    tmp_path: Path, slot: str
) -> None:
    """Invalid scalar-slot arrays cannot supply evidence absent from descriptor views."""
    prefix = "a" * 64
    descriptor = {
        "mediaType": OCI_MANIFEST,
        "digest": "sha512:" + prefix + "b" * 64,
        "size": 0,
    }
    tagged = _index([])
    parsed = json.loads(_manifest(layers=[]))
    parsed[slot] = [descriptor]
    malformed = _raw(parsed)
    path = _layout(
        tmp_path,
        _index(
            [
                edge(tagged, OCI_INDEX, annotations={REF_NAME: "sha512-" + prefix}),
                edge(malformed, OCI_MANIFEST),
            ]
        ),
        {address(value): value for value in (tagged, malformed)},
    )
    result = inspect_layout(path)
    list_node, member = result.root.children[0].children
    assert list_node.referrer_list.status == "unresolved"
    assert getattr(member, slot).digest is None
    assert getattr(member, slot).errors[0].code == "INVALID_DESCRIPTOR"
    assert not result.complete
    _validate(result.to_dict())


def test_repeated_sha512_tags_use_linear_candidate_prefix_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated cached indexes must not multiply full-candidate prefix scans."""
    count = 100
    prefix_checks = 0
    original = inspection._valid_digest

    class CountedDigest(str):
        """Instrument candidate-prefix comparisons without changing digest values."""

        def startswith(self, prefix, *args) -> bool:
            """Count each prefix comparison performed by inspection."""
            nonlocal prefix_checks
            prefix_checks += 1
            return super().startswith(prefix, *args)

    def counted_digest(value: object) -> str | None:
        """Wrap validated SHA-512 candidates while leaving other metadata untouched."""
        digest = original(value)
        if digest is not None and digest.startswith("sha512:"):
            return CountedDigest(digest)
        return digest

    documents = [
        _index(
            [],
            subject={
                "mediaType": OCI_MANIFEST,
                "digest": "sha512:" + f"{position:064x}" + "b" * 64,
                "size": 0,
            },
        )
        for position in range(count)
    ]
    path = _layout(
        tmp_path,
        _index(
            [
                edge(
                    documents[0],
                    OCI_INDEX,
                    annotations={REF_NAME: "sha512-" + "0" * 64},
                )
            ]
            * count
            + [edge(value, OCI_INDEX) for value in documents[1:]]
        ),
        {address(value): value for value in documents},
    )
    monkeypatch.setattr(inspection, "_valid_digest", counted_digest)
    result = inspect_layout(path)
    assert result.complete
    for node in result.root.children[0].children[:count]:
        assert node.referrer_list.digest == "sha512:" + "0" * 64 + "b" * 64
    assert prefix_checks <= count * 2


@pytest.mark.parametrize("subject", [None, "a", "b"])
def test_tag_association_is_occurrence_local_and_never_invents_member_subject(
    tmp_path: Path, subject: str | None
) -> None:
    """Repeat one list with distinct declarations, retaining actual member subject direction."""
    fields = (
        {
            "subject": {
                "mediaType": OCI_MANIFEST,
                "digest": "sha256:" + subject * 64,
                "size": 0,
            }
        }
        if subject
        else {}
    )
    manifest = _manifest(layers=[], **fields)
    tagged = _index([edge(manifest, OCI_MANIFEST)])
    path = _layout(
        tmp_path,
        _index(
            [
                edge(tagged, OCI_INDEX, annotations={REF_NAME: "sha256-" + "a" * 64}),
                edge(tagged, OCI_INDEX, annotations={REF_NAME: "ordinary"}),
            ]
        ),
        {address(value): value for value in (tagged, manifest, b"{}")},
    )
    result = inspect_layout(path)
    first, second = result.root.children[0].children
    assert first.referrer_list.digest == "sha256:" + "a" * 64
    assert (
        str(first.referrer_list.source)
        == "index.json#/manifests/0/annotations/" + REF_NAME
    )
    assert second.referrer_list is None
    member = first.children[0]
    if subject is None:
        assert member.subject is None
    else:
        assert member.subject.digest == "sha256:" + subject * 64
    assert result.complete == (subject != "b")
    if subject == "b":
        assert member.subject.errors[0].code == "CONFLICTING_DESCRIPTOR"
        assert second.children[0].subject.errors == ()
    _validate(result.to_dict())


def test_readonly_bounded_cached_io_and_no_payload_hashing_network_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated targets share stat/parse observations and never open payload/unlisted files."""
    path, manifest, payload = _tiny(tmp_path)
    (path / "index.json").write_bytes(_index([edge(manifest, OCI_MANIFEST)] * 5))
    (path / "unlisted").write_bytes(b"not inspected")
    opened: list[str] = []
    payload_stats: list[str] = []
    read_file = SafeDirectory.read_file
    real_stat = os.stat
    manifest_name = address(manifest).split(":")[1]
    payload_name = address(payload).split(":")[1]

    def read(directory, name):
        """Permit only marker/entry/contained metadata opens."""
        assert name in ("oci-layout", "index.json", manifest_name)
        opened.append(name)
        return read_file(directory, name)

    def observe(name, *args, **kwargs):
        """Count opaque-target no-follow stats, including repeated references."""
        if name == payload_name:
            assert kwargs.get("follow_symlinks") is False
            payload_stats.append(name)
        return real_stat(name, *args, **kwargs)

    with (
        patch.object(SafeDirectory, "read_file", read),
        patch.object(os, "stat", observe),
    ):
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {observe})
        monkeypatch.setattr(
            os, "supports_follow_symlinks", os.supports_follow_symlinks | {observe}
        )
        with (
            patch("hashlib.new", side_effect=AssertionError("payload hashing")),
            patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("network"),
            ),
            patch(
                "regshape.libs.auth.credentials.resolve_credentials",
                side_effect=AssertionError("credentials"),
            ),
        ):
            result = inspect_layout(path)
    assert result.complete
    assert opened == ["oci-layout", "index.json", address(manifest).split(":")[1]]
    assert len(payload_stats) == 1
    assert len(result._storage.sizes) == 2
    assert result._storage.object_references == 7
    assert sum(result._storage.sizes.values()) == 422


@pytest.mark.parametrize("target", ["root", "marker", "entry", "payload", "metadata"])
def test_symlinks_fail_closed_without_following_targets(
    tmp_path: Path, target: str
) -> None:
    """No root/descendant link is read, including a linked otherwise-valid regular file."""
    path, manifest, payload = _tiny(tmp_path)
    if target == "root":
        link = path.parent / "link"
        link.symlink_to(path, target_is_directory=True)
        with pytest.raises(LayoutInspectionError):
            inspect_layout(link)
        return
    name = {
        "marker": "oci-layout",
        "entry": "index.json",
        "payload": "blobs/" + address(payload).replace(":", "/"),
        "metadata": "blobs/" + address(manifest).replace(":", "/"),
    }[target]
    file = path / name
    other = path.parent / "linked"
    file.rename(other)
    file.symlink_to(other)
    if target in ("marker", "entry"):
        with pytest.raises(LayoutInspectionError):
            inspect_layout(path)
    else:
        result = inspect_layout(path)
        assert not result.complete
        assert result.diagnostics[0].code == "UNREADABLE_OBJECT"


def test_nonregular_input_does_not_block_on_fifo(tmp_path: Path) -> None:
    """Opaque FIFO is rejected by no-follow stat without opening or waiting for a writer."""
    path, _, payload = _tiny(tmp_path)
    file = path / "blobs" / address(payload).replace(":", "/")
    file.unlink()
    os.mkfifo(file)
    result = inspect_layout(path)
    assert not result.complete
    assert all(item.code == "UNREADABLE_OBJECT" for item in result.diagnostics)


def test_changed_during_metadata_read_is_not_reused(tmp_path: Path) -> None:
    """Changing an open document creates explicit unstable-metadata failures on both occurrences."""
    path, manifest, _ = _tiny(tmp_path)
    (path / "index.json").write_bytes(_index([edge(manifest, OCI_MANIFEST)] * 2))
    read_file = SafeDirectory.read_file
    target = path / "blobs" / address(manifest).replace(":", "/")

    class ChangedStream:
        """Borrow a real safe stream and mutate source metadata after its bounded read."""

        def __init__(self, context):
            """Retain the real file close context for deterministic cleanup."""
            self.context = context

        def __enter__(self):
            """Return an instrumented read stream without leaking its real descriptor."""
            self.stream = self.context.__enter__()
            return self

        def fileno(self):
            """Preserve real fstat identity observation."""
            return self.stream.fileno()

        def read(self, size):
            """Modify on the first actual read, not by replacing production stat results."""
            data = self.stream.read(size)
            if data:
                target.write_bytes(manifest + b" ")
            return data

        def __exit__(self, *args):
            """Close through the existing production ownership contract."""
            return self.context.__exit__(*args)

    def read(directory, name):
        """Instrument only the contained manifest, leaving marker/index stable."""
        context = read_file(directory, name)
        return (
            ChangedStream(context)
            if name == address(manifest).split(":")[1]
            else context
        )

    with patch.object(SafeDirectory, "read_file", read):
        result = inspect_layout(path)
    assert [item.code for item in result.diagnostics] == ["CHANGED_DURING_READ"] * 2
    assert all(item.schema_version is None for item in result.root.children[0].children)


@pytest.mark.parametrize(
    "budget,cap",
    [
        ("max_layout_marker_bytes", 28),
        ("max_document_bytes", 420),
        ("max_total_metadata_bytes", 28 + 240 + 420),
        ("max_objects", 2),
        ("max_descriptors", 3),
        ("max_depth", 3),
        ("max_occurrences", 5),
    ],
)
def test_independent_inclusive_limits_and_first_unit_over(
    tmp_path: Path, budget: str, cap: int
) -> None:
    """The exact fixture fits at cap; cap-minus-one fails at a named source boundary."""
    path, _, _ = _tiny(tmp_path)
    if budget == "max_layout_marker_bytes":
        cap = (path / "oci-layout").stat().st_size
    if budget == "max_total_metadata_bytes":
        cap = (
            sum(
                file.stat().st_size
                for file in (path / "oci-layout", path / "index.json")
            )
            + 420
        )
    policy = replace(InspectionLimits(), **{budget: cap})
    assert inspect_layout(path, limits=policy).complete
    policy = replace(policy, **{budget: cap - 1})
    if budget in ("max_layout_marker_bytes",):
        with pytest.raises(LayoutInspectionError) as caught:
            inspect_layout(path, limits=policy)
        assert caught.value.diagnostics[-1].code == "LIMIT_REACHED"
        assert budget in caught.value.diagnostics[-1].message
    else:
        result = inspect_layout(path, limits=policy)
        assert not result.complete
        assert result.diagnostics[-1].code == "LIMIT_REACHED"
        assert budget in result.diagnostics[-1].message
        _validate(result.to_dict())


def test_json_depth_counts_ignored_fields_and_marker_and_entry_limits(
    tmp_path: Path,
) -> None:
    """Container depth includes ignored metadata; oversized entry/marker returns no tree."""
    path = _layout(tmp_path, _index([], ignored={"nested": []}), {})
    assert inspect_layout(path, limits=InspectionLimits(max_json_depth=3)).complete
    for limits in (
        InspectionLimits(max_json_depth=2),
        InspectionLimits(max_document_bytes=1),
        InspectionLimits(max_occurrences=1),
    ):
        with pytest.raises(LayoutInspectionError) as caught:
            inspect_layout(path, limits=limits)
        assert caught.value.diagnostics[-1].code == "LIMIT_REACHED"


def test_diagnostic_limit_reserves_final_explicit_marker(tmp_path: Path) -> None:
    """After one normal error the second available slot marks stopped diagnostics, not silence."""
    path, _, payload = _tiny(tmp_path)
    (path / "blobs" / address(payload).replace(":", "/")).unlink()
    result = inspect_layout(path, limits=InspectionLimits(max_diagnostics=3))
    assert [item.code for item in result.diagnostics] == ["MISSING_OBJECT"] * 2
    limited = inspect_layout(path, limits=InspectionLimits(max_diagnostics=2))
    assert [item.code for item in limited.diagnostics] == [
        "MISSING_OBJECT",
        "LIMIT_REACHED",
    ]
    assert "max_diagnostics cap 2" in limited.diagnostics[-1].message
    _validate(limited.to_dict())
    one = inspect_layout(path, limits=InspectionLimits(max_diagnostics=1))
    assert [item.code for item in one.diagnostics] == ["LIMIT_REACHED"]
    _validate(one.to_dict())


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_root_failure_diagnostic_cap_reserves_limit_marker(
    tmp_path: Path, cap: int
) -> None:
    """No-tree failures obey the same inclusive cap and final-slot reservation."""
    path = _layout(tmp_path, b"invalid JSON", {})
    with pytest.raises(LayoutInspectionError) as caught:
        inspect_layout(path, limits=InspectionLimits(max_diagnostics=cap))
    diagnostics = caught.value.diagnostics
    assert len(diagnostics) <= cap
    expected = (
        ["INVALID_LAYOUT", "MALFORMED_DOCUMENT"]
        if cap == 3
        else ["INVALID_LAYOUT", "LIMIT_REACHED"]
        if cap == 2
        else ["LIMIT_REACHED"]
    )
    assert [item.code for item in diagnostics] == expected
    assert all(item.source.document == "index.json" for item in diagnostics)
    if cap < 3:
        assert f"max_diagnostics cap {cap}" in diagnostics[-1].message


def test_output_byte_budget_includes_escaping_indentation_lf_and_preflights_copy(
    tmp_path: Path,
) -> None:
    """Both APIs reject one byte below exact encoded output before allocating a display copy."""
    path, _, _ = _tiny(tmp_path, annotations={"controls": "\\\n\t雪" * 1000})
    result = inspect_layout(path)
    encoded = result.to_json()
    cap = len(encoded.encode("ascii"))
    exact = replace(result, limits=replace(result.limits, max_output_bytes=cap))
    assert exact.to_json() == encoded
    assert exact.to_dict() == json.loads(encoded)
    limited = replace(result, limits=replace(result.limits, max_output_bytes=cap - 1))
    with (
        patch(
            "regshape.libs.layout.inspection_models._copy_slot",
            side_effect=AssertionError("copy before preflight"),
        ),
        pytest.raises(LayoutInspectionError) as caught,
    ):
        limited.to_dict()
    assert caught.value.diagnostics[0].code == "OUTPUT_LIMIT"
    with pytest.raises(LayoutInspectionError):
        limited.to_json()


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, None, "1"])
def test_invalid_arguments_rejected_before_io(value: object) -> None:
    """Reject every invalid budget type/range before opening any local object."""
    with patch.object(os, "open", side_effect=AssertionError("IO before validation")):
        with pytest.raises(ValueError):
            InspectionLimits(max_objects=value)
        with pytest.raises(ValueError):
            inspect_layout(123)
        with pytest.raises(ValueError):
            inspect_layout("path", limits={})


def test_unsupported_platform_rejected_before_io(tmp_path: Path) -> None:
    """Windows/missing read primitives fail with a typed platform error before any open."""
    with (
        patch.object(inspection.sys, "platform", "win32"),
        patch.object(os, "open") as opened,
    ):
        for limits, code in (
            (InspectionLimits(), "UNSUPPORTED_PLATFORM"),
            (InspectionLimits(max_diagnostics=1), "LIMIT_REACHED"),
        ):
            with pytest.raises(LayoutInspectionError) as caught:
                inspect_layout(tmp_path, limits=limits)
            assert caught.value.diagnostics[0].code == code
            assert len(caught.value.diagnostics) == 1
        opened.assert_not_called()


def test_readonly_does_not_require_publication_primitives_or_private_permissions(
    tmp_path: Path,
) -> None:
    """Read-capable inspection succeeds without mkdir/rename/unlink support or 0700 directories."""
    path, _, _ = _tiny(tmp_path)
    path.chmod(0o755)
    with patch.object(os, "supports_dir_fd", {os.open, os.stat}):
        assert inspect_layout(path).complete


def test_fresh_process_sdk_import_does_not_import_click_or_cli() -> None:
    """Public inspection symbols load directly without importing terminal/framework modules."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from regshape.libs.layout import inspect_layout, InspectionLimits; "
                "assert 'click' not in sys.modules; "
                "assert not any(k.startswith('regshape.cli') for k in sys.modules)"
            ),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("shape", ["wide", "reused-index"])
def test_exact_stress_shape_counts_and_one_read_per_distinct_metadata(
    tmp_path: Path, shape: str
) -> None:
    """Exercise the approved stress counts without treating them as performance certification."""
    count = 1024 if shape == "wide" else 64
    manifests = [
        _manifest(annotations={"fixture": str(position)}) for position in range(count)
    ]
    objects = {address(value): value for value in [b"{}", *manifests]}
    if shape == "wide":
        index = _index([edge(value, OCI_MANIFEST) for value in manifests])
    else:
        nested = _index([edge(value, OCI_MANIFEST) for value in manifests])
        objects[address(nested)] = nested
        index = _index([edge(nested, OCI_INDEX)] * 100)
    path = _layout(tmp_path, index, objects)
    opened: list[str] = []
    real_read = SafeDirectory.read_file

    def read(directory, name):
        """Track metadata opens without altering target identity or read semantics."""
        opened.append(name)
        return real_read(directory, name)

    with patch.object(SafeDirectory, "read_file", read):
        result = inspect_layout(path)
    expected = (1025, 3072, 3074) if shape == "wide" else (66, 292, 19302)
    assert result.complete
    assert (
        len(result._storage.sizes),
        result._storage.object_references,
        len(list(_nodes(result.root))),
    ) == expected
    assert len(opened) == count + (2 if shape == "wide" else 3)
    assert len(opened) == len(set(opened))


def test_diagnostic_cap_on_invalid_slots_never_keeps_unreported_failures(
    tmp_path: Path,
) -> None:
    """An exhausted diagnostic budget leaves an explicit partial prefix, not hidden errors."""
    path = _layout(tmp_path, _index([None, None, None]), {})
    result = inspect_layout(path, limits=InspectionLimits(max_diagnostics=2))
    entry = result.root.children[0]
    assert len(entry.manifests) == 2
    assert [item.code for item in result.diagnostics] == [
        "INVALID_DESCRIPTOR",
        "LIMIT_REACHED",
    ]
    assert (
        tuple(error for descriptor in entry.manifests for error in descriptor.errors)
        == result.diagnostics
    )
    _validate(result.to_dict())


def test_subject_error_cap_stops_before_unvisited_manifest_fields(
    tmp_path: Path,
) -> None:
    """A subject diagnostic limit cannot leave an invalid config error outside the result projection."""
    raw = _raw({**json.loads(_manifest()), "subject": None, "config": None})
    path = _layout(tmp_path, _index([edge(raw, OCI_MANIFEST)]), {address(raw): raw})
    result = inspect_layout(path, limits=InspectionLimits(max_diagnostics=1))
    child = result.root.children[0].children[0]
    assert child.errors == result.diagnostics
    assert child.schema_version is None and child.config is None
    _validate(result.to_dict())


def test_cached_unreadable_metadata_keeps_sibling_and_does_not_retry(
    tmp_path: Path,
) -> None:
    """Permission failures are explicit and cached, distinct from a missing address."""
    path, manifest, _ = _tiny(tmp_path)
    (path / "index.json").write_bytes(_index([edge(manifest, OCI_MANIFEST)] * 2))
    name = address(manifest).split(":")[1]
    real_read = SafeDirectory.read_file
    attempts = []

    def read(directory, filename):
        """Inject a deterministic local permission failure after the no-follow stat."""
        if filename == name:
            attempts.append(filename)
            raise PermissionError("fixture denied")
        return real_read(directory, filename)

    with patch.object(SafeDirectory, "read_file", read):
        result = inspect_layout(path)
    assert attempts == [name]
    assert [item.code for item in result.diagnostics] == ["UNREADABLE_OBJECT"] * 2
    _validate(result.to_dict())


def test_descriptor_budget_counts_subject_slots_without_subject_io(
    tmp_path: Path,
) -> None:
    """A subject spends a descriptor unit, but its external address spends no object unit."""
    raw = _manifest(layers=[], subject=edge(b"external", OCI_MANIFEST))
    path = _layout(
        tmp_path,
        _index([edge(raw, OCI_MANIFEST)]),
        {address(raw): raw, address(b"{}"): b"{}"},
    )
    assert inspect_layout(
        path, limits=InspectionLimits(max_descriptors=3, max_objects=2)
    ).complete
    result = inspect_layout(path, limits=InspectionLimits(max_descriptors=2))
    assert not result.complete
    assert "max_descriptors cap 2" in result.diagnostics[-1].message


def test_derived_subject_error_is_projected_in_owner_traversal_order(
    tmp_path: Path,
) -> None:
    """Late tag resolution does not put a subject error after an earlier-discovered payload error."""
    manifest = _manifest(layers=[], subject=edge(b"outside", OCI_MANIFEST))
    listed = _index([edge(manifest, OCI_MANIFEST)])
    path = _layout(
        tmp_path,
        _index([edge(listed, OCI_INDEX, annotations={REF_NAME: "sha256-" + "a" * 64})]),
        {
            address(manifest): manifest,
            address(listed): listed,
        },
    )
    result = inspect_layout(path)
    assert [item.code for item in result.diagnostics] == [
        "CONFLICTING_DESCRIPTOR",
        "MISSING_OBJECT",
    ]
    actual = result.root.children[0].children[0].children[0]
    assert result.diagnostics == actual.subject.errors + actual.children[0].errors
    _validate(result.to_dict())


def test_list_member_index_subject_is_authoritative_and_source_owned(
    tmp_path: Path,
) -> None:
    """A referrer index member's explicit subject conflicts just like a manifest subject."""
    member = _index([], subject=edge(b"outside", OCI_INDEX))
    listed = _index([edge(member, OCI_INDEX)])
    path = _layout(
        tmp_path,
        _index([edge(listed, OCI_INDEX, annotations={REF_NAME: "sha256-" + "a" * 64})]),
        {
            address(member): member,
            address(listed): listed,
        },
    )
    result = inspect_layout(path)
    actual = result.root.children[0].children[0].children[0]
    assert actual.subject.digest == address(b"outside")
    assert actual.subject.errors[0].code == "CONFLICTING_DESCRIPTOR"
    assert actual.subject.errors == result.diagnostics
    _validate(result.to_dict())


def test_invalid_layer_slot_keeps_source_position_and_valid_platform_key_order(
    tmp_path: Path,
) -> None:
    """A missing layer digest never shifts the next visible layer to position zero."""
    payload = b"{}"
    manifest = _manifest(layers=[None, edge(payload, annotations={})])
    platform = {"architecture": "amd64", "os": "linux", "extension": {"x": None}}
    path = _layout(
        tmp_path,
        _index([edge(manifest, OCI_MANIFEST, platform=platform)]),
        {
            address(manifest): manifest,
            address(payload): payload,
        },
    )
    result = inspect_layout(path)
    owner = result.root.children[0].children[0]
    assert [item.position for item in owner.children] == [None, 1]
    assert owner.layers[0].digest is None
    assert owner.layers[1].annotations == {}
    assert list(result.to_dict()["children"][0]["manifests"][0]["platform"]) == list(
        platform
    )
    _validate(result.to_dict())

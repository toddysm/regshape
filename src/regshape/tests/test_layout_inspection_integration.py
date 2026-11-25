"""Cross-component stored-layout, CLI compatibility and exact stress regressions.

No live registry or container service participates. Fixture setup and preservation
snapshots happen outside the read-only IO guard; all four inspection views execute
the actual SDK and Click entry point against the same stored bytes.
"""

import hashlib
import inspect
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from regshape.cli.layout_inspect import render_inspection
from regshape.cli.main import regshape
from regshape.libs.layout import (
    IndexNode,
    InspectionLimits,
    LayoutInspectionError,
    ManifestNode,
    SummaryGroup,
    SummaryIndexNode,
    inspect_layout,
    read_index,
    read_stage,
    summarize_layout,
    validate_layout,
)
from regshape.tests.benchmark_layout_inspection import SHAPES, VIEWS, evaluate
from regshape.tests.layout_inspection_fixtures import (
    FAMILY,
    FIXTURES,
    PAYLOAD,
    STRESS_COUNTS,
    build_layout,
    metadata_files,
    observe_io,
)

EXPECTED = {
    "simple": (2, 3, 0),
    "nested": (3, 6, 0),
    "subject": (3, 6, 1),
    "shared": (3, 11, 0),
    "mixed": (4, 8, 0),
    "incomplete": (1, 5, 0),
    "busybox": (15, 21, 4),
}


@pytest.fixture(autouse=True)
def native_platform(request: pytest.FixtureRequest) -> None:
    """Keep unsupported-platform rejection portable; native fixture checks need Linux/macOS."""
    if (
        sys.platform not in ("linux", "darwin")
        and "unsupported_platform" not in request.node.name
    ):
        pytest.skip("Native layout inspection supports Linux/macOS only")


def runner() -> CliRunner:
    """Separate real stdout/stderr on both minimum Click 8.1 and newer Click."""
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        options = inspect.signature(CliRunner).bind(mix_stderr=False).kwargs
        return CliRunner(**options)
    return CliRunner()


def snapshot(path: Path) -> dict:
    """Record bytes, modes, identities and write timestamps, excluding access-time effects."""
    return {
        str(item.relative_to(path)): (
            item.read_bytes() if item.is_file() else None,
            item.stat().st_mode,
            item.stat().st_ino,
            item.stat().st_mtime_ns,
            item.stat().st_ctime_ns,
        )
        for item in [path, *sorted(path.rglob("*"))]
    }


def node_count(root: dict) -> int:
    """Count display nodes iteratively, not embedded descriptors or summary members."""
    pending = [root]
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        pending.extend(node.get("children", []))
    return count


@pytest.mark.parametrize("shape", FAMILY)
@pytest.mark.parametrize(
    "summary,as_json", [(False, False), (False, True), (True, False), (True, True)]
)
def test_stored_family_sdk_cli_parity_and_preservation(
    tmp_path: Path,
    shape: str,
    summary: bool,
    as_json: bool,
) -> None:
    """Match selected SDK output, exact source counts, diagnostics and unchanged stored layout."""
    path = build_layout(tmp_path / "layout", shape)
    metadata = metadata_files(path)
    before = snapshot(path)
    sdk = inspect_layout(path)
    selected = summarize_layout(sdk) if summary else sdk
    stats = summarize_layout(sdk).root.statistics
    assert (
        stats.unique_objects,
        stats.object_references,
        stats.subject_references,
    ) == EXPECTED[shape]
    assert stats.stored_bytes == sum(sdk._storage.sizes.values())
    args = ["layout", "inspect", "-p", str(path)]
    args += ["--summary"] if summary else []
    args += ["--json"] if as_json else []
    with observe_io(metadata) as facts:
        result = runner().invoke(regshape, args)
    expected_complete = shape not in ("mixed", "incomplete")
    assert sdk.complete == expected_complete
    assert result.exit_code == (0 if expected_complete else 1), result.exception
    assert result.stdout == (
        selected.to_json()
        if as_json
        else render_inspection(
            selected, encoding="utf-8", ascii_only=False, color=False
        )
    )
    assert result.stderr == "".join(
        f"{error.code}: {error.message}; Location: {error.location}; Source: {error.source}\n"
        for error in sdk.diagnostics
    )
    schema = json.loads(
        (
            Path(__file__).parents[3] / "specs/layout/layout-inspect.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(selected.to_dict())
    assert facts.metadata_opens == {name: 1 for name in metadata}
    assert all(count == 1 for count in facts.parses.values())
    assert len(facts.parses) == len(metadata)
    assert facts.metadata_bytes == sum(metadata.values())
    payload_name = hashlib.sha256(PAYLOAD).hexdigest()
    if shape not in ("busybox",):
        assert facts.target_stats[payload_name] == 1
    assert all(
        count == 1 for name, count in facts.target_stats.items() if name not in metadata
    )
    assert snapshot(path) == before


def test_busybox_pinned_metadata_and_locked_normalized_tree(tmp_path: Path) -> None:
    """Verify archived source digests and reproduce the entire approved tree, normalizing only root."""
    reference = json.loads((FIXTURES / "busybox-metadata.json").read_text())
    assert len(reference["documents"]) == 7
    for digest, value in reference["documents"].items():
        assert "sha256:" + hashlib.sha256(value.encode()).hexdigest() == digest
    path = build_layout(tmp_path / "layout", "busybox")
    result = runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    assert result.exit_code == 0, result.stderr
    spec = (Path(__file__).parents[3] / "specs/cli/layout-inspect.md").read_text()
    locked = (
        spec.split("## Baseline real-image example", 1)[1]
        .split("```text\n", 1)[1]
        .split("```", 1)[0]
    )
    assert result.stdout.replace(str(path), "/examples/mcr-busybox-1.36", 1) == locked
    assert result.stderr == ""
    summary = summarize_layout(inspect_layout(path))
    groups = summary.root.children[0].children
    assert groups is not None and isinstance(groups[0], SummaryIndexNode)
    assert groups[0].children is not None
    platform_group = groups[0].children[0]
    assert isinstance(platform_group, SummaryGroup)
    assert platform_group.category == "platform-images"
    assert (
        platform_group.references,
        platform_group.config_references,
        platform_group.layer_references,
    ) == (2, 2, 4)
    assert all(isinstance(group, SummaryGroup) for group in groups[1:])
    assert [
        (group.category, group.references)
        for group in groups[1:]
        if isinstance(group, SummaryGroup)
    ] == [
        ("signatures", 2),
        ("lifecycle", 2),
    ]
    assert (
        sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) < 16_384
    )


def test_occurrence_descriptor_provenance_survives_both_views(tmp_path: Path) -> None:
    """Repeated manifest occurrences retain different root/nested edge facts, never intrinsic annotations."""
    path = build_layout(tmp_path / "layout", "nested")
    expanded = inspect_layout(path).root.children[0].children
    assert expanded is not None and isinstance(expanded[0], IndexNode)
    assert expanded[0].children is not None
    nested = expanded[0].children[1]
    direct = expanded[1]
    assert isinstance(nested, ManifestNode) and isinstance(direct, ManifestNode)
    assert nested.incoming is not None and direct.incoming is not None
    assert nested.digest == direct.digest
    assert nested.incoming.annotations == {"edge": "nested"}
    assert direct.incoming.annotations == {"edge": "root"}
    assert nested.annotations == direct.annotations == {"fixture": "first"}
    assert nested.incoming.source.pointer == "/manifests/1"
    assert direct.incoming.source.document == "index.json"
    assert direct.incoming.source.pointer == "/manifests/1"
    for summary in (False, True):
        result = runner().invoke(
            regshape,
            ["layout", "inspect", "-p", str(path), "--json"]
            + (["--summary"] if summary else []),
        )
        assert result.exit_code == 0, result.stderr
        assert '"edge": "nested"' in result.stdout and '"edge": "root"' in result.stdout


@pytest.mark.parametrize("shape", STRESS_COUNTS)
def test_approved_stress_all_views_exact_counts_and_cached_io(
    tmp_path: Path, shape: str
) -> None:
    """Verify correctness gates before any timing; repeated occurrences do not multiply metadata/payload IO."""
    path = build_layout(tmp_path / "layout", shape)
    metadata = metadata_files(path)
    with observe_io(metadata) as facts:
        sdk = inspect_layout(path)
        summary = summarize_layout(sdk)
    expected_objects, expected_refs, expected_nodes = STRESS_COUNTS[shape]
    assert sdk.complete and summary.complete
    assert summary.root.statistics.unique_objects == expected_objects
    assert summary.root.statistics.object_references == expected_refs
    assert node_count(sdk.to_dict()) == expected_nodes
    assert facts.metadata_opens == {name: 1 for name in metadata}
    assert facts.target_stats[hashlib.sha256(PAYLOAD).hexdigest()] == 1
    assert facts.metadata_bytes == sum(metadata.values())
    for summarized in (False, True):
        for as_json in (False, True):
            selected = summary if summarized else sdk
            with observe_io(metadata) as cli_facts:
                result = runner().invoke(
                    regshape,
                    ["layout", "inspect", "-p", str(path)]
                    + (["--summary"] if summarized else [])
                    + (["--json"] if as_json else []),
                )
            assert result.exit_code == 0, result.stderr
            assert cli_facts.metadata_opens == facts.metadata_opens
            assert result.stdout == (
                selected.to_json()
                if as_json
                else render_inspection(
                    selected, encoding="utf-8", ascii_only=False, color=False
                )
            )
            assert result.stderr == ""


@pytest.mark.parametrize(
    "summary,as_json", [(False, False), (False, True), (True, False), (True, True)]
)
def test_integrated_occurrence_and_selected_output_limits(
    tmp_path: Path, summary: bool, as_json: bool
) -> None:
    """Fit exactly at independent caps, then report a real partial traversal or zero-output failure."""
    path = build_layout(tmp_path / "layout", "shared")
    sdk = inspect_layout(path, limits=replace(InspectionLimits(), max_occurrences=23))
    assert sdk.complete and node_count(sdk.to_dict()) == 23
    limited = inspect_layout(path, limits=replace(sdk.limits, max_occurrences=22))
    assert not limited.complete and limited.diagnostics[-1].code == "LIMIT_REACHED"
    selected = summarize_layout(sdk) if summary else sdk
    text = (
        selected.to_json()
        if as_json
        else render_inspection(
            selected, encoding="utf-8", ascii_only=False, color=False
        )
    )
    exact = replace(
        sdk, limits=replace(sdk.limits, max_output_bytes=len(text.encode()))
    )
    over = replace(
        exact, limits=replace(exact.limits, max_output_bytes=len(text.encode()) - 1)
    )
    args = (
        ["layout", "inspect", "-p", str(path)]
        + (["--summary"] if summary else [])
        + (["--json"] if as_json else [])
    )
    with patch("regshape.cli.layout.inspect_layout", return_value=exact):
        at_cap = runner().invoke(regshape, args)
    assert at_cap.exit_code == 0 and at_cap.stdout == text and at_cap.stderr == ""
    with patch("regshape.cli.layout.inspect_layout", return_value=over):
        above = runner().invoke(regshape, args)
    assert above.exit_code == 1 and above.stdout == ""
    assert above.stderr.startswith("OUTPUT_LIMIT:")
    with patch("regshape.cli.layout.inspect_layout", return_value=limited):
        partial = runner().invoke(regshape, args)
    assert partial.exit_code == 1 and partial.stdout
    assert "LIMIT_REACHED:" in partial.stderr


def test_inspection_does_not_change_legacy_operations(tmp_path: Path) -> None:
    """Run all views between legacy reads; preserve shallow validation, show JSON/warning and status."""
    path = build_layout(tmp_path / "layout", "nested")
    (path / ".regshape-stage.json").write_text(
        '{"layers":[],"config":null,"manifest":null}\n'
    )
    before = snapshot(path)
    old_index = read_index(path).to_json()
    old_stage = read_stage(path)
    validate_layout(path)
    status_before = runner().invoke(
        regshape, ["layout", "status", "-p", str(path), "--json"]
    )
    assert status_before.exit_code == 0
    for flags in ([], ["--json"], ["--summary"], ["--summary", "--json"]):
        result = runner().invoke(
            regshape, ["layout", "inspect", "-p", str(path), *flags]
        )
        assert result.exit_code == 0 and result.stderr == ""
    shown = runner().invoke(regshape, ["layout", "show", "-p", str(path)])
    assert shown.exit_code == 0
    assert shown.stdout == json.dumps(json.loads(old_index), indent=2) + "\n"
    assert shown.stderr == (
        "Warning: 'layout show' is deprecated in 0.2 and will be removed in 0.3;"
        " use 'layout inspect'.\n"
    )
    assert read_index(path).to_json() == old_index and read_stage(path) == old_stage
    validate_layout(path)
    status_after = runner().invoke(
        regshape, ["layout", "status", "-p", str(path), "--json"]
    )
    assert (
        status_after.stdout == status_before.stdout
        and status_after.stderr == status_before.stderr == ""
    )
    assert snapshot(path) == before


def test_unsupported_platform_all_views_refuse_reads(tmp_path: Path) -> None:
    """Windows rejection is explicit and happens before any file open in all four CLI views."""
    with (
        patch("regshape.libs.layout.inspection.sys.platform", "win32"),
        patch(
            "regshape.libs.artifacts.storage.SafeDirectory.open",
            side_effect=AssertionError("No reads allowed"),
        ),
    ):
        with pytest.raises(LayoutInspectionError) as error:
            inspect_layout(tmp_path)
        assert error.value.diagnostics[0].code == "UNSUPPORTED_PLATFORM"
        for flags in ([], ["--json"], ["--summary"], ["--summary", "--json"]):
            result = runner().invoke(
                regshape, ["layout", "inspect", "-p", str(tmp_path), *flags]
            )
            assert result.exit_code == 1 and result.stdout == ""
            assert result.stderr.startswith("UNSUPPORTED_PLATFORM:")


@pytest.mark.parametrize("summary", [False, True])
def test_integrated_safe_terminal_and_json_values(
    tmp_path: Path, summary: bool
) -> None:
    """Escape hostile source values in real ASCII CLI output while keeping JSON source data reversible."""
    path = build_layout(tmp_path / "layout", "nested")
    entry = path / "index.json"
    value = json.loads(entry.read_bytes())
    hostile = "control:\x1b[31m\n\t\u0085\u202e\\end"
    value["annotations"] = {"hostile": hostile}
    value["manifests"][1]["annotations"] = {"edge": hostile}
    entry.write_text(json.dumps(value))
    sdk = inspect_layout(path)
    selected = summarize_layout(sdk) if summary else sdk
    args = ["layout", "inspect", "-p", str(path)] + (["--summary"] if summary else [])
    with observe_io(metadata_files(path)):
        text = runner().invoke(regshape, [*args, "--ascii"], env={"NO_COLOR": "1"})
        wire = runner().invoke(regshape, [*args, "--json"], color=True)
    assert text.exit_code == wire.exit_code == 0
    assert text.stderr == wire.stderr == ""
    assert text.stdout == render_inspection(
        selected, encoding="utf-8", ascii_only=True, color=False
    )
    assert (
        text.stdout.isascii() and "\x1b" not in text.stdout and "\t" not in text.stdout
    )
    assert "\\u001b[31m\\n\\t\\u0085\\u202e\\\\end" in text.stdout
    assert wire.stdout == selected.to_json()
    assert json.loads(wire.stdout)["children"][0]["annotations"]["hostile"] == hostile


def test_resource_gate_checks_exact_caps_and_rejects_missing_approval() -> None:
    """Never turn baseline-only, missing-view or over-cap measurements into a quantitative pass."""
    cases = {f"{shape}/{view}" for shape in SHAPES for view in VIEWS}
    report: dict = {
        "fixture_revision": "fixture-r1",
        "environment": {"platform": "darwin-arm64"},
        "measurements": {
            key: {"elapsed_max_seconds": 2.0, "peak_rss_max_bytes": 100}
            for key in cases
        },
    }
    thresholds: dict = {
        "fixture_revision": "fixture-r1",
        "platform": "darwin-arm64",
        "approval": "https://github.com/toddysm/regshape/issues/216#issuecomment-123",
        "limits": {
            key: {"elapsed_seconds": 2.0, "peak_rss_bytes": 100} for key in cases
        },
    }
    assert all(check["passed"] for check in evaluate(report, thresholds))
    first = min(cases)
    thresholds["limits"][first]["elapsed_seconds"] = 1.99
    assert not all(check["passed"] for check in evaluate(report, thresholds))
    thresholds["limits"][first]["elapsed_seconds"] = 2.0
    thresholds["limits"][first]["peak_rss_bytes"] = 99
    assert not all(check["passed"] for check in evaluate(report, thresholds))
    for field in ("approval", "fixture_revision", "platform"):
        with pytest.raises(ValueError):
            evaluate(report, {**thresholds, field: "unapproved/mismatched"})
    thresholds["limits"].pop(first)
    with pytest.raises(ValueError, match="every shape/view"):
        evaluate(report, thresholds)


def test_metadata_inventory_rejects_descriptor_path_traversal(tmp_path: Path) -> None:
    """Reject path-like digest fields before constructing or reading any descriptor-derived file."""
    path = build_layout(tmp_path / "layout", "simple")
    entry = path / "index.json"
    value = json.loads(entry.read_bytes())
    value["manifests"][0]["digest"] = "../outside"
    entry.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Digest must be lowercase"):
        metadata_files(path)

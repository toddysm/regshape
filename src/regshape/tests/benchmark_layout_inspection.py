"""Measure all four offline CLI views in fresh processes, outside unit timing gates.

One warm-up and five measured runs use stable warm-cache fixtures. Elapsed time
covers the guarded CLI invocation, not interpreter startup or fixture discovery;
peak RSS covers the entire child process, including imports/capture. Instrumentation
overhead is included. Baseline measurements are never called a performance pass.
Workers inherit the parent-created fixture cwd and accept no input path.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.layout import inspect_layout, summarize_layout
from regshape.tests.layout_inspection_fixtures import (
    FIXTURES,
    STRESS_COUNTS,
    build_layout,
    metadata_files,
    observe_io,
)

VIEWS = ("expanded-text", "expanded-json", "summary-text", "summary-json")
SHAPES = ("busybox", "wide", "reused-index")


def hardware() -> dict:
    """Read public CPU/memory characteristics without serial numbers or user identifiers."""
    if sys.platform == "darwin":
        model, memory = subprocess.check_output(
            ["sysctl", "-n", "hw.model", "hw.memsize"], text=True
        ).splitlines()
        return {"model": model, "memory_bytes": int(memory)}
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.machine(),
    )
    return {
        "model": cpu,
        "memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
    }


def filesystem(path: Path) -> str:
    """Identify the fixture filesystem; errors remain explicit evidence failures."""
    if sys.platform == "darwin":
        device = (
            subprocess.check_output(["df", str(path)], text=True)
            .splitlines()[-1]
            .split()[0]
        )
        value = subprocess.check_output(["diskutil", "info", device], text=True)
        for line in value.splitlines():
            if "File System Personality:" in line:
                return line.split(":", 1)[1].strip()
        raise RuntimeError("Fixture filesystem personality was not reported")
    return subprocess.check_output(
        ["stat", "-f", "-c", "%T", str(path)], text=True
    ).strip()


def revision() -> str:
    """Bind the driver, fixture/IO builder and pinned metadata bytes independently of the commit."""
    builder = Path(__file__).with_name("layout_inspection_fixtures.py")
    return hashlib.sha256(
        Path(__file__).read_bytes()
        + builder.read_bytes()
        + (FIXTURES / "busybox-metadata.json").read_bytes()
    ).hexdigest()


def occurrences(value: dict) -> int:
    """Count expanded display nodes iteratively, ignoring descriptor-only source facts."""
    pending = [value]
    total = 0
    while pending:
        node = pending.pop()
        total += 1
        pending.extend(node.get("children", []))
    return total


def peak_rss_bytes() -> int:
    """Read this post-exec process's peak resident bytes, excluding inherited Linux high-water marks.

    Linux VmHWM resets at exec; macOS getrusage already reports this process.
    :raises RuntimeError: If Linux does not report a VmHWM value in KiB.
    """
    if sys.platform == "linux":
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                parts = line.split()
                if len(parts) == 3 and parts[2] == "kB":
                    return int(parts[1]) * 1024
        raise RuntimeError("Linux post-exec peak RSS (VmHWM in KiB) was not reported")
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def sample(path: Path, view: str) -> dict:
    """Run one actual CLI invocation, enforcing per-address IO before returning resource evidence.

    :raises AssertionError: For CLI failure, repeated metadata/payload IO or forbidden effects.
    :raises ValueError: For an unknown view.
    """
    if view not in VIEWS:
        raise ValueError(f"Unknown view: {view}")
    metadata = metadata_files(path)
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        options = inspect.signature(CliRunner).bind(mix_stderr=False).kwargs
        runner = CliRunner(**options)
    else:
        runner = CliRunner()
    args = ["layout", "inspect", "-p", str(path)]
    if view.startswith("summary"):
        args.append("--summary")
    if view.endswith("json"):
        args.append("--json")
    with observe_io(metadata) as facts:
        start = time.perf_counter()
        result = runner.invoke(regshape, args, env={"NO_COLOR": "1"})
        elapsed = time.perf_counter() - start
    assert result.exit_code == 0 and result.stderr == "", (
        result.exception,
        result.stderr,
    )
    assert facts.metadata_opens == {name: 1 for name in metadata}, (
        f"Metadata opens must occur exactly once per address ({view})"
    )
    assert len(facts.parses) == len(metadata) and all(
        count == 1 for count in facts.parses.values()
    )
    expected_bytes = sum(metadata.values())
    assert facts.metadata_bytes == expected_bytes, (
        f"Metadata read bytes {facts.metadata_bytes} != {expected_bytes} ({view})"
    )
    assert all(
        count == 1 for name, count in facts.target_stats.items() if name not in metadata
    ), f"Opaque target stats must occur exactly once per address ({view})"
    return {
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": peak_rss_bytes(),
        "output_bytes": len(result.stdout.encode("utf-8")),
        "io": {
            "opens": facts.opens,
            "stats": facts.stats,
            "metadata_opens": dict(facts.metadata_opens),
            "reads": dict(facts.reads),
            "parses": dict(facts.parses),
            "target_stats": dict(facts.target_stats),
            "metadata_bytes": facts.metadata_bytes,
        },
    }


def _validate_thresholds(
    thresholds: dict, fixture_revision: str, target_platform: str
) -> dict:
    """Validate approval identity and every resource cap before expensive measurement.

    :returns: Complete finite positive caps keyed by shape/view.
    :raises ValueError: For invalid identity, missing cases or malformed resource caps.
    Approval authenticity remains the workflow caller's responsibility.
    """
    if (
        not isinstance(thresholds, dict)
        or thresholds.get("fixture_revision") != fixture_revision
        or thresholds.get("platform") != target_platform
        or not isinstance(thresholds.get("approval"), str)
        or not thresholds["approval"].startswith(
            "https://github.com/toddysm/regshape/issues/216#issuecomment-"
        )
        or not thresholds["approval"].rsplit("-", 1)[-1].isascii()
        or not thresholds["approval"].rsplit("-", 1)[-1].isdecimal()
    ):
        raise ValueError(
            "Thresholds require matching fixture/platform and recorded #216 approval"
        )
    limits = thresholds.get("limits")
    expected = {f"{shape}/{view}" for shape in SHAPES for view in VIEWS}
    if not isinstance(limits, dict) or set(limits) != expected:
        raise ValueError("Thresholds must cover every shape/view exactly")
    for key, cap in limits.items():
        if not isinstance(cap, dict) or set(cap) != {
            "elapsed_seconds",
            "peak_rss_bytes",
        }:
            raise ValueError(f"Both quantitative resource caps are required: {key}")
        for metric, maximum in cap.items():
            if (
                type(maximum) not in (int, float)
                or not math.isfinite(maximum)
                or maximum <= 0
            ):
                raise ValueError(
                    f"Resource caps must be finite positive numbers: {key}/{metric}"
                )
    return limits


def evaluate(report: dict, thresholds: dict) -> list[dict]:
    """Compare every measured maximum with validated owner-approved limits.

    :returns: Every comparison, including failures; caller sets the process exit.
    :raises ValueError: For mismatched, incomplete or invalid thresholds/measurements.
    Approval authenticity remains the workflow caller's responsibility.
    """
    limits = _validate_thresholds(
        thresholds, report["fixture_revision"], report["environment"]["platform"]
    )
    if set(report["measurements"]) != set(limits):
        raise ValueError("Measurements must cover every shape/view exactly")
    checks = []
    for key, measured in report["measurements"].items():
        for metric, maximum in limits[key].items():
            actual = (
                measured["elapsed_max_seconds"]
                if metric == "elapsed_seconds"
                else measured["peak_rss_max_bytes"]
            )
            if (
                type(actual) not in (int, float)
                or not math.isfinite(actual)
                or actual <= 0
            ):
                raise ValueError(f"Resource measurement is invalid: {key}/{metric}")
            checks.append(
                {
                    "case": key,
                    "metric": metric,
                    "actual": actual,
                    "limit": maximum,
                    "passed": actual <= maximum,
                }
            )
    return checks


def measure(output: Path, thresholds: Path | None) -> bool:
    """Create stable fixtures, precheck structure, then retain every fresh-process sample.

    :returns: True for a recorded baseline, or the actual approved-threshold rerun result.
    :raises ValueError: For invalid threshold JSON, identity or caps, before any worker starts.
    :raises RuntimeError: If a worker fails; captured diagnostics and partial samples are retained.
    """
    fixture_revision = revision()
    target_platform = f"{sys.platform}-{platform.machine()}"
    approved = None
    if thresholds is not None:
        approved = json.loads(thresholds.read_text())
        _validate_thresholds(approved, fixture_revision, target_platform)
    report: dict = {
        "fixture_revision": fixture_revision,
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "environment": {
            "platform": target_platform,
            "python": sys.version,
            "os": platform.platform(),
            "hardware": hardware(),
            "cpu_count": os.cpu_count(),
            "cache": "stable files; one warm-up per view; metadata discovery warms reads; not cold cache",
            "measurement": "instrumented CLI elapsed excludes startup; RSS includes full fresh process",
        },
        "warmups_per_view": 1,
        "samples_per_view": 5,
        "fixtures": {},
        "measurements": {},
        "performance_gate": "not-approved; measurements only",
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="regshape-layout-benchmark-") as temp:
        root = Path(temp).resolve()
        report["environment"]["filesystem"] = filesystem(root)
        for shape in SHAPES:
            path = build_layout(root / shape, shape)
            sdk = inspect_layout(path)
            stats = summarize_layout(sdk).root.statistics
            actual = (
                stats.unique_objects,
                stats.object_references,
                occurrences(sdk.to_dict()),
            )
            assert sdk.complete and actual == STRESS_COUNTS.get(shape, (15, 21, 23)), (
                actual
            )
            report["fixtures"][shape] = {
                "unique_objects": actual[0],
                "object_references": actual[1],
                "display_nodes": actual[2],
                "metadata_bytes": sum(metadata_files(path).values()),
                "root_path_bytes": len(str(path).encode()),
                "filesystem_device": path.stat().st_dev,
            }
            for view in VIEWS:
                samples: list[dict] = []
                for run in range(6):
                    try:
                        child = subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "regshape.tests.benchmark_layout_inspection",
                                "--sample",
                                "--view",
                                view,
                            ],
                            capture_output=True,
                            text=True,
                            check=True,
                            cwd=path,
                            env={
                                **os.environ,
                                "PYTHONIOENCODING": "utf-8",
                                "NO_COLOR": "1",
                            },
                        )
                    except subprocess.CalledProcessError as error:
                        case = f"{shape}/{view}"
                        report["performance_gate"] = "failed"
                        report["failure"] = {
                            "case": case,
                            "iteration": run,
                            "returncode": error.returncode,
                            "stdout": error.stdout,
                            "stderr": error.stderr,
                            "partial_samples": samples,
                        }
                        output.write_text(json.dumps(report, indent=2) + "\n")
                        raise RuntimeError(
                            f"Resource worker failed: {case}, iteration {run}; "
                            f"evidence: {output}\nstdout: {error.stdout}\nstderr: {error.stderr}"
                        ) from error
                    value = json.loads(child.stdout)
                    if run:
                        samples.append(value)
                report["measurements"][f"{shape}/{view}"] = {
                    "elapsed_median_seconds": statistics.median(
                        s["elapsed_seconds"] for s in samples
                    ),
                    "elapsed_max_seconds": max(s["elapsed_seconds"] for s in samples),
                    "peak_rss_max_bytes": max(s["peak_rss_bytes"] for s in samples),
                    "samples": samples,
                }
                output.write_text(json.dumps(report, indent=2) + "\n")
    passed = True
    if approved is not None:
        report["checks"] = evaluate(report, approved)
        passed = all(check["passed"] for check in report["checks"])
        report["performance_gate"] = "passed" if passed else "failed"
        report["approval"] = approved["approval"]
    output.write_text(json.dumps(report, indent=2) + "\n")
    return passed


def main() -> None:
    """Write baseline/rerun JSON or emit one sample; fail explicitly on unsupported native platforms."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--sample", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--view", choices=VIEWS)
    args = parser.parse_args()
    if sys.platform not in ("linux", "darwin"):
        parser.error("Fresh-process resource evidence requires Linux or local macOS")
    if args.sample:
        if args.view is None:
            parser.error("--sample requires --view")
        print(json.dumps(sample(Path.cwd(), args.view)))
    elif args.output is None:
        parser.error("--output is required for baseline or approved-threshold reruns")
    else:
        passed = measure(args.output, args.thresholds)
        print(f"{'Recorded' if passed else 'FAILED'} resource evidence: {args.output}")
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

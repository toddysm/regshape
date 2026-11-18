"""Measure download release gates outside unit CI, retaining every raw sample.

Run with ``PYTHONPATH=src python -m regshape.tests.benchmark_downloads --output
report.json``. This loopback-only harness never contacts real registries. It
profiles private worker/assembly boundaries without changing public API records.
Threshold failures are reported, not converted into flaky unit-test timing gates.
"""

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from regshape.libs.blobs import (
    BlobDownload,
    DownloadOptions,
    DownloadStatus,
    get_blob,
    get_blobs,
)
from regshape.libs.blobs._ranges import _RangePart, _RangeScratch
from regshape.libs.manifests.download import _DownloadFile
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport._coordinator import _Coordinator
from regshape.tests.artifact_fixtures import address
from regshape.tests.download_fixture import DownloadFixture

MiB = 1_048_576


@dataclass(frozen=True)
class Case:
    """One reproducible fixture; baseline and candidate share payload and network policy."""

    name: str
    count: int
    size: int
    modes: tuple[str, ...]
    shaped: bool = True
    capability: Literal["ranges", "ignored", "missing-etag"] = "ranges"
    gate: str | None = None
    gate_mode: str | None = None


CASES = (
    Case(
        "many-small",
        32,
        MiB,
        ("sequential", "objects", "combined"),
        gate="speedup",
        gate_mode="objects",
    ),
    Case(
        "single-large",
        1,
        128 * MiB,
        ("sequential", "objects", "ranges"),
        gate="speedup",
        gate_mode="ranges",
    ),
    Case("combined", 4, 32 * MiB, ("sequential", "combined")),
    Case(
        "small-default",
        1,
        65_536,
        ("sequential", "objects"),
        gate="guardrail",
        gate_mode="objects",
    ),
    Case(
        "missing-etag",
        1,
        128 * MiB,
        ("sequential", "ranges"),
        capability="missing-etag",
        gate="guardrail",
        gate_mode="ranges",
    ),
    Case(
        "ignored-range",
        1,
        128 * MiB,
        ("sequential", "ranges"),
        capability="ignored",
        gate="guardrail",
        gate_mode="ranges",
    ),
    Case("fast-many", 32, MiB, ("sequential", "objects"), shaped=False),
    Case("fast-large", 1, 128 * MiB, ("sequential", "ranges"), shaped=False),
    Case("fast-combined", 4, 32 * MiB, ("sequential", "combined"), shaped=False),
    Case("legacy-many", 32, MiB, ("sequential", "legacy")),
    Case("legacy-large", 1, 128 * MiB, ("sequential", "legacy")),
)


def percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank quantile; nonempty samples and 0<fraction<=1 are required."""
    if not values or not 0 < fraction <= 1:
        raise ValueError("A quantile requires samples and a fraction in (0, 1]")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def comparison(baseline: list[dict], candidate: list[dict], gate: str | None) -> dict:
    """Evaluate exact median-speedup/p95-ratio gates, requiring 20 finite measured samples.

    :returns: Both ratios and a pass/fail decision, or None without a numeric gate.
    :raises ValueError: If samples cannot constitute the approved release evidence.
    """
    times = [
        [sample["wall_seconds"] for sample in samples]
        for samples in (baseline, candidate)
    ]
    if any(
        len(values) < 20
        or any(not math.isfinite(value) or value <= 0 for value in values)
        for values in times
    ):
        raise ValueError(
            "Release evidence requires at least 20 positive finite measured samples"
        )
    speedup = statistics.median(times[0]) / statistics.median(times[1])
    ratio = percentile(times[1], 0.95) / percentile(times[0], 0.95)
    if gate not in (None, "speedup", "guardrail"):
        raise ValueError("Unknown release gate")
    return {
        "median_speedup": speedup,
        "p95_ratio": ratio,
        "gate": gate,
        "passed": speedup >= 1.5
        if gate == "speedup"
        else ratio <= 1.20
        if gate
        else None,
    }


def measure(
    server: DownloadFixture,
    objects: dict[str, bytes],
    options: DownloadOptions,
    root: Path,
) -> dict:
    """Time one saved-output operation, verifying cleanup outside the timed interval.

    Worker wall/CPU time is summed over actual tasks; caller CPU includes ordered
    range assembly. Scratch read/write byte counts cover successful range spools,
    not physical device I/O, filesystem metadata, cache flushes or secure erasure.
    """
    worker: defaultdict[str, float] = defaultdict(float)
    scratch: defaultdict[str, float] = defaultdict(float)
    lock = threading.Lock()
    statuses: list[DownloadStatus] = []
    task, consume = _Coordinator._task, _RangePart.consume
    read_file, write = _RangeScratch.read_file, _DownloadFile.write
    coordinators = []
    enter = _Coordinator.__enter__

    def entered(coordinator):
        """Retain the real coordinator's final pending-job/resource peaks."""
        coordinators.append(coordinator)
        return enter(coordinator)

    def timed_task(coordinator, *args):
        """Profile worker-thread execution without charging HTTP-server CPU to workers."""
        began, cpu = time.perf_counter(), time.thread_time()
        try:
            return task(coordinator, *args)
        finally:
            with lock:
                worker["wall_seconds"] += time.perf_counter() - began
                worker["cpu_seconds"] += time.thread_time() - cpu

    class Reader:
        """Time the assembler's actual buffered spool reads, excluding hash/output work."""

        def __init__(self, source):
            """Borrow an open spool; the original context manager retains close ownership."""
            self.source = source

        def fileno(self):
            """Preserve the assembler's retained-inode validation against the original handle."""
            return self.source.fileno()

        def read(self, size):
            """Record bytes returned and buffered-read elapsed time, not physical disk latency."""
            began = time.perf_counter()
            chunk = self.source.read(size)
            scratch["read_seconds"] += time.perf_counter() - began
            scratch["read_bytes"] += len(chunk)
            return chunk

    @contextmanager
    def timed_read(owner, path):
        """Yield an observed spool reader without extending or changing the handle lifetime."""
        with read_file(owner, path) as source:
            yield Reader(source)

    def written(output, chunk):
        """Measure spool writes; ordinary unbounded final outputs do not use _DownloadFile."""
        began = time.perf_counter()
        write(output, chunk)
        with lock:
            scratch["write_seconds"] += time.perf_counter() - began
            scratch["write_bytes"] += len(chunk)

    def consumed(part, coordinator):
        """Record total ordered assembly time, including scratch read/hash/write and callbacks."""
        began = time.perf_counter()
        try:
            return consume(part, coordinator)
        finally:
            scratch["assembly_seconds"] += time.perf_counter() - began

    server.reset()
    with patch(
        "regshape.libs.transport.client.resolve_credentials",
        return_value=(None, None),
    ):
        client = RegistryClient(TransportConfig(server.authority, insecure=True))
    requests = [
        BlobDownload(digest, str(root / str(index)), len(raw))
        for index, (digest, raw) in enumerate(objects.items())
    ]
    with (
        patch.object(_Coordinator, "_task", timed_task),
        patch.object(_Coordinator, "__enter__", entered),
        patch.object(_RangePart, "consume", consumed),
        patch.object(_RangeScratch, "read_file", timed_read),
        patch.object(_DownloadFile, "write", written),
    ):
        began, cpu, caller = (
            time.perf_counter(),
            time.process_time(),
            time.thread_time(),
        )
        if options.transport == "legacy":
            blobs = tuple(
                get_blob(
                    client,
                    "repo",
                    request.digest,
                    request.output_path,
                    total_size=request.total_size,
                    download_options=options,
                )
                for request in requests
            )
        elif len(requests) == 1:
            request = requests[0]
            result = get_blob(
                client,
                "repo",
                request.digest,
                request.output_path,
                total_size=request.total_size,
                download_options=options,
                download_status_callback=statuses.append,
            )
            blobs = (result,)
        else:
            batch = get_blobs(
                client,
                "repo",
                requests,
                download_options=options,
                download_status_callback=statuses.append,
            )
            blobs = batch.blobs
        wall = time.perf_counter() - began
        process_cpu = time.process_time() - cpu
        caller_cpu = time.thread_time() - caller
    server.wait_idle()
    if [blob.digest for blob in blobs] != list(objects):
        raise RuntimeError("Benchmark returned wrong or unordered verified digests")
    if options.transport != "legacy" and (
        not statuses or statuses[-1].event != "summary"
    ):
        raise RuntimeError("Benchmark has no final metrics")
    stats = statuses[-1].stats if statuses else None
    physical = sum(server.calls[key] for key in ("blob", "storage", "probe", "token"))
    if stats is not None and (
        stats.request_count != physical
        or stats.bytes_accepted != sum(map(len, objects.values()))
        or stats.active_requests_peak > options.concurrency
        or stats.scratch_bytes_peak > options.concurrency * options.range_size
        or any(
            coordinator.pending or coordinator.scratch_bytes
            for coordinator in coordinators
        )
    ):
        raise RuntimeError("Benchmark violated request, content or resource accounting")
    for request in requests:
        if request.output_path is None:
            raise RuntimeError("Benchmark requires saved output")
        path = Path(request.output_path)
        if address(path.read_bytes()) != request.digest:
            raise RuntimeError(
                "Benchmark output failed independent digest verification"
            )
        path.unlink()
    if list(root.iterdir()):
        raise RuntimeError("Benchmark retained unexpected scratch/output")
    return {
        "wall_seconds": wall,
        "process_cpu_seconds": process_cpu,
        "caller_cpu_seconds": caller_cpu,
        "worker_wall_seconds": worker["wall_seconds"],
        "worker_cpu_seconds": worker["cpu_seconds"],
        "scratch_read_bytes": int(scratch["read_bytes"]),
        "scratch_write_bytes": int(scratch["write_bytes"]),
        "scratch_read_seconds": scratch["read_seconds"],
        "scratch_write_seconds": scratch["write_seconds"],
        "range_assembly_seconds": scratch["assembly_seconds"],
        "jobs_peak": max(
            (coordinator.jobs_peak for coordinator in coordinators), default=0
        ),
        "physical_request_count": physical,
        "connection_count": len(server.data_ports),
        "server_peak_data_handlers": server.peak,
        "server_body_bytes_sent": server.sent,
        "fallbacks": dict(
            Counter(event.reason for event in statuses if event.event == "fallback")
        ),
        "stats": asdict(stats) if stats is not None else None,
    }


def run_case(case: Case, samples: int, warmups: int, root: Path) -> dict:
    """Alternate mode order per round after unmeasured per-mode warm-ups on one fixture."""
    objects = {
        address(raw): raw
        for raw in (bytes([index]) * case.size for index in range(case.count))
    }
    raw: dict[str, list[dict]] = {mode: [] for mode in case.modes}
    policies = {
        mode: DownloadOptions.legacy()
        if mode == "legacy"
        else DownloadOptions(
            concurrency=1 if mode == "sequential" else 4,
            ranges_per_blob=2 if mode in ("ranges", "combined") else 1,
            range_size=8 * MiB,
            max_retries=3,
            scratch_dir=root,
        )
        for mode in case.modes
    }
    with DownloadFixture(
        objects,
        capability=case.capability,
        latency=0.025 if case.shaped else 0,
        bandwidth=32 * MiB if case.shaped else 0,
    ) as server:
        for _ in range(warmups):
            for mode in case.modes:
                measure(server, objects, policies[mode], root)
        for index in range(samples):
            order = case.modes if index % 2 == 0 else tuple(reversed(case.modes))
            for mode in order:
                raw[mode].append(measure(server, objects, policies[mode], root))
            print(f"{case.name}: {index + 1}/{samples}", file=sys.stderr, flush=True)
    baseline = raw["sequential"]
    return {
        "fixture": asdict(case),
        "warmups_per_mode": warmups,
        "latency_seconds": 0.025 if case.shaped else 0,
        "bytes_per_second_per_connection": 32 * MiB if case.shaped else 0,
        "authentication": "anonymous, same policy for every mode",
        "pool_policy": "operation-owned shared adapter; fresh operation per sample",
        "modes": {
            mode: {
                "policy": {
                    "concurrency": policies[mode].concurrency,
                    "ranges_per_blob": policies[mode].ranges_per_blob,
                    "range_size": policies[mode].range_size,
                    "max_retries": policies[mode].max_retries,
                    "transport": policies[mode].transport,
                },
                "p50_seconds": statistics.median(
                    sample["wall_seconds"] for sample in values
                ),
                "p95_seconds": percentile(
                    [sample["wall_seconds"] for sample in values], 0.95
                ),
                "comparison": comparison(
                    baseline,
                    values,
                    case.gate if mode == case.gate_mode else None,
                ),
                "samples": values,
            }
            for mode, values in raw.items()
        },
    }


def main() -> None:
    """Write complete raw benchmark evidence; failing numeric gates produce exit status one.

    Output starts incomplete before the first fixture and is checkpointed after
    each fixture, replacing any previous run. Runtime/protocol failures propagate
    rather than fabricating completed samples. No live-registry certification is
    possible from this harness, regardless of numeric fixture results.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=1)
    args = parser.parse_args()
    if args.samples < 20 or args.warmups < 1:
        parser.error(
            "Use at least 20 measured samples and at least one warm-up per mode"
        )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    case_reports: list[dict] = []
    report = {
        "schema_version": 1,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": commit,
        "dirty_paths": dirty,
        "environment": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "dependencies": {
                name: version(name) for name in ("requests", "urllib3", "click")
            },
            "storage": "OS temporary directory, warm OS caches, buffered writes; no fsync",
            "network": "HTTP/1.1 TCP_NODELAY loopback; no TLS/proxy/cloud; app-paced 64 KiB writes",
            "content": "deterministic distinct repeated-byte entities; identity coding, no compression",
            "cpu_scope": "process includes loopback servers; worker and caller thread CPU reported separately",
            "isolation": "host scheduling not controlled by harness; no CPU affinity, load isolation or frequency lock",
        },
        "registry_release_gate": "BLOCKED: two pinned actual registries and authorized storage redirect not run",
        "legacy_baseline": "Separate end-to-end singular loops; original transport, no fabricated pooled stats",
        "complete": False,
        "cases": case_reports,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="regshape-benchmark-") as directory:
        for case in CASES:
            case_reports.append(
                run_case(case, args.samples, args.warmups, Path(directory))
            )
            args.output.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
    report["complete"] = True
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report["numeric_gates_passed"] = all(
        mode["comparison"]["passed"] is not False
        for case in case_reports
        for mode in case["modes"].values()
    )
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["numeric_gates_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

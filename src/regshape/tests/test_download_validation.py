"""Stress physical cross-mode contracts without timing-based performance assertions.

Existing focused modules own detailed protocol/ownership fault matrices. These
tests compose auth probes, redirects, scheduling, spools and public observations
over actual loopback connections, including supported Windows blob paths.
"""

import json
import os
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from socketserver import _SocketWriter
from unittest.mock import patch

import pytest
import requests

from regshape.libs.blobs import BlobDownload, DownloadOptions, get_blobs
from regshape.libs.errors import BlobError
from regshape.libs.transport._coordinator import _Coordinator, _Worker
from regshape.libs.transport.download import DownloadTransport
from regshape.tests import benchmark_downloads
from regshape.tests.artifact_fixtures import address
from regshape.tests.benchmark_downloads import comparison, measure
from regshape.tests.download_fixture import DownloadFixture, _header_value
from regshape.tests.test_blob_batch import _client

R = 65_536


@pytest.mark.parametrize(
    "first,second", [("blobs", "artifacts"), ("artifacts", "blobs")]
)
def test_public_download_apis_import_in_fresh_interpreter(first, second):
    """Public SDK imports must not depend on CLI/test collection warming their modules."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import regshape.libs.{first}; import regshape.libs.{second}; "
                "from regshape.libs.blobs import get_blobs; "
                "from regshape.libs.artifacts import pull_artifact"
            ),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "count,concurrency,ranges,overlap",
    [(16, 1, 1, 1), (16, 4, 1, 4), (1, 4, 2, 2), (16, 4, 2, 4)],
    ids=["sequential", "object-parallel", "range-parallel", "combined"],
)
@pytest.mark.parametrize("capability", ["ranges", "ignored", "missing-etag"])
def test_cross_mode_auth_redirect_stress(
    tmp_path,
    count,
    concurrency,
    ranges,
    overlap,
    capability,
):
    """Repeat one capped invocation with real probes/token/redirects and exact final content."""
    payloads = [bytes([index]) * (6 * R + 1) for index in range(count)]
    objects = {address(raw): raw for raw in payloads}
    # A missing/ignored first validator cannot overlap later parts of a single blob.
    expected_overlap = overlap if count > 1 or capability == "ranges" else 1
    caller = threading.get_ident()
    coordinators, record_sizes = [], []
    enter, emit = _Coordinator.__enter__, _Worker.emit

    def entered(coordinator):
        """Retain the actual bounded scheduler for post-join resource assertions."""
        coordinators.append(coordinator)
        return enter(coordinator)

    def emitted(worker, payload):
        """Measure actual bridge payload sizes without buffering blob data or changing delivery."""
        record_sizes.append(len(repr(payload).encode("utf-8")))
        return emit(worker, payload)

    with (
        DownloadFixture(
            objects,
            capability=capability,
            authenticated=True,
            redirected=True,
            overlap=expected_overlap,
            gate_later_ranges=count == 1 and capability == "ranges",
        ) as server,
        patch.object(_Coordinator, "__enter__", entered),
        patch.object(
            _Worker,
            "emit",
            emitted,
        ),
    ):
        for iteration in range(3):
            server.reset()
            output = tmp_path / str(iteration)
            output.mkdir()
            progress, statuses, threads = [], [], []

            def observe(event, threads=threads, statuses=statuses):
                """Deliver safe frozen statuses only on the caller, after per-blob spool cleanup."""
                threads.append(threading.get_ident())
                statuses.append(event)
                assert len(json.dumps(asdict(event)).encode()) < 65_536
                if event.event == "completed":
                    coordinator = coordinators[-1]
                    assert not any(
                        pending.job.transfer_id == event.transfer_id
                        for pending in coordinator.pending.values()
                    )

            result = get_blobs(
                _client(server.authority),
                "repo",
                [
                    BlobDownload(digest, str(output / str(index)), len(raw))
                    for index, (digest, raw) in enumerate(objects.items())
                ],
                download_options=DownloadOptions(
                    concurrency=concurrency,
                    ranges_per_blob=ranges,
                    range_size=R,
                    scratch_dir=output,
                    max_retries=0,
                ),
                progress_callback=progress.append,
                download_status_callback=observe,
            )
            assert tuple(blob.digest for blob in result.blobs) == tuple(objects)
            assert [
                path.read_bytes()
                for path in sorted(output.iterdir(), key=lambda p: int(p.name))
            ] == payloads
            assert (
                statuses[-1].event == "summary" and statuses[-1].stats == result.stats
            )
            assert Counter(event.event for event in statuses)["completed"] == count
            assert set(threads) == {caller}
            assert server.calls["probe"] == server.calls["token"] == 1
            assert server.calls["unauthorized"] == 2
            physical = sum(
                server.calls[key] for key in ("blob", "storage", "probe", "token")
            )
            assert physical == result.stats.request_count
            assert expected_overlap <= result.stats.active_requests_peak <= concurrency
            # An abandoned no-ETag body can still be writing after the client closes it.
            if capability != "missing-etag" or ranges == 1:
                assert expected_overlap <= server.requests_peak <= concurrency
            assert result.stats.bytes_accepted == sum(map(len, payloads))
            assert result.stats.bytes_received == sum(map(len, payloads))
            assert result.stats.scratch_bytes_peak <= concurrency * R
            assert not server.foreign_authorization
            if count > 1 and concurrency > 1:
                assert len(server.data_ports) < len(server.intervals)
            coordinator = coordinators[-1]
            assert coordinator.jobs_peak <= concurrency
            assert not coordinator.pending and coordinator.queue.empty()
            assert coordinator.queue.maxsize == 4 * concurrency
            assert coordinator.scratch_bytes == 0
            assert all(session.closed for session in coordinator.transport.sessions)
            assert coordinator.transport.closed
            assert max(record_sizes) < 65_536
            for digest in objects:
                entries = [event for event in progress if event.digest == digest]
                assert entries[-1].phase == "completed"
                assert len({event.transfer_id for event in entries}) == 1
                intervals = [
                    interval for key, interval in server.intervals if key == digest
                ]
                if ranges > 1 and capability == "ranges":
                    assert intervals.count(f"bytes=0-{R - 1}") == 1
                    assert len(intervals) == 7
                elif ranges > 1 and capability == "missing-etag":
                    assert len(intervals) == 2
                else:
                    assert len(intervals) == 1


@pytest.mark.parametrize("ranges", [1, 2])
def test_cross_mode_corruption_never_reports_invalid_completion(tmp_path, ranges):
    """A physically well-framed corrupt blob fails after drain without a verified result."""
    expected = b"expected" * R
    digest = address(expected)
    statuses = []
    with (
        DownloadFixture({digest: b"incorrect" * (len(expected) // 9)}) as server,
        pytest.raises(BlobError),
    ):
        get_blobs(
            _client(server.authority),
            "repo",
            [BlobDownload(digest, str(tmp_path / "output"))],
            download_options=DownloadOptions(
                concurrency=4,
                ranges_per_blob=ranges,
                range_size=R,
                scratch_dir=tmp_path,
                max_retries=0,
            ),
            download_status_callback=statuses.append,
        )
    assert not any(event.event == "completed" for event in statuses)
    assert statuses[-1].event == "summary"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("control", ["probe", "token", "redirect"])
def test_control_hops_overlap_data_under_independent_server_cap(control):
    """A new scope's control/redirect hop overlaps three bodies without using SDK counters."""
    payload = b"control-overlap" * R
    digest = address(payload)
    with DownloadFixture(
        {digest: payload},
        authenticated=True,
        redirected=True,
        overlap=4,
    ) as server:
        server.gating = False
        with DownloadTransport(_client(server.authority), DownloadOptions()) as root:
            context = root._download_context
            with root.get(f"/v2/repo/blobs/{digest}", stream=True) as response:
                assert response.content == payload
            server.reset()
            before = context.request_count
            server.control_kind = control
            server.control_release = threading.Event()
            server.gating = True

            def receive(repo):
                """Borrow one worker-local session for one full framed data response."""
                worker = context.fork()
                try:
                    with worker.get(
                        f"/v2/{repo}/blobs/{digest}", stream=True
                    ) as response:
                        assert response.status_code == 200
                        return response.content
                finally:
                    worker._session.close()

            attempted = threading.Event()
            send = root._session.send

            def overflow_send(request, **kwargs):
                """Verify an independent C-request server snapshot exhausts the real limiter."""
                if not attempted.is_set():
                    acquired = context.slots.acquire(blocking=False)
                    if acquired:
                        context.slots.release()
                    attempted.set()
                    assert not acquired, "A control hop bypassed the global limiter"
                return send(request, **kwargs)

            def overflow_receive():
                """Use the otherwise idle root session for a fifth, excess logical request."""
                with root.get(f"/v2/repo/blobs/{digest}", stream=True) as response:
                    return response.content

            with (
                ThreadPoolExecutor(max_workers=5) as executor,
                patch.object(root._session, "send", overflow_send),
            ):
                futures = [
                    executor.submit(receive, repo)
                    for repo in ["repo", "repo", "repo", "other"]
                ]
                try:
                    with server.condition:
                        assert server.condition.wait_for(
                            lambda: bool(server.control_overlaps),
                            timeout=10,
                        )
                        assert server.requests_active == 4
                    overflow = executor.submit(overflow_receive)
                    assert attempted.wait(10)
                finally:
                    server.control_release.set()
                assert [future.result(timeout=15) for future in futures] == [
                    payload
                ] * 4
                assert overflow.result(timeout=15) == payload
            assert server.requests_peak == 4
            assert "storage" in server.control_overlaps
            assert (
                "blob" if control == "redirect" else control
            ) in server.control_overlaps
            assert context.active == 0 and context.active_peak == 4
            physical = sum(
                server.calls[key] for key in ("blob", "storage", "probe", "token")
            )
            assert context.request_count - before == physical


def test_failed_benchmark_rerun_invalidates_previous_complete_report(
    tmp_path, monkeypatch
):
    """A failure before the first measured fixture cannot leave stale passing release evidence."""
    output = tmp_path / "report.json"
    output.write_text(json.dumps({"complete": True, "numeric_gates_passed": True}))
    primary = RuntimeError("synthetic first fixture failure")
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(output)])
    with (
        patch.object(benchmark_downloads, "run_case", side_effect=primary),
        pytest.raises(
            RuntimeError,
        ) as caught,
    ):
        benchmark_downloads.main()
    assert caught.value is primary
    report = json.loads(output.read_text())
    assert report["complete"] is False and report["cases"] == []
    assert "numeric_gates_passed" not in report


def test_redirect_encodes_request_derived_path_component():
    """Keep request-supplied percent escapes inside the redirect's encoded path component."""
    digest = "sha256:example%0D%0AInjected:yes"
    with (
        DownloadFixture({digest: b"content"}, redirected=True) as server,
        requests.Session() as session,
    ):
        session.trust_env = False
        with session.get(
            f"http://{server.authority}/v2/repo/blobs/{digest}",
            allow_redirects=False,
            timeout=5,
        ) as response:
            assert response.status_code == 307
            assert response.headers["Location"] == (
                f"http://{server.storage_authority}/storage/"
                "sha256:example%250D%250AInjected:yes"
            )
            assert "Injected" not in response.headers


@pytest.mark.parametrize("line_break", ["\r", "\n", "\r\n"])
def test_fixture_rejects_response_header_line_breaks(line_break):
    """Invalid header values must fail explicitly, not emit or silently strip injected fields."""
    with pytest.raises(ValueError, match="Invalid fixture response header"):
        _header_value(f"safe{line_break}Injected: yes")


def test_many_small_gate_does_not_apply_to_observational_combined_mode(tmp_path):
    """A slow combined sample cannot fail the approved object-only S1 speedup gate."""
    case = next(case for case in benchmark_downloads.CASES if case.name == "many-small")

    def sample(server, objects, options, root):
        """Make object parallelism pass while the observational combined mode is slower."""
        return {
            "wall_seconds": 2.0
            if options.concurrency == 1
            else (1.0 if options.ranges_per_blob == 1 else 4.0),
        }

    with patch.object(benchmark_downloads, "measure", side_effect=sample):
        report = benchmark_downloads.run_case(case, 20, 1, tmp_path)
    modes = report["modes"]
    assert modes["objects"]["comparison"]["passed"] is True
    assert modes["combined"]["comparison"]["median_speedup"] == 0.5
    assert modes["combined"]["comparison"]["gate"] is None
    assert modes["combined"]["comparison"]["passed"] is None


def test_benchmark_waits_for_server_counter_updates_after_client_receipt(tmp_path):
    """A descheduled final server write cannot publish incomplete sent-byte observations."""
    raw = b"late-server-observation" * 2048
    objects = {address(raw): raw}
    written, observing, release, entering = (threading.Event() for _ in range(4))
    write = _SocketWriter.write

    def delayed_write(writer, body):
        """Send the complete body, then pause before the fixture can update its counters."""
        result = write(writer, body)
        if len(body) == len(raw):
            if not entering.wait(10):
                raise RuntimeError("Client did not enter post-receipt observation")
            written.set()
            if not release.wait(10):
                raise RuntimeError("Delayed fixture write was not released")
        return result

    with DownloadFixture(objects) as server:
        wait_idle = server.wait_idle

        def observed_idle():
            """Signal post-receipt observation while the server's sent counter is still zero."""
            if server.intervals:
                entering.set()
                assert written.wait(10)
                assert server.sent == 0
                observing.set()
            wait_idle()

        with (
            patch.object(_SocketWriter, "write", delayed_write),
            patch.object(server, "wait_idle", observed_idle),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(
                measure, server, objects, DownloadOptions(), tmp_path
            )
            try:
                assert observing.wait(10)
                assert not future.done()
            finally:
                release.set()
            sample = future.result(timeout=15)
    assert sample["server_body_bytes_sent"] == len(raw)


def test_benchmark_release_gates_use_median_and_nearest_rank_p95():
    """Reject under-sampled evidence and detect median/guardrail regressions independently."""
    baseline = [{"wall_seconds": 2.0}] * 20
    with pytest.raises(ValueError, match="20"):
        comparison(baseline[:19], baseline, "speedup")
    fast = [{"wall_seconds": 1.3}] * 20
    assert comparison(baseline, fast, "speedup")["passed"] is True
    assert (
        comparison(baseline, [{"wall_seconds": 1.34}] * 20, "speedup")["passed"]
        is False
    )
    # One slowest sample is outside nearest-rank p95; a second crosses the guardrail.
    candidate = [{"wall_seconds": 2.4}] * 19 + [{"wall_seconds": 100.0}]
    assert comparison(baseline, candidate, "guardrail")["passed"] is True
    candidate[-2] = {"wall_seconds": 2.4001}
    assert comparison(baseline, candidate, "guardrail")["passed"] is False


@pytest.mark.parametrize(
    "mode,capability",
    [
        ("full", "ranges"),
        ("ranged", "ranges"),
        ("ranged", "ignored"),
        ("ranged", "missing-etag"),
        ("legacy", "ranges"),
    ],
)
def test_benchmark_measures_real_operations_and_preserves_legacy_gaps(
    tmp_path,
    mode,
    capability,
):
    """Benchmark counters come from verified physical receipt, not fabricated legacy metrics."""
    objects = {address(raw): raw for raw in (b"a" * (5 * R), b"b" * (5 * R))}
    options = (
        DownloadOptions.legacy()
        if mode == "legacy"
        else DownloadOptions(
            concurrency=4,
            ranges_per_blob=2 if mode == "ranged" else 1,
            range_size=R,
            scratch_dir=tmp_path,
        )
    )
    with DownloadFixture(objects, capability=capability) as server:
        sample = measure(server, objects, options, tmp_path)
    assert sample["wall_seconds"] > 0 and sample["process_cpu_seconds"] >= 0
    assert not list(tmp_path.iterdir())
    if mode == "legacy":
        assert sample["stats"] is None and sample["worker_cpu_seconds"] == 0
        assert sample["physical_request_count"] == 2
    else:
        assert sample["stats"]["bytes_accepted"] == 10 * R
        assert sample["physical_request_count"] == sample["stats"]["request_count"]
        if mode == "ranged" and capability == "ranges":
            assert (
                sample["scratch_read_bytes"] == sample["scratch_write_bytes"] == 10 * R
            )
        else:
            assert sample["scratch_read_bytes"] == sample["scratch_write_bytes"] == 0


@pytest.mark.parametrize("count", [1, 2], ids=["single", "batch"])
def test_benchmark_reports_missing_final_metrics_explicitly(tmp_path, count):
    """Missing callbacks fail with the benchmark diagnostic for single and batch downloads."""
    objects = {address(raw): raw for raw in (b"a", b"b")[:count]}
    api_name = "get_blob" if count == 1 else "get_blobs"
    download = getattr(benchmark_downloads, api_name)

    def without_status(*args, **kwargs):
        """Perform the real download while simulating an API that omits its status callback."""
        kwargs.pop("download_status_callback")
        return download(*args, **kwargs)

    with (
        DownloadFixture(objects) as server,
        patch.object(benchmark_downloads, api_name, side_effect=without_status),
        pytest.raises(RuntimeError, match="^Benchmark has no final metrics$"),
    ):
        measure(server, objects, DownloadOptions(), tmp_path)

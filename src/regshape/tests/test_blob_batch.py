"""Public batch/status contracts, no-effect preflight and borrowed-client compatibility.

Synthetic streams exercise faults; ephemeral loopback servers prove physical
request caps/accounting without relying on throughput or scheduler timing.
"""

import errno
import os
import threading
from concurrent.futures import Future
from dataclasses import FrozenInstanceError, asdict, fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from regshape.libs.artifacts import DownloadStats as ArtifactStats
from regshape.libs.artifacts import DownloadStatus as ArtifactStatus
from regshape.libs.artifacts import pull_artifact
from regshape.libs.blobs import (
    BlobBatchResult,
    BlobDownload,
    DownloadOptions,
    DownloadStats,
    DownloadStatus,
    get_blob,
    get_blobs,
)
from regshape.libs.blobs.batch import _status_observer
from regshape.libs.errors import BlobError
from regshape.libs.progress import TransferPhase
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport._coordinator import (
    _Observation, _Outcome, _Stats, _Status, _Telemetry, _Worker,
)
from regshape.libs.models.blob import BlobInfo
from regshape.libs.transport.middleware import RetryConfig
from regshape.tests.artifact_fixtures import Registry, StreamResponse, address, document, edge
from regshape.tests.test_download_transport import server  # noqa: F401 - shared loopback fixture

_ARTIFACT = pytest.param(
    "artifact", marks=pytest.mark.skipif(os.name == "nt", reason="artifact storage requires POSIX")
)


def _client(authority="registry.example", **kwargs):
    """Borrow anonymous resolved credentials without touching the Docker store."""
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=(None, None)
    ):
        return RegistryClient(TransportConfig(authority, insecure=True, **kwargs))


def test_exports_frozen_records_and_original_result_schema():
    """SDK types are shared by both domains; existing BlobInfo fields stay unchanged."""
    assert ArtifactStats is DownloadStats and ArtifactStatus is DownloadStatus
    request = BlobDownload(address(b"one"))
    assert request.chunk_size == 65_536 and request.output_path is None
    stats = DownloadStats(bytes_received=3, bytes_accepted=2)
    status = DownloadStatus("id", request.digest, "strategy", "full", None, None, 1, stats)
    result = BlobBatchResult((), stats)
    for record in (request, stats, status, result):
        with pytest.raises(FrozenInstanceError):
            record.extra = True
    assert len(fields(stats)) == 11
    assert set(asdict(result)) == {"blobs", "stats"}


@pytest.mark.parametrize("changes", [
    {"request_count": True}, {"retry_count": -1}, {"bytes_received": 1.5},
    {"bytes_accepted": -1}, {"active_requests_peak": False},
    {"scratch_bytes_peak": -1}, {"elapsed_seconds": float("inf")},
    {"receive_seconds": float("nan")}, {"hash_seconds": -1},
    {"write_seconds": True}, {"queue_wait_seconds": "1"},
])
def test_stats_validate_actual_counters_and_durations(changes):
    """Invalid snapshots cannot masquerade as usable SDK metrics."""
    with pytest.raises(ValueError):
        DownloadStats(**changes)


def test_finite_integer_duration_does_not_overflow_float_validation():
    """Nonnegative integer seconds remain finite even beyond float conversion range."""
    assert DownloadStats(elapsed_seconds=10**400).elapsed_seconds == 10**400


@pytest.mark.parametrize("changes", [
    {"event": "arbitrary"}, {"reason": "https://untrusted.invalid/private"},
    {"strategy": "other"}, {"http_status": True}, {"http_status": 99},
    {"attempt": 0}, {"transfer_id": None}, {"digest": "invalid"},
    {"stats": {}}, {"event": "summary"}, {"scratch_root": 1},
])
def test_status_fixed_codes_and_identity_validation(changes):
    """Blob and summary identity contracts reject untrusted free-form status text."""
    status = DownloadStatus("id", address(b"x"), "strategy", "full", None, None, 1, DownloadStats())
    with pytest.raises(ValueError):
        replace(status, **changes)


def test_typed_etag_fallback_and_verified_completion_bridge():
    """The public bridge retains ETag reason, logical attempt and reset byte semantics."""
    events = []
    observe = _status_observer(events.append)
    assert observe is not None
    for event, accepted in (("fallback", 0), ("completed", 4)):
        observe(_Observation(
            "stable", address(b"blob"), 2, _Status(event, "full", "etag_changed"),
            _Stats(bytes_received=9, bytes_accepted=accepted),
        ))
    assert [event.reason for event in events] == ["etag_changed", "etag_changed"]
    assert [event.stats.bytes_accepted for event in events] == [0, 4]
    assert all(event.stats.bytes_received == 9 and event.attempt == 2 for event in events)
    assert events[0].stats is not events[1].stats


@pytest.mark.parametrize("changes,error", [
    ({"digest": "md5:bad"}, BlobError), ({"total_size": True}, ValueError),
    ({"chunk_size": 0}, ValueError), ({"chunk_size": False}, ValueError),
    ({"max_bytes": 0}, ValueError), ({"output_path": ""}, ValueError),
    ({"output_dir_fd": True}, ValueError), ({"output_path": b"bytes"}, ValueError),
    ({"max_bytes": 1, "total_size": 2}, BlobError),
])
def test_all_entries_preflight_before_auth_pool_and_output(tmp_path, changes, error):
    """An invalid trailing request cannot start an earlier valid transfer."""
    first = BlobDownload(address(b"first"), str(tmp_path / "first"))
    second = replace(BlobDownload(address(b"second")), **changes)
    with (
        patch("regshape.libs.transport.download._DownloadAuth") as auth,
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        patch("regshape.libs.transport.client._send_http_request") as send,
        pytest.raises(error),
    ):
        get_blobs(_client(), "repo", [first, second])
    auth.assert_not_called()
    pool.assert_not_called()
    send.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_duplicate_digest_and_nonrequest_inputs_are_rejected():
    """Verify-only entries do not collide, but duplicate content addresses do."""
    request = BlobDownload(address(b"one"))
    with (
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        pytest.raises(ValueError, match="distinct digests"),
    ):
        get_blobs(_client(), "repo", [request, request])
    pool.assert_not_called()
    with pytest.raises(ValueError, match="BlobDownload"):
        get_blobs(_client(), "repo", [object()])


@pytest.mark.parametrize("kind", [
    "relative", "hardlink",
    pytest.param("symlink", marks=pytest.mark.skipif(os.name == "nt", reason="symlink privilege")),
    pytest.param("parent-alias", marks=pytest.mark.skipif(os.name == "nt", reason="symlink privilege")),
])
def test_colliding_sinks_are_rejected_before_overwrite(tmp_path, kind):
    """Canonical paths, symlink parents and existing file identities are all checked."""
    first = tmp_path / "first"
    first.write_bytes(b"untouched")
    if kind == "relative":
        second = tmp_path / "." / "first"
    elif kind == "symlink":
        second = tmp_path / "second"
        second.symlink_to(first)
    elif kind == "parent-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        second = alias / "first"
    else:
        second = tmp_path / "second"
        os.link(first, second)
    with (
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        pytest.raises(ValueError, match="distinct output"),
    ):
        get_blobs(_client(), "repo", [
            BlobDownload(address(b"a"), str(first)),
            BlobDownload(address(b"b"), str(second)),
        ])
    pool.assert_not_called()
    assert first.read_bytes() == b"untouched"


def test_missing_sink_parent_raises_blob_error_before_effects(tmp_path):
    """A later inaccessible sink cannot truncate an earlier output or start acquisition."""
    first = tmp_path / "first"
    first.write_bytes(b"untouched")
    with (
        patch("regshape.libs.transport.download._DownloadAuth") as auth,
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        patch("regshape.libs.transport.client._send_http_request") as send,
        pytest.raises(BlobError, match="Cannot write to blob output path") as caught,
    ):
        get_blobs(_client(), "repo", [
            BlobDownload(address(b"one"), str(first)),
            BlobDownload(address(b"two"), str(tmp_path / "missing" / "blob")),
        ])
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    auth.assert_not_called()
    pool.assert_not_called()
    send.assert_not_called()
    assert first.read_bytes() == b"untouched"
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("target", ["parent", "file"])
def test_sink_stat_permission_error_preserves_cause_before_effects(tmp_path, target):
    """Both parent and target stat failures use the singular output-error taxonomy."""
    output = tmp_path / "blob"
    denied = tmp_path if target == "parent" else output
    error = PermissionError(errno.EACCES, "permission denied")
    stat = type(output).stat if target == "parent" else os.stat
    method = "pathlib.Path.stat" if target == "parent" else "regshape.libs.blobs.batch.os.stat"

    def inspect(path, *args, **kwargs):
        """Inject a permission failure at exactly the selected preflight boundary."""
        if path == denied and (target == "parent" or "dir_fd" in kwargs):
            raise error
        return stat(path, *args, **kwargs)

    with (
        patch(method, autospec=True, side_effect=inspect),
        patch("regshape.libs.transport.download._DownloadAuth") as auth,
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        patch("regshape.libs.transport.client._send_http_request") as send,
        pytest.raises(BlobError, match="Cannot write to blob output path") as caught,
    ):
        get_blobs(_client(), "repo", [BlobDownload(address(b"x"), str(output))])
    assert caught.value.__cause__ is error
    auth.assert_not_called()
    pool.assert_not_called()
    send.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.stat not in os.supports_dir_fd, reason="directory handles unavailable")
def test_closed_sink_directory_handle_raises_blob_error_before_effects(tmp_path):
    """A closed borrowed directory handle fails preflight with the original EBADF cause."""
    client = _client()
    directory = os.open(tmp_path, os.O_RDONLY)
    os.close(directory)
    with (
        patch("regshape.libs.transport.download._DownloadAuth") as auth,
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        patch("regshape.libs.transport.client._send_http_request") as send,
        pytest.raises(BlobError, match="Cannot write to blob output path") as caught,
    ):
        get_blobs(client, "repo", [
            BlobDownload(address(b"x"), "blob", max_bytes=1, output_dir_fd=directory),
        ])
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.EBADF
    auth.assert_not_called()
    pool.assert_not_called()
    send.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.stat not in os.supports_dir_fd, reason="directory handles unavailable")
def test_handle_relative_and_ordinary_sink_collide(tmp_path):
    """A borrowed directory handle and an absolute filename share the same namespace."""
    directory = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="distinct output"):
            get_blobs(_client(), "repo", [
                BlobDownload(address(b"a"), str(tmp_path / "new")),
                BlobDownload(address(b"b"), "new", max_bytes=10, output_dir_fd=directory),
            ])
        assert os.fstat(directory)
    finally:
        os.close(directory)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("names", [("Blob", "blob"), ("\u00e9", "e\u0301")])
def test_nonexistent_portable_filename_aliases_collide(tmp_path, names):
    """Case/Unicode-equivalent names are conservatively unsafe even before files exist."""
    with (
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        pytest.raises(ValueError, match="distinct output"),
    ):
        get_blobs(_client(), "repo", [
            BlobDownload(address(b"one"), str(tmp_path / names[0])),
            BlobDownload(address(b"two"), str(tmp_path / names[1])),
        ])
    pool.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("count", [99_999, 100_000, 100_001])
def test_materialization_exact_request_limit(count):
    """Consume no more than the first over-limit entry, before acquisition.

    Cheap preflight records avoid allocating 100,000 worker sinks; the real
    per-entry and sink validators are exercised independently above.
    """
    seen = []
    stop = RuntimeError("preflight complete")

    def requests():
        """Yield distinct valid input records and reveal the exact consumption bound."""
        for index in range(count + 3):
            if index == count:
                return
            seen.append(index)
            yield BlobDownload(f"sha256:{index:064x}")

    def prepare(repo, request, policy):
        """Stand in only for individual validation/resource-free record allocation."""
        return SimpleNamespace(digest=request.digest)

    with (
        patch("regshape.libs.blobs.batch._full_blob", side_effect=prepare),
        patch("regshape.libs.blobs.batch._sinks", side_effect=stop) as sinks,
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        pytest.raises((ValueError, RuntimeError)) as caught,
    ):
        get_blobs(_client(), "repo", requests())
    if count <= 100_000:
        assert caught.value is stop
        assert len(sinks.call_args.args[0]) == count
    else:
        assert isinstance(caught.value, ValueError)
        sinks.assert_not_called()
    assert len(seen) == count
    pool.assert_not_called()


def test_empty_batch_has_one_real_zero_summary_without_requests():
    """An empty valid iterable returns the declared tuple/stats shape, never legacy metrics."""
    statuses = []
    with patch("regshape.libs.transport.client._send_http_request") as send:
        result = get_blobs(_client(), "repo", iter(()), download_status_callback=statuses.append)
    send.assert_not_called()
    assert result.blobs == ()
    assert len(statuses) == 1 and statuses[0].event == "summary"
    assert statuses[0].transfer_id is statuses[0].digest is statuses[0].attempt is None
    assert result.stats is statuses[0].stats
    assert result.stats.request_count == result.stats.bytes_received == 0


@pytest.mark.parametrize("operation", ["singular", "batch", _ARTIFACT])
@pytest.mark.parametrize("observer", ["progress", "status"])
def test_invalid_observers_rejected_before_pool(tmp_path, operation, observer):
    """A noncallable observer cannot allocate resources or leave output."""
    kwargs = {"progress_callback" if observer == "progress" else "download_status_callback": 1}
    with (
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        pytest.raises((ValueError, TypeError)),
    ):
        if operation == "singular":
            get_blob(_client(), "repo", address(b"x"), **kwargs)
        elif operation == "batch":
            get_blobs(_client(), "repo", [BlobDownload(address(b"x"))], **kwargs)
        else:
            pull_artifact(_client(), "repo", "latest", tmp_path / "layout", **kwargs)
    pool.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("observer,phase", [
    ("progress", "transferring"), ("progress", "completed"),
    ("status", "strategy"), ("status", "completed"), ("status", "summary"),
])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError, KeyboardInterrupt])
def test_observer_error_identity_and_no_further_calls(observer, phase, error_type):
    """Observer exceptions, including process control and OSError, are never rewrapped."""
    error = error_type("synthetic observer fault")
    calls = []

    def observe(event):
        """Fail at one supported lifecycle point and reject recursive notifications."""
        assert error not in calls
        calls.append(event)
        actual = event.event if observer == "status" else event.phase.value
        if actual == phase:
            calls.append(error)
            raise error

    kwargs = {"download_status_callback" if observer == "status" else "progress_callback": observe}
    with (
        patch.object(RegistryClient, "get", side_effect=lambda *a, **k: StreamResponse(b"data")),
        pytest.raises(error_type) as caught,
    ):
        get_blobs(_client(), "repo", [BlobDownload(address(b"data"))], **kwargs)
    assert caught.value is error
    assert calls[-1] is error


@pytest.mark.parametrize("operation", ["singular", "batch", _ARTIFACT])
@pytest.mark.parametrize("fail_on", ["progress", "status"])
def test_same_observer_shared_across_surfaces_never_called_after_failure(tmp_path, operation, fail_on):
    """Disabling a failed original callable applies across its distinct adapter wrappers."""
    error = OSError("shared observer fault")
    calls = []
    failed_at = []
    payload = b"shared callback"
    registry = Registry(document(config=edge(payload)), {address(payload): payload})

    def observe(event):
        """Accept both schemas but fail on one, with no later invocation permitted."""
        calls.append(event)
        if (
            fail_on == "progress" and hasattr(event, "phase")
            or fail_on == "status" and isinstance(event, DownloadStatus) and event.event == "strategy"
        ):
            failed_at.append(len(calls))
            raise error

    with (
        patch.object(RegistryClient, "get", side_effect=registry.get),
        pytest.raises(OSError) as caught,
    ):
        kwargs = {"progress_callback": observe, "download_status_callback": observe}
        if operation == "singular":
            get_blob(_client(), "repo", address(payload), **kwargs)
        elif operation == "batch":
            get_blobs(_client(), "repo", [BlobDownload(address(payload))], **kwargs)
        else:
            pull_artifact(_client(), "repo", "moving", tmp_path / "layout", **kwargs)
    assert caught.value is error
    assert failed_at == [len(calls)]


def test_strategy_observer_runs_before_any_job_dispatch():
    """Synchronous submission makes premature dispatch deterministic, without payload I/O."""
    submitted = []
    digest = address(b"blob")
    statuses = []

    def submit(function, *args):
        """Return a stopped synthetic job so only dispatch/observer ordering is exercised."""
        submitted.append(True)
        future = Future()
        future.set_result(_Outcome(
            BlobInfo(digest, "application/octet-stream", 4), None, _Stats(),
            _Telemetry((0, 0, 0, 0, 0), 0.0, (), ()),
        ))
        return future

    def observe(event):
        """Require strategy approval before executor submission, not merely before progress."""
        if event.event == "strategy":
            assert submitted == []
        statuses.append(event)

    with patch("regshape.libs.transport._coordinator.ThreadPoolExecutor.submit", side_effect=submit):
        get_blob(_client(), "repo", digest, download_status_callback=observe)
    assert submitted == [True]
    assert [event.event for event in statuses] == ["strategy", "completed", "summary"]


@pytest.mark.skipif(os.name == "nt", reason="artifact storage requires POSIX")
def test_artifact_phase_failure_disables_shared_status_observer(tmp_path):
    """A phase observer shared with status is not retried through the final summary."""
    from regshape.libs.artifacts import ArtifactPhase

    error = RuntimeError("phase callback fault")
    calls = []
    registry = Registry(document(children=[]))

    def observe(event):
        """Fail at publication rather than at an earlier blob status."""
        calls.append(event)
        if isinstance(event, ArtifactPhase):
            raise error

    with (
        patch.object(RegistryClient, "get", side_effect=registry.get),
        pytest.raises(RuntimeError) as caught,
    ):
        pull_artifact(
            _client(), "repo", "moving", tmp_path / "layout",
            phase_callback=observe, download_status_callback=observe,
        )
    assert caught.value is error
    assert len(calls) == 1
    assert not (tmp_path / "layout").exists()


def test_fail_fast_retains_prior_success_and_original_domain_error(tmp_path):
    """Sequential failure returns no result; earlier verified standalone files survive."""
    good, bad = tmp_path / "good", tmp_path / "bad"
    statuses = []
    error = BlobError("synthetic domain failure")
    with (
        patch.object(RegistryClient, "get", side_effect=[StreamResponse(b"good"), error]) as get,
        pytest.raises(BlobError) as caught,
    ):
        get_blobs(
            _client(), "repo", [
                BlobDownload(address(b"good"), str(good)),
                BlobDownload(address(b"bad"), str(bad)),
                BlobDownload(address(b"never"), str(tmp_path / "never")),
            ], download_options=DownloadOptions.sequential(),
            download_status_callback=statuses.append,
        )
    assert caught.value is error and get.call_count == 2
    assert good.read_bytes() == b"good" and not bad.exists()
    assert [event.event for event in statuses] == [
        "strategy", "completed", "strategy", "failed", "summary",
    ]
    assert not (tmp_path / "never").exists()


def test_public_batch_cap_order_physical_stats_and_caller_thread(tmp_path):
    """Two real simultaneous streams finish out of order under one batch-wide C cap."""
    payloads = [b"first", b"second", b"third"]
    content = {address(value): value for value in payloads}
    barrier = threading.Barrier(2)
    release_first = threading.Event()
    lock = threading.Lock()
    active = peak = started = 0

    class Handler(BaseHTTPRequestHandler):
        """Serve independent blob bodies with deterministic overlap and no auth."""

        def log_message(self, *args):
            """Keep loopback diagnostics out of assertion evidence."""

        def do_GET(self):
            """Send headers before blocking so anonymous single-flight auth can finish."""
            nonlocal active, peak, started
            body = content[self.path.rsplit("/", 1)[-1]]
            with lock:
                active += 1
                peak = max(peak, active)
                started += 1
                number = started
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if number <= 2:
                    barrier.wait(timeout=5)
                if body == payloads[0]:
                    assert release_first.wait(5)
                self.wfile.write(body)
                self.wfile.flush()
            finally:
                with lock:
                    active -= 1

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    caller = threading.get_ident()
    statuses, progress, threads = [], [], []

    def status(event):
        """Release the first stream only after the second is verified on the caller."""
        threads.append(threading.get_ident())
        statuses.append(event)
        if event.event == "completed" and event.digest == address(payloads[1]):
            release_first.set()

    def observe(event):
        """Track the original immutable progress type and stable transfer identity."""
        threads.append(threading.get_ident())
        progress.append(event)

    try:
        result = get_blobs(
            _client(f"127.0.0.1:{httpd.server_port}"), "repo",
            (BlobDownload(address(value), str(tmp_path / str(index)))
             for index, value in enumerate(payloads)),
            download_options=DownloadOptions(concurrency=2),
            progress_callback=observe, download_status_callback=status,
        )
    finally:
        release_first.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert 2 == peak == result.stats.active_requests_peak
    assert result.stats.request_count == started == 3
    assert result.stats.bytes_received == result.stats.bytes_accepted == sum(map(len, payloads))
    assert result.stats.scratch_bytes_peak == 0
    assert result.stats.hash_seconds > 0 and result.stats.write_seconds > 0
    assert result.stats.receive_seconds > 0 and result.stats.elapsed_seconds > 0
    assert [blob.digest for blob in result.blobs] == list(content)
    assert [event.digest for event in statuses if event.event == "completed"][0] == address(payloads[1])
    assert statuses[-1].stats is result.stats
    assert set(threads) == {caller}
    for index, value in enumerate(payloads):
        assert (tmp_path / str(index)).read_bytes() == value
        events = [event for event in statuses if event.digest == address(value)]
        row = [event for event in progress if event.digest == address(value)]
        assert events[0].event == "strategy" and events[-1].event == "completed"
        assert {event.transfer_id for event in events + row} == {row[0].transfer_id}
        assert events[-1].stats.request_count == 1
        assert events[-1].stats.bytes_accepted == len(value)
        assert row[-1].phase == TransferPhase.COMPLETED
        assert set(result.blobs[index].to_dict()) == {"digest", "content_type", "size"}


@pytest.mark.parametrize("operation", ["singular", "batch", _ARTIFACT])
@pytest.mark.parametrize("options,config,budget,status_code", [
    (DownloadOptions(), {}, 3, 429),
    (DownloadOptions(max_retries=0), {}, 0, 429),
    (DownloadOptions(), {"enable_retries": True,
                         "retry_config": RetryConfig(max_retries=1, status_codes=(503,))}, 1, 503),
    (DownloadOptions(), {"enable_retries": False,
                         "retry_config": RetryConfig(max_retries=2)}, 0, 503),
])
def test_modern_retry_resolution_and_physical_counts(server, tmp_path, operation, options, config, budget, status_code):  # noqa: F811 - shared fixture
    """Defaults, explicit zero and caller policies apply to all modern entry points."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = budget
    state.failure_status = status_code
    statuses, progress = [], []
    client = _client(authority, **config)
    with patch.object(_Worker, "wait"), patch(
        "regshape.libs.transport.middleware.RetryMiddleware._wait_backoff"
    ):
        if operation == "singular":
            get_blob(client, "repo", address(state.payload), download_options=options,
                     download_status_callback=statuses.append)
        elif operation == "batch":
            get_blobs(client, "repo", [BlobDownload(address(state.payload))],
                      download_options=options, download_status_callback=statuses.append)
        else:
            pull_artifact(client, "repo", "latest", tmp_path / "layout",
                          download_options=options, download_status_callback=statuses.append,
                          progress_callback=progress.append)
    summary = statuses[-1]
    assert summary.event == "summary"
    assert summary.stats.request_count == state.requests == budget + (2 if operation == "artifact" else 1)
    assert summary.stats.retry_count == budget
    assert summary.stats.bytes_accepted == len(state.payload)
    assert summary.stats.bytes_received == len(state.payload)
    assert client.last_response is None
    if operation != "artifact":
        assert len([event for event in statuses if event.event == "retry"]) == budget
    else:
        identity = next(event.transfer_id for event in progress if event.digest == address(state.payload))
        assert {event.transfer_id for event in statuses if event.digest == address(state.payload)} == {identity}


@pytest.mark.parametrize("operation", ["singular", "batch"])
@pytest.mark.parametrize("budget", [0, 3])
def test_retry_budget_exact_exhaustion_preserves_failure(server, operation, budget):  # noqa: F811 - shared fixture
    """One initial request plus exactly N retries cannot produce a completed result."""
    authority, state = server
    state.mode = "anonymous"
    state.failures = 10
    statuses = []
    with (
        patch.object(_Worker, "wait"),
        pytest.raises(BlobError),
    ):
        kwargs = {
            "download_options": DownloadOptions(max_retries=budget),
            "download_status_callback": statuses.append,
        }
        if operation == "singular":
            get_blob(_client(authority), "repo", address(state.payload), **kwargs)
        else:
            get_blobs(_client(authority), "repo", [BlobDownload(address(state.payload))], **kwargs)
    assert state.requests == budget + 1
    assert statuses[-1].stats.request_count == budget + 1
    assert statuses[-1].stats.retry_count == budget
    assert statuses[-2].event == "failed" and statuses[-1].event == "summary"
    assert not any(event.event == "completed" for event in statuses)


def test_shared_auth_exchange_and_batch_counters(server):  # noqa: F811 - shared fixture
    """Distinct SHA-256/SHA-512 requests share one repository token and physical budget."""
    authority, state = server
    statuses = []
    result = get_blobs(
        _client(authority), "repo",
        [BlobDownload(address(state.payload, algorithm)) for algorithm in ("sha256", "sha512")],
        download_status_callback=statuses.append,
    )
    assert state.unauthorized == len(state.token_calls) == 1
    assert result.stats.request_count == state.requests + len(state.token_calls) == 4
    completed = [event for event in statuses if event.event == "completed"]
    assert sum(event.stats.request_count for event in completed) == result.stats.request_count
    assert result.stats.bytes_accepted == result.stats.bytes_received == 2 * len(state.payload)
    assert all(event.stats.active_requests_peak == 1 for event in completed)


@pytest.mark.parametrize("operation", ["singular", _ARTIFACT])
def test_explicit_legacy_retains_original_retry_budget(server, tmp_path, operation):  # noqa: F811 - shared fixture
    """Legacy retries remain the enabled caller's policy, never the modern default."""
    authority, state = server
    state.mode = "anonymous"
    state.failure_status = 503
    state.failures = 2
    client = _client(authority, enable_retries=True, retry_config=RetryConfig(max_retries=2))
    with (
        patch("regshape.libs.transport.middleware.RetryMiddleware._wait_backoff"),
        patch("regshape.libs.transport.download._DownloadAuth", side_effect=AssertionError("new auth")),
        patch("regshape.libs.transport.download._DownloadAdapter", side_effect=AssertionError("new pool")),
        patch("regshape.libs.blobs.batch._Coordinator", side_effect=AssertionError("new coordinator")),
    ):
        if operation == "singular":
            get_blob(client, "repo", address(state.payload), download_options=DownloadOptions.legacy())
        else:
            pull_artifact(client, "repo", "latest", tmp_path / "layout",
                          download_options=DownloadOptions.legacy())
    assert state.requests == 2 + (2 if operation == "artifact" else 1)
    assert client.last_response is not None


@pytest.mark.parametrize("operation", ["singular", _ARTIFACT])
@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_and_omitted_custom_clients_bypass_all_new_resources(tmp_path, caplog, operation, explicit):
    """True legacy keeps borrowed custom behavior, with no fabricated metrics or resources."""
    payload = b"legacy"
    registry = Registry(document(config=edge(payload)), {address(payload): payload})
    options = {"download_options": DownloadOptions.legacy()} if explicit else {}
    with (
        patch("regshape.libs.transport.download._DownloadAuth", side_effect=AssertionError("auth")),
        patch("regshape.libs.transport.download._DownloadAdapter", side_effect=AssertionError("pool")),
        patch("regshape.libs.blobs.batch._Coordinator", side_effect=AssertionError("coordinator")),
    ):
        if operation == "singular":
            result = get_blob(registry, "repo", address(payload), **options)
            assert result.digest == address(payload)
        else:
            result = pull_artifact(registry, "repo", "moving", tmp_path / "layout", **options)
            assert result.blobs == 1
    assert caplog.text.count("original serial transport") == (0 if explicit else 1)
    assert registry.calls


@pytest.mark.parametrize("operation", ["singular", _ARTIFACT, "batch"])
@pytest.mark.parametrize("options", [None, DownloadOptions(), DownloadOptions.legacy()])
def test_new_contracts_reject_unsupported_transport_before_effects(tmp_path, operation, options):
    """Batch/status never manufacture legacy counters or silently drop custom behavior."""
    client = Mock()
    client.config.registry = "registry.example"
    with (
        patch("regshape.libs.transport.download._DownloadAdapter") as pool,
        pytest.raises(ValueError),
    ):
        kwargs = {"download_options": options, "download_status_callback": lambda event: None}
        if operation == "singular":
            get_blob(client, "repo", address(b"x"), str(tmp_path / "blob"), **kwargs)
        elif operation == "artifact":
            pull_artifact(client, "repo", "latest", tmp_path / "layout", **kwargs)
        else:
            get_blobs(client, "repo", [BlobDownload(address(b"x"))], **kwargs)
    pool.assert_not_called()
    client.get.assert_not_called()
    assert list(tmp_path.iterdir()) == []

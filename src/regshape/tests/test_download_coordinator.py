"""Deterministic bounded scheduling, caller-only observation and telemetry contracts.

Tasks use barriers/events rather than throughput timing assumptions. All file
cleanup assertions run after their producers have stopped.
"""

import io
import logging
import threading
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest

from regshape.libs._logging import emit_log
from regshape.libs.artifacts.models import ArtifactProgress
from regshape.libs.blobs import DownloadOptions
from regshape.libs.decorators import (
    TelemetryConfig,
    _telemetry_config,
    get_telemetry_config,
)
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.progress import TransferDirection, TransferPhase, TransferProgress
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport._coordinator import (
    _Coordinator,
    _DownloadCancelled,
    _Job,
    _Status,
    _Telemetry,
    _Text,
)
from regshape.libs.transport.download import DownloadTransport
from regshape.tests.artifact_fixtures import address


@pytest.fixture
def client():
    """Borrow a base client with synthetic anonymous credentials, never Docker config."""
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=(None, None)
    ):
        return RegistryClient(TransportConfig("registry.example"))


def _job(
    identity,
    run,
    *,
    cleanup=None,
    scratch=0,
    attempt=1,
    reason=None,
    terminal=False,
    validate_result=None,
):
    """Build an isolated job with optional caller validation and owned-scratch cleanup."""
    return _Job(
        identity,
        address(identity.encode()),
        run,
        cleanup or (lambda accepted: None),
        scratch_bytes=scratch,
        attempt=attempt,
        reason=reason,
        terminal=terminal,
        validate_result=validate_result,
    )


def _progress(worker, count=0):
    """Queue an existing public event using the job's stable identity and attempt."""
    worker.emit(
        TransferProgress(
            worker.job.transfer_id,
            worker.job.digest,
            TransferDirection.DOWNLOAD,
            TransferPhase.TRANSFERRING,
            count,
            None,
            worker.job.attempt,
        )
    )


def test_round_robin_and_global_cap(client):
    """A third ready blob gets a turn before an early blob's next range."""
    options = DownloadOptions(concurrency=2, ranges_per_blob=1)
    barrier = threading.Barrier(2)
    starts = []
    lock = threading.Lock()

    def task(worker):
        """Record actual overlap and hold the first pair until both started."""
        with lock:
            starts.append(worker.job.transfer_id)
            first = len(starts) <= 2
        if first:
            barrier.wait(timeout=5)
        return worker.job.transfer_id

    with DownloadTransport(client, options) as root:
        with _Coordinator(root._download_context) as coordinator:
            coordinator.run(
                [
                    [_job("a", task), _job("a", task)],
                    [_job("b", task), _job("b", task)],
                    [_job("c", task)],
                ],
                on_result=lambda value: None,
            )
            assert set(starts[:2]) == {"a", "b"}
            assert starts[2] == "c"
            assert coordinator.jobs_peak == 2


def test_window_retains_out_of_order_results_and_scratch(client):
    """A stalled earliest range prevents unbounded later reservations, not unrelated work."""
    options = DownloadOptions(concurrency=3, ranges_per_blob=2, range_size=65_536)
    first_release = threading.Event()
    later_completed = threading.Event()
    generated = []
    consumed = []

    def first(worker):
        """Stall the prefix while a later part and the other blob run."""
        assert first_release.wait(5)
        return 0

    def later(worker):
        """Complete the second part without authorizing any subsequent part."""
        later_completed.set()
        return 1

    def ranges():
        """Generate only the bounded consecutive window until the prefix commits."""
        for index in range(20):
            generated.append(index)
            run = (
                first
                if index == 0
                else later if index == 1 else lambda worker, i=index: i
            )
            yield _job("a", run, scratch=65_536)

    def unrelated(worker):
        """Verify the blocked source still owns precisely its first two reservations."""
        assert later_completed.wait(5)
        assert generated == [0, 1]
        first_release.set()
        return "other"

    with DownloadTransport(client, options) as root:
        with _Coordinator(root._download_context) as coordinator:
            coordinator.run(
                [ranges(), [_job("b", unrelated)]], on_result=consumed.append
            )
            assert [value for value in consumed if isinstance(value, int)] == list(
                range(20)
            )
            assert coordinator.scratch_peak <= 3 * options.range_size
            assert coordinator.scratch_bytes == 0
            assert coordinator.jobs_peak <= options.concurrency


def test_queue_full_cancellation_joins_before_cleanup(client):
    """Cancelling a full producer queue releases backpressure before caller cleanup."""
    callback_entered = threading.Event()
    queue_full = threading.Event()
    release_callback = threading.Event()
    task_done = threading.Event()
    cleanup_calls = []
    thread_id = threading.get_ident()

    def task(worker):
        """Fill all 4C queue slots while the invoking thread's observer is held."""
        try:
            _progress(worker)
            assert callback_entered.wait(5)
            for _ in range(100):
                worker.emit(_Text("output", "line"))
                if worker.owner.queue.full():
                    queue_full.set()
        finally:
            task_done.set()

    def callback(event):
        """Block just long enough for the independent controller to observe a full queue."""
        assert threading.get_ident() == thread_id
        callback_entered.set()
        assert release_callback.wait(5)

    def cleanup(accepted):
        """Require that cleanup cannot race any active producer."""
        assert task_done.is_set()
        assert threading.get_ident() == thread_id
        cleanup_calls.append(accepted)

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(
            root._download_context, progress_callback=callback
        ) as coordinator:

            def cancel():
                """Record cancellation while producers are still backpressured."""
                try:
                    assert queue_full.wait(5)
                    assert coordinator.queue.qsize() == 4
                    coordinator.cancel()
                finally:
                    release_callback.set()

            controller = threading.Thread(target=cancel)
            controller.start()
            try:
                with pytest.raises(_DownloadCancelled):
                    coordinator.run(
                        [[_job("a", task, cleanup=cleanup)]],
                        on_result=lambda value: None,
                    )
            finally:
                release_callback.set()
                coordinator.cancel()
                controller.join(timeout=5)
            assert not controller.is_alive()
            assert cleanup_calls == [False]
            assert coordinator.queue.empty()


@pytest.mark.parametrize(
    "observer_kind", ["progress", "status", "consumer", "output", "log"]
)
def test_observer_error_identity_and_no_reuse(client, observer_kind):
    """Public observers and diagnostic/result sinks fail only on the caller, exactly once."""
    error = RuntimeError("synthetic observer")
    calls = []
    caller = threading.get_ident()

    def fail(value):
        """Raise the same exception object, recording caller-thread invocation."""
        calls.append(threading.get_ident())
        raise error

    class Sink(io.StringIO):
        """Writable destination that fails on its first actual caller write."""

        def write(self, text):
            """Delegate to the identity-preserving failure observer."""
            fail(text)

    parent = TelemetryConfig(
        output=Sink() if observer_kind == "output" else io.StringIO(),
        log_file=Sink() if observer_kind == "log" else None,
    )
    token = _telemetry_config.set(parent)
    try:

        def task(worker):
            """Publish many records so post-error cleanup would expose observer reuse."""
            for _ in range(30):
                if observer_kind in ("output", "log"):
                    worker.emit(_Text(observer_kind, "text"))
                else:
                    _progress(worker)
            return 1

        with DownloadTransport(client, DownloadOptions.sequential()) as root:
            with pytest.raises(RuntimeError) as caught:
                with _Coordinator(
                    root._download_context,
                    progress_callback=fail if observer_kind == "progress" else None,
                    status_callback=fail if observer_kind == "status" else None,
                ) as coordinator:
                    coordinator.run(
                        [[_job("a", task)]],
                        on_result=(
                            fail if observer_kind == "consumer" else lambda value: None
                        ),
                    )
        assert caught.value is error
        assert calls == [caller]
    finally:
        _telemetry_config.reset(token)


def test_all_progress_survives_fast_worker_completion(client):
    """A completed future cannot overtake and discard its queued progress records."""
    received = []

    def task(worker):
        """Produce more snapshots than fit in the bounded queue."""
        for count in range(50):
            _progress(worker, count)
        return "verified"

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(
            root._download_context, progress_callback=received.append
        ) as coordinator:
            coordinator.run([[_job("a", task)]], on_result=lambda value: None)
    assert [
        event.bytes_completed
        for event in received
        if event.phase == TransferPhase.TRANSFERRING
    ] == list(range(50))
    assert received[-1].phase == TransferPhase.COMPLETED


def test_telemetry_isolation_and_bounded_parent_merging(client):
    """Nested worker scenarios cannot reset parent metrics or accumulate per-range timings."""
    caller = threading.get_ident()
    calls = []

    class Sink(io.StringIO):
        """Record the actual thread of every terminal/log-file write."""

        def write(self, text):
            """Retain text and thread identity only on the invoking thread."""
            calls.append(threading.get_ident())
            return super().write(text)

    parent = TelemetryConfig(
        metrics_enabled=True,
        time_methods_enabled=True,
        time_scenarios_enabled=True,
        debug_calls_enabled=True,
        output=Sink(),
        log_file=Sink(),
        method_timings=[("outer", 1.0)],
    )
    parent.metrics.record_request(201)
    token = _telemetry_config.set(parent)
    configs = []
    logger = logging.getLogger("regshape.tests.coordinator")
    old_level = logger.level
    logger.setLevel(logging.INFO)

    @track_scenario("worker scenario")
    @track_time
    def task(worker):
        """Exercise all enabled worker options, metrics and scenario timing consumption."""
        config = get_telemetry_config()
        assert config is not parent
        assert config.debug_calls_enabled and config.time_scenarios_enabled
        configs.append(config)
        config.metrics.record_request(200, bytes_received=2)
        emit_log(logger, logging.INFO, "safe worker diagnostic")
        return 1

    try:
        with DownloadTransport(
            client, DownloadOptions(concurrency=2, ranges_per_blob=2)
        ) as root:
            with _Coordinator(root._download_context) as coordinator:
                coordinator.run(
                    [
                        [_job("a", task) for _ in range(30)],
                    ],
                    on_result=lambda value: None,
                )
        assert len({id(config) for config in configs}) == 30
        assert parent.metrics.total_requests == 31
        assert parent.metrics.status_code_counts == {201: 1, 200: 30}
        assert parent.method_timings[0] == ("outer", 1.0)
        assert len(parent.method_timings) == 2
        assert set(calls) == {caller}
        assert get_telemetry_config() is parent
    finally:
        logger.setLevel(old_level)
        _telemetry_config.reset(token)


def test_deferred_logging_handler_runs_on_caller(client):
    """Built-in library logger dispatch never reaches a handler on a worker."""
    caller = threading.get_ident()
    threads = []
    logger = logging.getLogger("regshape.tests.coordinator.handler")

    class Handler(logging.Handler):
        """Observe actual logging sink execution rather than only queue production."""

        def emit(self, record):
            """Capture the sink thread without retaining a mutable worker record."""
            threads.append(threading.get_ident())

    handler = Handler()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        with DownloadTransport(client, DownloadOptions.sequential()) as root:
            with _Coordinator(root._download_context) as coordinator:
                coordinator.run(
                    [
                        [
                            _job(
                                "a",
                                lambda worker: emit_log(logger, logging.INFO, "queued"),
                            )
                        ]
                    ],
                    on_result=lambda value: None,
                )
        assert threads == [caller]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_oversized_diagnostic_rejected(client):
    """A diagnostic larger than 64 KiB fails explicitly without unbounded buffering."""
    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(root._download_context) as coordinator:
            with pytest.raises(ValueError, match="64 KiB"):
                coordinator.run(
                    [
                        [
                            _job(
                                "a",
                                lambda worker: worker.emit(
                                    _Text("output", "x" * 65_536)
                                ),
                            )
                        ]
                    ],
                    on_result=lambda value: None,
                )
            assert coordinator.queue.empty()


def test_reset_preserves_received_and_changes_attempt(client):
    """Drained ETag fallback resets accepted bytes, preserving row identity and cumulative receive."""
    statuses = []
    resets = []

    def receive(worker):
        """Count consumed bytes in an isolated task without a payload bridge."""
        worker.stats = replace(worker.stats, bytes_received=3, bytes_accepted=3)
        return 3

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(
            root._download_context, status_callback=statuses.append
        ) as coordinator:
            coordinator.run([[_job("a", receive)]], on_result=lambda value: None)
            coordinator.reset("a", lambda: resets.append(True))
            assert coordinator.snapshot("a").bytes_received == 3
            assert coordinator.snapshot("a").bytes_accepted == 0
            coordinator.run(
                [
                    [
                        _job(
                            "a",
                            receive,
                            attempt=2,
                            reason="etag_changed",
                        )
                    ]
                ],
                on_result=lambda value: None,
            )
            assert coordinator.snapshot("a").bytes_received == 6
            assert coordinator.snapshot("a").bytes_accepted == 3
    fallback = next(event for event in statuses if event.status.event == "fallback")
    assert fallback.transfer_id == "a" and fallback.attempt == 2
    assert fallback.stats.bytes_accepted == 0
    completed = [event for event in statuses if event.status.event == "completed"][-1]
    assert completed.status.reason == "etag_changed"
    assert resets == [True]
    with pytest.raises(FrozenInstanceError):
        completed.stats.bytes_received = 0


def test_reset_queues_zero_artifact_progress_on_same_row(client):
    """The bridge also retains artifact row identity and logical attempt during fallback."""
    progress = []

    def task(worker):
        """Publish the current public artifact model, without introducing a CLI-specific row."""
        worker.emit(
            ArtifactProgress(
                worker.job.transfer_id,
                "blob",
                "transferring",
                worker.job.digest,
                "layer",
                3,
                5,
                worker.job.attempt,
                "network",
            )
        )

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(
            root._download_context, progress_callback=progress.append
        ) as coordinator:
            coordinator.run([[_job("a", task)]], on_result=lambda value: None)
            coordinator.reset("a", lambda: None)
    assert len(progress) == 3
    assert progress[1].phase == "received"
    assert progress[-1].transfer_id == "a"
    assert progress[-1].bytes_completed == 0
    assert progress[-1].attempt == 2


def test_immutable_observer_error_is_not_mutated(client):
    """Preservation never assigns traceback/cause/diagnostic fields to the original observer error."""

    class ImmutableError(Exception):
        """Exception deliberately rejecting Python-level attribute mutation."""

        def __setattr__(self, name, value):
            """Fail any attempted annotation, including traceback repair."""
            raise AssertionError("Exception mutation")

    error = ImmutableError("immutable callback")

    def observer(event):
        """Raise the same immutable object from caller-side progress delivery."""
        raise error

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with pytest.raises(ImmutableError) as caught:
            with _Coordinator(
                root._download_context, progress_callback=observer
            ) as coordinator:
                coordinator.run([[_job("a", _progress)]], on_result=lambda value: None)
    assert caught.value is error


def test_successful_blob_is_not_reclassified_after_later_failure(client):
    """Earlier successful standalone transfers remain completed when another blob fails."""
    statuses = []
    primary = RuntimeError("later job")

    def fail(worker):
        """Fail the next source after the previous source has already been consumed."""
        raise primary

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with pytest.raises(RuntimeError) as caught:
            with _Coordinator(
                root._download_context, status_callback=statuses.append
            ) as coordinator:
                coordinator.run(
                    [
                        [_job("a", lambda worker: "ok", terminal=True)],
                        [_job("b", fail)],
                    ],
                    on_result=lambda value: None,
                )
    assert caught.value is primary
    terminal_a = [
        event.status.event
        for event in statuses
        if event.transfer_id == "a"
        and event.status.event in ("completed", "failed", "cancelled")
    ]
    assert terminal_a == ["completed"]
    assert any(
        event.transfer_id == "b" and event.status.event == "failed"
        for event in statuses
    )


def test_failure_progress_and_healthy_status_after_observer_error(client):
    """A failed progress observer is disabled, while a healthy status observer receives final state."""
    statuses = []
    error = RuntimeError("progress")

    def observer(event):
        """Stop the operation on the first public progress record."""
        raise error

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with pytest.raises(RuntimeError) as caught:
            with _Coordinator(
                root._download_context,
                progress_callback=observer,
                status_callback=statuses.append,
            ) as coordinator:
                coordinator.run([[_job("a", _progress)]], on_result=lambda value: None)
    assert caught.value is error
    assert any(event.status.event == "failed" for event in statuses)
    assert statuses[-1].status.event == "summary"


def test_invalid_status_and_scratch_fail_explicitly(client):
    """Unknown reasons and over-budget reservations cannot become successful observations."""
    with pytest.raises(ValueError, match="reason"):
        _Status("fallback", reason="registry-controlled-text")
    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(root._download_context) as coordinator:
            with pytest.raises(ValueError, match="range_size"):
                coordinator.run(
                    [
                        [
                            _job(
                                "a",
                                lambda worker: None,
                                scratch=coordinator.transport.options.range_size + 1,
                            )
                        ]
                    ],
                    on_result=lambda value: None,
                )
            assert coordinator.jobs_peak == 0


def test_nested_coordinator_rejected(client):
    """One invocation cannot multiply executor pools or exceed its global bound."""
    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(root._download_context):
            with pytest.raises(RuntimeError, match="one invocation"):
                with _Coordinator(root._download_context):
                    pass


def test_slow_consumer_cannot_starve_completed_job_and_next_blob(client):
    """A refilling producer queue cannot retain an unrelated completed job's scheduling slot."""
    advance = threading.Event()
    refilled = threading.Event()
    producer_done = threading.Event()
    third_started = threading.Event()
    allow_small_finish = threading.Event()
    third_started_early = []

    class SlowSink(io.StringIO):
        """Fence unrelated completion and scheduling while the producer keeps refilling."""

        def __init__(self):
            """Count consumed writes without sharing state with other observer instances."""
            super().__init__()
            self.writes = 0

        def write(self, text):
            """Prove the next scheduler turn can run the third job before draining the queue."""
            self.writes += 1
            if self.writes == 1:
                pending = next(
                    item
                    for item in coordinator.pending.values()
                    if item.job.transfer_id == "b"
                )
                allow_small_finish.set()
                pending.future.result(timeout=5)
            elif self.writes == 2:
                assert third_started.wait(5)
            if not producer_done.is_set():
                refilled.clear()
                advance.set()
                assert refilled.wait(5)
            return len(text)

    def producer(worker):
        """Refill faster than the synchronized consumer until all forty bounded records are sent."""
        for index in range(40):
            worker.emit(_Text("output", "line"))
            if index >= 4:
                if index == 39:
                    producer_done.set()
                refilled.set()
                if not producer_done.is_set():
                    assert advance.wait(5)
                    advance.clear()
        return "large"

    def small(worker):
        """Remain pending until the first observation explicitly permits completion."""
        assert allow_small_finish.wait(5)
        return "small"

    def third(worker):
        """Prove a ready blob starts before the other producer finishes all observations."""
        third_started_early.append(not producer_done.is_set())
        third_started.set()
        return "third"

    token = _telemetry_config.set(TelemetryConfig(output=SlowSink()))
    try:
        with DownloadTransport(client, DownloadOptions(concurrency=2)) as root:
            with _Coordinator(root._download_context) as coordinator:
                coordinator.run(
                    [
                        [_job("a", producer, terminal=True)],
                        [_job("b", small, terminal=True)],
                        [_job("c", third, terminal=True)],
                    ],
                    on_result=lambda value: None,
                )
        assert third_started_early == [True]
    finally:
        advance.set()
        allow_small_finish.set()
        _telemetry_config.reset(token)


@pytest.mark.parametrize("worker_failure", [False, True])
def test_telemetry_merge_failure_still_cleans_every_owned_job(client, worker_failure):
    """A bounded diagnostic failure cannot skip file cleanup or replace an earlier task error."""
    cleaned = []
    primary = RuntimeError("original worker fault")

    def task(worker):
        """Create a valid per-task timing that exceeds the invocation's distinct-key ceiling."""
        get_telemetry_config().method_timings.append(("new_method", 1.0))
        if worker_failure:
            raise primary
        return 1

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(root._download_context) as coordinator:
            coordinator.timing_keys = {f"existing_{index}" for index in range(64)}
            with pytest.raises(
                RuntimeError if worker_failure else ValueError
            ) as caught:
                coordinator.run(
                    [
                        [
                            _job(
                                "a",
                                task,
                                cleanup=cleaned.append,
                            )
                        ]
                    ],
                    on_result=lambda value: None,
                )
            assert cleaned == [False]
            assert coordinator.pending == {}
            if worker_failure:
                assert caught.value is primary


def test_logging_failure_does_not_skip_other_joined_cleanup(client):
    """Even a broken secondary-error logger runs only after every stopped job's cleanup."""
    cleaned = []
    barrier = threading.Barrier(2)
    primary = RuntimeError("task fault")
    diagnostic = RuntimeError("logger fault")

    def task(worker):
        """Keep both tasks active until both cleanup obligations exist."""
        barrier.wait(timeout=5)
        raise primary

    def cleanup(accepted):
        """Record all cleanup attempts, failing the first one deliberately."""
        cleaned.append(accepted)
        if len(cleaned) == 1:
            raise OSError("cleanup fault")

    with DownloadTransport(client, DownloadOptions(concurrency=2)) as root:
        with _Coordinator(root._download_context) as coordinator:
            with patch("regshape.libs._http.logger.error", side_effect=diagnostic):
                with pytest.raises(RuntimeError) as caught:
                    coordinator.run(
                        [
                            [_job("a", task, cleanup=cleanup)],
                            [_job("b", task, cleanup=cleanup)],
                        ],
                        on_result=lambda value: None,
                    )
    assert caught.value is primary
    assert cleaned == [False, False]


@pytest.mark.parametrize("cancel_stage", ["progress", "validation"])
def test_last_queued_callback_cancellation_discards_completed_job(client, cancel_stage):
    """Cancellation from the final queued callback or validation cannot publish a stopped job."""
    consumed = []
    cleaned = []

    def task(worker):
        """Queue one progress record without blocking the task's completion."""
        _progress(worker, 1)
        return "verified"

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(root._download_context) as coordinator:

            def cancel(event):
                """Wait for the stopped future, then cancel from its final queued record."""
                if (
                    cancel_stage == "progress"
                    and event.phase == TransferPhase.TRANSFERRING
                ):
                    pending = next(iter(coordinator.pending.values()))
                    pending.future.result(timeout=5)
                    coordinator.cancel()

            coordinator.progress_callback = cancel
            with pytest.raises(_DownloadCancelled):
                coordinator.run(
                    [
                        [
                            _job(
                                "a",
                                task,
                                cleanup=cleaned.append,
                                terminal=True,
                                validate_result=(
                                    coordinator.cancel
                                    if cancel_stage == "validation"
                                    else None
                                ),
                            )
                        ]
                    ],
                    on_result=consumed.append,
                )
    assert consumed == []
    assert cleaned == [False]


@pytest.mark.parametrize("stage", ["session", "telemetry", "consumer", "cleanup"])
def test_finalization_failure_never_emits_completed_progress(client, stage):
    """No worker success record escapes before session, telemetry, consumer and cleanup succeed."""
    primary = RuntimeError(f"{stage} fault")
    progress = []
    cleaned = []
    original_merge = _Telemetry.merge

    def task(worker):
        """Queue premature success and optionally fail the worker's later session close."""
        if stage == "session":
            original_close = worker.client._session.close

            def close():
                """Release the worker session before injecting its close failure."""
                worker.client._session.close = original_close
                original_close()
                raise primary

            worker.client._session.close = close
        worker.emit(
            TransferProgress(
                worker.job.transfer_id,
                worker.job.digest,
                TransferDirection.DOWNLOAD,
                TransferPhase.COMPLETED,
                1,
                1,
            )
        )
        return "verified"

    def merge(telemetry, parent, keys):
        """Retain real aggregation except for the explicit caller-side merge failure."""
        if stage == "telemetry":
            raise primary
        original_merge(telemetry, parent, keys)

    def consume(value):
        """Fail after observation delivery when testing result-consumer finalization."""
        if stage == "consumer":
            raise primary

    def cleanup(accepted):
        """Record discards even when successful-result cleanup itself raises."""
        cleaned.append(accepted)
        if accepted and stage == "cleanup":
            raise primary

    with (
        patch.object(_Telemetry, "merge", merge),
        DownloadTransport(client, DownloadOptions.sequential()) as root,
    ):
        with _Coordinator(
            root._download_context, progress_callback=progress.append
        ) as coordinator:
            with pytest.raises(RuntimeError) as caught:
                coordinator.run(
                    [[_job("a", task, cleanup=cleanup, terminal=True)]],
                    on_result=consume,
                )
    assert caught.value is primary
    assert [event.phase for event in progress] == [TransferPhase.FAILED]
    assert cleaned[-1] is False


def test_completed_progress_follows_all_finalization_steps(client):
    """A completed row follows session close, telemetry, identity checks, consumer and cleanup."""
    events = []
    original_merge = _Telemetry.merge

    def task(worker):
        """Record actual session closure after producing a deferred success record."""
        original_close = worker.client._session.close

        def close():
            """Release the real worker session before recording stopped resources."""
            worker.client._session.close = original_close
            original_close()
            events.append("session")

        worker.client._session.close = close
        worker.emit(
            TransferProgress(
                worker.job.transfer_id,
                worker.job.digest,
                TransferDirection.DOWNLOAD,
                TransferPhase.COMPLETED,
                1,
                1,
            )
        )
        return "verified"

    def merge(telemetry, parent, keys):
        """Record completed real caller-side aggregation."""
        original_merge(telemetry, parent, keys)
        events.append("telemetry")

    def progress(event):
        """Assert the whole finalization order at the first public success event."""
        if event.phase == TransferPhase.COMPLETED:
            assert events == ["session", "telemetry", "validate", "consumer", "cleanup"]
            events.append("completed")

    with (
        patch.object(_Telemetry, "merge", merge),
        DownloadTransport(client, DownloadOptions.sequential()) as root,
    ):
        with _Coordinator(
            root._download_context, progress_callback=progress
        ) as coordinator:
            coordinator.run(
                [
                    [
                        _job(
                            "a",
                            task,
                            cleanup=lambda accepted: events.append("cleanup"),
                            validate_result=lambda: events.append("validate"),
                            terminal=True,
                        )
                    ]
                ],
                on_result=lambda value: events.append("consumer"),
            )
    assert events[-1] == "completed"


@pytest.mark.parametrize("completed", [False, True])
def test_first_terminal_progress_failure_preserves_healthy_status(client, completed):
    """A progress observer first failing at a terminal event cannot suppress healthy status."""
    statuses = []
    observed = []
    primary = RuntimeError("job fault")
    observer_error = RuntimeError("terminal progress fault")

    def task(worker):
        """Fail before any progress, or finish without progress for successful finalization."""
        if not completed:
            raise primary
        return "verified"

    def progress(event):
        """Fail only when first invoked for the terminal notification."""
        observed.append(event.phase)
        raise observer_error

    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(
            root._download_context,
            progress_callback=progress,
            status_callback=statuses.append,
        ) as coordinator:
            with pytest.raises(RuntimeError) as caught:
                coordinator.run(
                    [[_job("a", task, terminal=True)]],
                    on_result=lambda value: None,
                )
    assert caught.value is (observer_error if completed else primary)
    assert observed == [TransferPhase.COMPLETED if completed else TransferPhase.FAILED]
    assert any(
        event.status.event == ("completed" if completed else "failed")
        for event in statuses
    )

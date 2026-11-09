"""Bound invocation work, marshal observations, and drain before caller cleanup.

This is the internal foundation for batch, range and artifact consumers, not a
public facade. Jobs own exclusive sinks; result consumers and owned-file cleanup
run on the invoking thread. Range sources yield in byte order, and reservations
are retained until their contiguous result has been consumed and cleaned.
"""

import io
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context
from dataclasses import dataclass, replace
from queue import Empty, Full, Queue
from types import TracebackType
from typing import Generic, Literal, TypeVar, cast

from regshape.libs._http import report_secondary_failure
from regshape.libs._logging import LogMessage, emit_log, log_dispatch
from regshape.libs.artifacts.models import ArtifactProgress
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.decorators.metrics import PerformanceMetrics, metrics_collector
from regshape.libs.progress import TransferDirection, TransferPhase, TransferProgress
from regshape.libs.transport import RegistryClient
from regshape.libs.transport.download import (
    DownloadTransport,
    request_lifecycle,
    request_observer,
)
from regshape.libs.transport.middleware import RetryMiddleware

T = TypeVar("T")
_MAX_RECORD_BYTES = 65_536
_MAX_SOURCES = 100_000
_REASONS = frozenset(
    {
        "sequential",
        "ranges_disabled",
        "range_enabled",
        "small_blob",
        "unknown_size",
        "no_strong_validator",
        "range_ignored",
        "nonidentity_encoding",
        "short_range",
        "etag_changed",
        "http_status",
        "transport_error",
    }
)
_EVENTS = frozenset(
    {"strategy", "fallback", "retry", "completed", "failed", "cancelled", "summary"}
)


class _DownloadCancelled(Exception):
    """Internal cooperative cancellation; never a successful partial result."""


@dataclass(frozen=True)
class _Stats:
    """Bounded immutable counters; accepted bytes belong only to the current attempt.

    Durations sum work across jobs except elapsed_seconds, replaced by caller
    wall time in operation/blob observations. Peaks are maxima, not additive.
    """

    request_count: int = 0
    retry_count: int = 0
    bytes_received: int = 0
    bytes_accepted: int = 0
    active_requests_peak: int = 0
    scratch_bytes_peak: int = 0
    elapsed_seconds: float = 0.0
    receive_seconds: float = 0.0
    hash_seconds: float = 0.0
    write_seconds: float = 0.0
    queue_wait_seconds: float = 0.0

    def plus(self, other: "_Stats") -> "_Stats":
        """Sum independent work and preserve monotonic peak counters."""
        return _Stats(
            self.request_count + other.request_count,
            self.retry_count + other.retry_count,
            self.bytes_received + other.bytes_received,
            self.bytes_accepted + other.bytes_accepted,
            max(self.active_requests_peak, other.active_requests_peak),
            max(self.scratch_bytes_peak, other.scratch_bytes_peak),
            self.elapsed_seconds + other.elapsed_seconds,
            self.receive_seconds + other.receive_seconds,
            self.hash_seconds + other.hash_seconds,
            self.write_seconds + other.write_seconds,
            self.queue_wait_seconds + other.queue_wait_seconds,
        )


@dataclass(frozen=True)
class _Status:
    """Safe strategy/lifecycle request; no exception text or registry-controlled fields."""

    event: str
    strategy: Literal["full", "ranged"] | None = None
    reason: str | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        """Reject unknown machine-readable values before crossing the bridge."""
        if self.event not in _EVENTS or self.strategy not in (None, "full", "ranged"):
            raise ValueError("Invalid internal download status")
        if self.reason is not None and self.reason not in _REASONS:
            raise ValueError("Invalid internal download reason")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("Invalid internal HTTP status")


@dataclass(frozen=True)
class _Observation:
    """Caller-thread snapshot to be adapted by the separate public status facade."""

    transfer_id: str | None
    digest: str | None
    attempt: int | None
    status: _Status
    stats: _Stats
    scratch_root: str | None = None


@dataclass(frozen=True)
class _Text:
    """One bounded write/flush for either of the borrowed telemetry destinations."""

    destination: Literal["output", "log"]
    text: str | None


@dataclass(frozen=True)
class _Record:
    """Bounded immutable bridge record, identified by an invocation-local task number."""

    task: int
    payload: TransferProgress | ArtifactProgress | _Status | _Text | LogMessage
    stats: _Stats


class _QueuedOutput(io.StringIO):
    """Implement text writes without buffering text or touching the borrowed sink."""

    def __init__(
        self, worker: "_Worker", destination: Literal["output", "log"]
    ) -> None:
        """Bind one task-local proxy to a caller-owned destination."""
        super().__init__()
        self.worker = worker
        self.destination = destination

    def write(self, text: str) -> int:
        """Enqueue one write; reject an oversized write rather than split or retain it."""
        self.worker.emit(_Text(self.destination, text))
        return len(text)

    def flush(self) -> None:
        """Queue a flush; no external stream method executes on the worker."""
        self.worker.emit(_Text(self.destination, None))


class _Timings(list[tuple[str, float]]):
    """Aggregate at most 64 fixed method names, even across task scenario flushes."""

    def __init__(self) -> None:
        """Create separate visible scenario entries and lifetime task totals."""
        super().__init__()
        self.totals: dict[str, float] = {}

    def append(self, item: tuple[str, float]) -> None:
        """Accumulate a bounded method key instead of retaining one entry per request."""
        name, duration = item
        if len(name) > 256 or name not in self.totals and len(self.totals) >= 64:
            raise ValueError("Download method timing key limit exceeded")
        self.totals[name] = self.totals.get(name, 0.0) + duration
        for index, (key, elapsed) in enumerate(self):
            if key == name:
                self[index] = (name, elapsed + duration)
                return
        super().append(item)


@dataclass(frozen=True)
class _Telemetry:
    """Immutable task totals, including metrics consumed by nested scenario renderers."""

    counts: tuple[int, int, int, int, int]
    elapsed: float
    statuses: tuple[tuple[int, int], ...]
    timings: tuple[tuple[str, float], ...]

    def merge(self, parent: TelemetryConfig, timing_keys: set[str]) -> None:
        """Add counters/timings on the caller without clearing parent scenario state."""
        new_keys = {name for name, _ in self.timings}
        if len(timing_keys | new_keys) > 64:
            raise ValueError("Download method timing key limit exceeded")
        timing_keys.update(new_keys)
        metrics = parent.metrics
        requests, sent, received, retries, errors = self.counts
        metrics.total_requests += requests
        metrics.total_bytes_sent += sent
        metrics.total_bytes_received += received
        metrics.retries += retries
        metrics.errors += errors
        metrics.total_elapsed += self.elapsed
        for code, count in self.statuses:
            metrics.status_code_counts[code] = (
                metrics.status_code_counts.get(code, 0) + count
            )
        for name, duration in self.timings:
            for index, (key, elapsed) in enumerate(parent.method_timings):
                if key == name:
                    parent.method_timings[index] = (name, elapsed + duration)
                    break
            else:
                parent.method_timings.append((name, duration))


@dataclass(frozen=True)
class _Job(Generic[T]):
    """One exclusive-sink task from an ordered per-blob source.

    :param cleanup: Invoking-thread callback after the task has stopped. True
        means its result consumer succeeded; False means discard owned scratch.
        It must check identity and never remove an unproven replacement.
    :param scratch_bytes: Reserved spool capacity, 0..range_size, held through
        ordered consumption and cleanup. Full-stream jobs reserve zero.
    :param terminal: True for a verified final job (all full-stream jobs). Its
        ordered source must have no trailing jobs, and completion is emitted
        immediately after successful result consumption and cleanup.
    :param validate_result: Optional invoking-thread validation after queued
        observers drain, immediately before result consumption. Failures discard
        the stopped job instead of publishing an unproven output.
    """

    transfer_id: str
    digest: str
    run: Callable[["_Worker"], T]
    cleanup: Callable[[bool], None]
    scratch_bytes: int = 0
    attempt: int = 1
    strategy: Literal["full", "ranged"] = "full"
    reason: str | None = None
    terminal: bool = False
    validate_result: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        """Validate identity/attempt using the existing immutable progress contract."""
        TransferProgress(
            self.transfer_id,
            self.digest,
            TransferDirection.DOWNLOAD,
            TransferPhase.PREPARING,
            0,
            None,
            self.attempt,
        )
        _Status("strategy", self.strategy, self.reason)
        if not callable(self.run) or not callable(self.cleanup):
            raise ValueError("Download jobs require run and cleanup callables")
        if type(self.scratch_bytes) is not int or self.scratch_bytes < 0:
            raise ValueError("Download scratch reservation must be nonnegative")
        if type(self.terminal) is not bool:
            raise ValueError("Download terminal marker must be boolean")
        if self.validate_result is not None and not callable(self.validate_result):
            raise ValueError("Download result validation must be callable")


@dataclass(frozen=True)
class _Outcome(Generic[T]):
    """Stopped-task result; exceptions stay out of the immutable observation queue."""

    value: T | None
    error: BaseException | None
    stats: _Stats
    telemetry: _Telemetry
    produced_records: int = 0
    secondary_errors: tuple[BaseException, ...] = ()


@dataclass
class _Pending(Generic[T]):
    """Caller-owned reservation retained until ordered consumption or joined cleanup."""

    number: int
    job: _Job[T]
    future: Future[_Outcome[T]]
    stats: _Stats = _Stats()
    merged: bool = False
    delivered_records: int = 0


@dataclass
class _Source(Generic[T]):
    """Lazy ordered source; at most W consecutive uncommitted tasks may be retained."""

    jobs: Iterator[_Job[T]]
    pending: deque[_Pending[T]]
    identity: tuple[str, str, int] | None = None
    exhausted: bool = False
    completed: bool = False


class _Worker:
    """Task-local client, counters and cancellation-aware observation producer."""

    def __init__(self, owner: "_Coordinator", number: int, job: _Job) -> None:
        """Borrow invocation transport and retain only this task's immutable identity."""
        self.owner = owner
        self.number = number
        self.job = job
        self.stats = _Stats()
        self.client: RegistryClient | None = None
        self.records = 0

    def check_cancelled(self) -> None:
        """Raise cooperative cancellation between reads, never claim to interrupt a socket read."""
        if self.owner.cancelled.is_set():
            raise _DownloadCancelled("Download cancelled")

    def wait(self, seconds: float) -> None:
        """Wait without holding a response/slot and wake promptly on cancellation."""
        if self.owner.cancelled.wait(seconds):
            self.check_cancelled()

    def request(self, count: int, elapsed: float) -> None:
        """Count physical hops/auth requests; a failed connection counts as one request."""
        self.stats = replace(
            self.stats,
            request_count=self.stats.request_count + count,
            receive_seconds=self.stats.receive_seconds + elapsed,
        )

    def emit(
        self,
        payload: TransferProgress | ArtifactProgress | _Status | _Text | LogMessage,
    ) -> None:
        """Apply bounded cancellation-aware backpressure; payloads never contain entity bytes."""
        if isinstance(payload, (TransferProgress, ArtifactProgress)) and (
            payload.transfer_id != self.job.transfer_id
            or payload.attempt != self.job.attempt
        ):
            raise ValueError(
                "Download progress must retain its task identity and attempt"
            )
        record = _Record(self.number, payload, self.stats)
        size = len(repr(record).encode("utf-8"))
        if size > _MAX_RECORD_BYTES:
            raise ValueError("Download diagnostic record exceeds 64 KiB")
        started = time.perf_counter()
        while True:
            self.check_cancelled()
            try:
                self.owner.queue.put(record, timeout=0.02)
                self.records += 1
                break
            except Full:
                continue
        self.stats = replace(
            self.stats,
            queue_wait_seconds=self.stats.queue_wait_seconds
            + time.perf_counter()
            - started,
        )


class _Coordinator:
    """Own exactly one pool for an invocation; callbacks and result sinks stay on its caller.

    Repeated run calls reuse the pool, allowing an explicit drained strategy
    reset. A source must yield all ranges of one transfer/attempt in byte order.
    Deferred range results retain both their job slot and spool reservation.
    Use inside the DownloadTransport lifetime; close before removing owned scratch.
    """

    def __init__(
        self,
        transport: DownloadTransport,
        *,
        progress_callback: (
            Callable[[TransferProgress | ArtifactProgress], None] | None
        ) = None,
        status_callback: Callable[[_Observation], None] | None = None,
    ) -> None:
        """Validate observers without starting threads or allocating scratch."""
        for callback in (progress_callback, status_callback):
            if callback is not None and not callable(callback):
                raise ValueError("Download observers must be callable")
        self.transport = transport
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.parent = get_telemetry_config()
        self.queue: Queue[_Record] = Queue(maxsize=4 * transport.options.concurrency)
        self.cancelled = threading.Event()
        self.executor: ThreadPoolExecutor | None = None
        self.thread = threading.get_ident()
        self.failed_observers: set[int] = set()
        self.pending: dict[int, _Pending] = {}
        self.totals: dict[str, _Stats] = {}
        self.identities: dict[str, tuple[str, int, str, str | None]] = {}
        self.next_number = 0
        self.scratch_bytes = 0
        self.scratch_peak = 0
        self.blob_scratch: dict[str, int] = {}
        self.blob_scratch_peak: dict[str, int] = {}
        self.jobs_peak = 0
        self.started = time.perf_counter()
        self.blob_started: dict[str, float] = {}
        self.active: dict[str, int] = {}
        self.peaks: dict[str, int] = {}
        self.activity_lock = threading.Lock()
        self.timing_keys: set[str] = set()
        self.last_progress: dict[str, TransferProgress | ArtifactProgress] = {}
        self.completed: set[tuple[str, int]] = set()
        self.failure_transfer: str | None = None

    def __enter__(self) -> "_Coordinator":
        """Create the invocation's sole executor; reject nested/replacement coordinators."""
        self._on_caller()
        with self.transport.lock:
            if self.transport.closed or self.transport._coordinator_created:
                raise RuntimeError(
                    "Download transport permits one invocation-owned coordinator"
                )
            self.transport._coordinator_created = True
        self.executor = ThreadPoolExecutor(
            max_workers=self.transport.options.concurrency,
            thread_name_prefix="regshape-download",
        )
        return self

    def _on_caller(self) -> None:
        """Reject moving orchestration, sinks or cleanup to another thread."""
        if threading.get_ident() != self.thread:
            raise RuntimeError("Download coordinator must run on its invoking thread")

    def cancel(self) -> None:
        """Signal cancellation from any thread; active buffered reads still must return."""
        self.cancelled.set()

    def _activity(self, transfer_id: str, acquired: bool) -> None:
        """Track actual per-blob request leases, not queued executor tasks."""
        with self.activity_lock:
            count = self.active.get(transfer_id, 0) + (1 if acquired else -1)
            self.active[transfer_id] = count
            self.peaks[transfer_id] = max(self.peaks.get(transfer_id, 0), count)

    def snapshot(self, transfer_id: str | None = None) -> _Stats:
        """Return available current-attempt counts; operation durations may overlap."""
        self._on_caller()
        total = _Stats()
        if transfer_id is None:
            for stats in self.totals.values():
                total = total.plus(stats)
        else:
            total = self.totals.get(transfer_id, _Stats())
        for pending in self.pending.values():
            if transfer_id is None or pending.job.transfer_id == transfer_id:
                total = total.plus(pending.stats)
        elapsed_start = (
            self.started
            if transfer_id is None
            else self.blob_started.get(
                transfer_id,
                self.started,
            )
        )
        return replace(
            total,
            request_count=(
                self.transport.request_count
                if transfer_id is None
                else total.request_count
            ),
            elapsed_seconds=time.perf_counter() - elapsed_start,
            scratch_bytes_peak=(
                self.scratch_peak
                if transfer_id is None
                else self.blob_scratch_peak.get(transfer_id, 0)
            ),
            active_requests_peak=(
                self.transport.active_peak
                if transfer_id is None
                else self.peaks.get(transfer_id, 0)
            ),
        )

    def _call(self, callback: Callable[[T], None] | None, value: T) -> None:
        """Call an observer once per event and permanently disable it after its first error."""
        if callback is None or id(callback) in self.failed_observers:
            return
        succeeded = False
        try:
            callback(value)
            succeeded = True
        finally:
            if not succeeded:
                self.failed_observers.add(id(callback))
                self.cancel()

    def status(self, transfer_id: str | None, status: _Status) -> None:
        """Emit caller-thread status, including verified etag_changed completion reasons."""
        self._on_caller()
        identity = self.identities.get(transfer_id) if transfer_id is not None else None
        try:
            self._call(
                self.status_callback,
                _Observation(
                    transfer_id,
                    identity[0] if identity else None,
                    identity[1] if identity else None,
                    status,
                    self.snapshot(transfer_id),
                ),
            )
        except BaseException:
            self.failure_transfer = transfer_id
            raise

    def reset(
        self,
        transfer_id: str,
        reset_sink: Callable[[], None],
        *,
        reason: str = "etag_changed",
    ) -> None:
        """Reset a drained transfer through a caller-supplied identity-checked sink reset.

        Received bytes remain cumulative; accepted bytes reset to zero and the
        same transfer ID advances one attempt. A failed reset never permits replay.
        All previous jobs must have drained and joined before this method is called.
        """
        self._on_caller()
        if self.pending or self.failed_observers:
            raise RuntimeError("Drain and join old jobs before resetting a download")
        _Status("fallback", "full", reason)
        digest, attempt, _, _ = self.identities[transfer_id]
        reset_sink()
        self.totals[transfer_id] = replace(
            self.totals.get(transfer_id, _Stats()),
            bytes_accepted=0,
        )
        self.identities[transfer_id] = (digest, attempt + 1, "full", reason)
        self.cancelled.clear()
        self.status(transfer_id, _Status("fallback", "full", reason))
        previous = self.last_progress.get(transfer_id)
        if previous is not None:
            event: TransferProgress | ArtifactProgress
            if isinstance(previous, TransferProgress):
                event = replace(
                    previous,
                    bytes_completed=0,
                    attempt=attempt + 1,
                    phase=TransferPhase.PREPARING,
                )
            else:
                event = replace(
                    previous,
                    bytes_completed=0,
                    attempt=attempt + 1,
                    phase="preparing",
                )
            self.last_progress[transfer_id] = event
            self._call(self.progress_callback, event)

    def _task(self, number: int, job: _Job[T]) -> _Outcome[T]:
        """Run in a fresh Context with isolated metrics, timing and queued diagnostics."""
        worker = _Worker(self, number, job)
        timings = _Timings()
        collector = PerformanceMetrics()
        config = replace(
            self.parent,
            output=_QueuedOutput(worker, "output"),
            log_file=(
                _QueuedOutput(worker, "log")
                if self.parent.log_file is not None
                else None
            ),
            metrics=PerformanceMetrics(),
            method_timings=timings,
        )
        configure_telemetry(config)
        metrics_collector.set(collector)
        log_dispatch.set(worker.emit)
        request_observer.set(worker.request)
        request_lifecycle.set(
            lambda acquired: self._activity(job.transfer_id, acquired)
        )
        value: T | None = None
        error: BaseException | None = None
        secondary_errors: tuple[BaseException, ...] = ()
        try:
            worker.check_cancelled()
            worker.client = self.transport.fork()
            if worker.client._pipeline is not None:
                worker.client._pipeline._middleware = [
                    item
                    for item in worker.client._pipeline._middleware
                    if not isinstance(item, RetryMiddleware)
                ]
            value = job.run(worker)
        except BaseException as primary:
            error = primary
        finally:
            if worker.client is not None and worker.client._session is not None:
                try:
                    worker.client._session.close()
                except BaseException as secondary:
                    if error is None:
                        error = secondary
                    else:
                        secondary_errors = (secondary,)
        telemetry = _Telemetry(
            (
                collector.total_requests,
                collector.total_bytes_sent,
                collector.total_bytes_received,
                collector.retries,
                collector.errors,
            ),
            collector.total_elapsed,
            tuple(collector.status_code_counts.items()),
            tuple(timings.totals.items()),
        )
        return _Outcome(
            value, error, worker.stats, telemetry, worker.records, secondary_errors
        )

    def _pump(self, *, discard: bool = False) -> None:
        """Pump one record without waiting on Futures; defer success until caller finalization."""
        try:
            record = self.queue.get(timeout=0.01)
        except Empty:
            return
        pending = self.pending.get(record.task)
        if pending is None:
            return
        pending.delivered_records += 1
        pending.stats = record.stats
        if discard:
            if isinstance(record.payload, LogMessage):
                logger = logging.getLogger(record.payload.name)
                if id(logger) not in self.failed_observers:
                    self._log_message(record.payload)
            return
        payload = record.payload
        self.failure_transfer = pending.job.transfer_id
        if isinstance(payload, (TransferProgress, ArtifactProgress)):
            self.last_progress[pending.job.transfer_id] = payload
            if payload.phase != "completed":
                self._call(self.progress_callback, payload)
        elif isinstance(payload, _Status):
            self.status(pending.job.transfer_id, payload)
        elif isinstance(payload, _Text):
            destination = (
                self.parent.output
                if payload.destination == "output"
                else self.parent.log_file
            )
            if destination is not None and id(destination) not in self.failed_observers:
                succeeded = False
                try:
                    if payload.text is None:
                        destination.flush()
                    else:
                        destination.write(payload.text)
                    succeeded = True
                finally:
                    if not succeeded:
                        self.failed_observers.add(id(destination))
                        self.cancel()
        else:
            logger = logging.getLogger(payload.name)
            if id(logger) not in self.failed_observers:
                try:
                    self._log_message(payload)
                except BaseException:
                    self.cancel()
                    raise
        self.failure_transfer = None

    def _log_message(self, message: LogMessage) -> None:
        """Reconstruct the log only on the caller; never retain worker traceback objects."""
        logger = logging.getLogger(message.name)
        succeeded = False
        try:
            emit_log(
                logger,
                message.level,
                message.text,
                extra=json.loads(message.metadata),
                method=message.method,
            )
            succeeded = True
        finally:
            if not succeeded:
                self.failed_observers.add(id(logger))

    def _merge(self, pending: _Pending) -> _Outcome:
        """Collect a stopped task once, preserving immutable final accounting on failure."""
        outcome = pending.future.result()
        pending.stats = outcome.stats
        if not pending.merged:
            outcome.telemetry.merge(self.parent, self.timing_keys)
            pending.merged = True
            for secondary in outcome.secondary_errors:
                if outcome.error is not None:
                    report_secondary_failure(
                        outcome.error, secondary, "Download worker session cleanup"
                    )
        return outcome

    def _release(self, pending: _Pending) -> None:
        """Release retained slots only after caller consumption and owned-file cleanup."""
        identity = pending.job.transfer_id
        self.totals[identity] = self.totals.get(identity, _Stats()).plus(pending.stats)
        self.scratch_bytes -= pending.job.scratch_bytes
        self.blob_scratch[identity] -= pending.job.scratch_bytes
        del self.pending[pending.number]

    def _terminal_observers(
        self, identity: str, event: Literal["completed", "failed", "cancelled"]
    ) -> list[BaseException]:
        """Attempt both terminal observers independently, returning errors for caller priority.

        Artifact receipt uses its native received phase, retaining stored/skipped
        when already established; only TransferProgress has a completed phase.
        """
        errors: list[BaseException] = []
        digest, attempt, strategy, reason = self.identities[identity]
        try:
            progress = self.last_progress.get(identity)
            if progress is None:
                progress = TransferProgress(
                    identity,
                    digest,
                    TransferDirection.DOWNLOAD,
                    TransferPhase.PREPARING,
                    self.snapshot(identity).bytes_accepted,
                    None,
                    attempt,
                )
            terminal: TransferProgress | ArtifactProgress
            if isinstance(progress, TransferProgress):
                terminal = replace(progress, phase=TransferPhase(event))
            else:
                artifact_phase: str = event
                if event == "completed":
                    artifact_phase = (
                        progress.phase
                        if progress.phase in ("stored", "skipped")
                        else "received"
                    )
                terminal = replace(progress, phase=artifact_phase)
            self.last_progress[identity] = terminal
            self._call(self.progress_callback, terminal)
        except BaseException as error:
            errors.append(error)
        try:
            self.status(
                identity,
                _Status(event, cast(Literal["full", "ranged"], strategy), reason),
            )
        except BaseException as error:
            errors.append(error)
        return errors

    def _complete_source(self, source: _Source) -> None:
        """Notify completion after consumption/cleanup; never reclassify a committed source."""
        if source.exhausted and not source.pending and not source.completed:
            source.completed = True
            if source.identity is not None:
                identity, _, attempt = source.identity
                self.completed.add((identity, attempt))
                errors = self._terminal_observers(identity, "completed")
                if errors:
                    for secondary in errors[1:]:
                        report_secondary_failure(
                            errors[0], secondary, "Download terminal observer"
                        )
                    raise errors[0]

    def run(
        self,
        sources: Sequence[Iterable[_Job[T]]],
        *,
        on_result: Callable[[T], None],
    ) -> _Stats:
        """Round-robin ordered sources under global C and per-blob W uncommitted limits.

        :param on_result: Invoking-thread consumer; publish only verified results.
            Range sources are delivered in source order, not completion order.
        :returns: Final available operation counters after source cleanup.
        :raises BaseException: Original task/observer/consumer error, after draining
            every producer and joining active tasks. Earlier results are not rolled back.
        """
        self._on_caller()
        if self.executor is None or self.pending:
            raise RuntimeError("Use an open, idle download coordinator")
        if len(sources) > _MAX_SOURCES or not callable(on_result):
            raise ValueError("Invalid download sources or result consumer")
        ready = deque(_Source(iter(source), deque()) for source in sources)
        identities: set[str] = set()
        primary: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        self.failure_transfer = None
        try:
            while ready or self.pending:
                if self.cancelled.is_set():
                    raise _DownloadCancelled("Download cancelled")
                for _ in range(len(ready)):
                    if len(self.pending) >= self.transport.options.concurrency:
                        break
                    source = ready.popleft()
                    if not source.exhausted and (
                        len(self.pending) < self.transport.options.concurrency
                        and len(source.pending) < self.transport.options.ranges_per_blob
                    ):
                        job = next(source.jobs, None)
                        if job is None:
                            source.exhausted = True
                        else:
                            if job.scratch_bytes > self.transport.options.range_size:
                                raise ValueError(
                                    "Download spool reservation exceeds range_size"
                                )
                            source_identity = (job.transfer_id, job.digest, job.attempt)
                            if source.identity is None:
                                if job.transfer_id in identities:
                                    raise ValueError(
                                        "A transfer must have one ordered download source"
                                    )
                                identities.add(job.transfer_id)
                                source.identity = source_identity
                            elif source.identity != source_identity:
                                raise ValueError(
                                    "A source must retain its transfer and attempt"
                                )
                            previous = self.identities.get(job.transfer_id)
                            if (
                                previous is None
                                and len(self.identities) >= _MAX_SOURCES
                            ):
                                raise ValueError(
                                    "Download transfer identity limit exceeded"
                                )
                            if previous is not None and previous[:2] != (
                                job.digest,
                                job.attempt,
                            ):
                                raise ValueError(
                                    "Explicitly reset a transfer before changing its attempt"
                                )
                            self.identities[job.transfer_id] = (
                                job.digest,
                                job.attempt,
                                job.strategy,
                                job.reason,
                            )
                            self.blob_started.setdefault(
                                job.transfer_id, time.perf_counter()
                            )
                            number = self.next_number
                            self.next_number += 1
                            pending = _Pending(
                                number,
                                job,
                                self.executor.submit(
                                    Context().run, self._task, number, job
                                ),
                            )
                            self.pending[number] = pending
                            source.pending.append(pending)
                            self.scratch_bytes += job.scratch_bytes
                            self.blob_scratch[job.transfer_id] = (
                                self.blob_scratch.get(job.transfer_id, 0)
                                + job.scratch_bytes
                            )
                            self.blob_scratch_peak[job.transfer_id] = max(
                                self.blob_scratch_peak.get(job.transfer_id, 0),
                                self.blob_scratch[job.transfer_id],
                            )
                            self.scratch_peak = max(
                                self.scratch_peak, self.scratch_bytes
                            )
                            self.jobs_peak = max(self.jobs_peak, len(self.pending))
                            if previous is None:
                                self.status(
                                    job.transfer_id,
                                    _Status("strategy", job.strategy, job.reason),
                                )
                    if not source.exhausted or source.pending:
                        ready.append(source)
                    else:
                        self._complete_source(source)
                self._pump()
                if self.cancelled.is_set():
                    raise _DownloadCancelled("Download cancelled")
                for pending in tuple(self.pending.values()):
                    if pending.future.done():
                        outcome = pending.future.result()
                        if outcome.error is not None:
                            self.failure_transfer = pending.job.transfer_id
                            raise outcome.error
                for source in ready:
                    while source.pending and source.pending[0].future.done():
                        head = source.pending[0]
                        if (
                            head.delivered_records
                            < head.future.result().produced_records
                        ):
                            break
                        pending = source.pending.popleft()
                        outcome = self._merge(pending)
                        self.failure_transfer = pending.job.transfer_id
                        if pending.job.terminal:
                            if source.pending or next(source.jobs, None) is not None:
                                raise ValueError(
                                    "Download source has jobs after its terminal job"
                                )
                            source.exhausted = True
                        if pending.job.validate_result is not None:
                            pending.job.validate_result()
                        if self.cancelled.is_set():
                            raise _DownloadCancelled("Download cancelled")
                        self._call(on_result, cast(T, outcome.value))
                        pending.job.cleanup(True)
                        self._release(pending)
                        self.failure_transfer = None
                    self._complete_source(source)
            while not self.queue.empty():
                self._pump()
        except BaseException as error:
            primary = error
            self.cancel()
        finally:
            # Do not cancel futures: even not-yet-started tasks must yield cleanup accounting.
            while not self.queue.empty() or any(
                not item.future.done() for item in self.pending.values()
            ):
                try:
                    self._pump(discard=True)
                except BaseException as cleanup:
                    if primary is None:
                        primary = cleanup
                    else:
                        cleanup_errors.append(cleanup)
            for pending in tuple(self.pending.values()):
                try:
                    self._merge(pending)
                except BaseException as cleanup:
                    if primary is None:
                        primary = cleanup
                    else:
                        cleanup_errors.append(cleanup)
                try:
                    pending.job.cleanup(False)
                except BaseException as cleanup:
                    if primary is None:
                        primary = cleanup
                    else:
                        cleanup_errors.append(cleanup)
                finally:
                    self._release(pending)
        if primary is not None:
            for cleanup_error in cleanup_errors:
                report_secondary_failure(primary, cleanup_error, "Download job cleanup")
        if primary is not None:
            failure_transfer = self.failure_transfer
            terminal_errors: list[BaseException] = []
            for identity in identities:
                _, attempt, _, _ = self.identities[identity]
                if (identity, attempt) in self.completed:
                    continue
                cancelled = (
                    isinstance(primary, (_DownloadCancelled, KeyboardInterrupt))
                    or failure_transfer is not None
                    and identity != failure_transfer
                )
                terminal_errors.extend(
                    self._terminal_observers(
                        identity, "cancelled" if cancelled else "failed"
                    )
                )
            for secondary in terminal_errors:
                report_secondary_failure(
                    primary, secondary, "Download terminal observer"
                )
            raise primary
        return self.snapshot()

    def close(self) -> None:
        """Join the sole executor before returning control to caller scratch cleanup."""
        self._on_caller()
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Join before a final summary; preserve any active original domain/observer error."""
        try:
            self.close()
            self.status(None, _Status("summary"))
        except BaseException as secondary:
            if exc is None:
                raise
            report_secondary_failure(exc, secondary, "Download coordinator close")
        return False

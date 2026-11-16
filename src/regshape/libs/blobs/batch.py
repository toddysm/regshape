"""Expose bounded batch downloads and adapt internal observations for SDK callers.

One invocation borrows one client, owns one pooled transport/coordinator, and
validates every request/sink before acquisition. Artifact nesting reuses that
coordinator; this layer does not implement graph or HTTP-range scheduling.
"""

import os
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from inspect import getattr_static
from types import BuiltinMethodType, MethodType
from typing import TypeVar, cast

import requests

from regshape.libs.artifacts.models import ArtifactProgress
from regshape.libs.blobs._download_worker import _FullBlob
from regshape.libs.blobs.download import (
    BlobBatchResult,
    BlobDownload,
    DownloadEvent,
    DownloadOptions,
    DownloadReason,
    DownloadStats,
    DownloadStatus,
    resolve_download_options,
)
from regshape.libs.errors import BlobError
from regshape.libs.manifests.download import _validate_inputs
from regshape.libs.models.blob import BlobInfo
from regshape.libs.progress import ProgressCallback, TransferProgress
from regshape.libs.transport import RegistryClient
from regshape.libs.transport._coordinator import (
    _Coordinator, _Job, _Observation, _Stats, _Worker,
)
from regshape.libs.transport.download import DownloadTransport

_MAX_DOWNLOADS = 100_000
T = TypeVar("T")


def _stats(snapshot: _Stats) -> DownloadStats:
    """Copy the fixed internal counters into a validated immutable public record."""
    return DownloadStats(**asdict(snapshot))


def _observer_key(callback: Callable) -> tuple[int, int]:
    """Identify bound receivers/implementations, not transient method wrappers.

    Static descriptor lookup also covers native bound methods without invoking
    caller-defined properties. Other callable objects retain their own identity.
    """
    if isinstance(callback, MethodType):
        return id(callback.__self__), id(callback.__func__)
    if isinstance(callback, BuiltinMethodType) and callback.__self__ is not None:
        descriptor = getattr_static(callback.__self__, callback.__name__, None)
        if descriptor is not None:
            return id(callback.__self__), id(descriptor)
    return id(callback), 0


def _guard_observer(
    callback: Callable[[T], None] | None, failures: set[tuple[int, int]],
) -> Callable[[T], None] | None:
    """Disable one callable/receiver-method pair across adapters after its first exception."""
    if callback is None:
        return None
    key = _observer_key(callback)

    def observe(record: T) -> None:
        """Preserve exception identity and never reenter an already failed observer."""
        if key in failures:
            return
        succeeded = False
        try:
            callback(record)
            succeeded = True
        finally:
            if not succeeded:
                failures.add(key)

    return observe


def _status_observer(
    callback: Callable[[DownloadStatus], None] | None,
    failures: set[tuple[int, int]] | None = None,
    summary: list[DownloadStats] | None = None,
) -> Callable[[_Observation], None] | None:
    """Adapt fixed codes and optionally retain the exact final batch summary."""
    if callback is None and summary is None:
        return None
    guarded = _guard_observer(callback, failures if failures is not None else set())

    def observe(record: _Observation) -> None:
        """Deliver a detached snapshot on the coordinator's invoking thread."""
        status = DownloadStatus(
            record.transfer_id, record.digest,
            cast(DownloadEvent, record.status.event), record.status.strategy,
            cast(DownloadReason | None, record.status.reason),
            record.status.http_status, record.attempt, _stats(record.stats),
            record.scratch_root,
        )
        if status.event == "summary" and summary is not None:
            summary.append(status.stats)
        if guarded is not None:
            guarded(status)

    return observe


def _progress_observer(
    callback: ProgressCallback | None,
    failures: set[tuple[int, int]],
) -> Callable[[TransferProgress | ArtifactProgress], None] | None:
    """Narrow the coordinator's union for the existing standalone blob observer."""
    guarded = _guard_observer(callback, failures)
    if guarded is None:
        return None

    def observe(record: TransferProgress | ArtifactProgress) -> None:
        """Reject an internal routing error rather than invent a blob observation."""
        if not isinstance(record, TransferProgress):
            raise RuntimeError("Blob coordinator produced non-blob progress")
        guarded(record)

    return observe


def _validate_observers(*callbacks: Callable | None) -> None:
    """Reject invalid observers before allocating a pool or opening an output."""
    if any(callback is not None and not callable(callback) for callback in callbacks):
        raise ValueError("Download observers must be callable")


@contextmanager
def _coordinated(
    client: RegistryClient,
    policy: DownloadOptions,
    *,
    progress_callback: ProgressCallback | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
    summary: list[DownloadStats] | None = None,
) -> Iterator[tuple[RegistryClient, _Coordinator]]:
    """Own or borrow the invocation coordinator; temporarily route nested progress.

    The outer owner alone closes workers/transport and emits an aggregate summary.
    Nested singular calls inherit status delivery and may supply their own
    caller-thread blob-to-artifact progress adapter. Replacing an active status
    observer is rejected instead of losing the operation's observations.
    """
    context = getattr(client, "_download_context", None)
    if not isinstance(context, DownloadTransport):
        with DownloadTransport(client, policy) as root:
            with _coordinated(
                root, policy, progress_callback=progress_callback,
                download_status_callback=download_status_callback,
                summary=summary,
            ) as owned:
                yield owned
        return
    if context.coordinator is not None:
        if download_status_callback is not None or summary is not None:
            raise ValueError("Nested downloads inherit their operation status observer")
        coordinator = context.coordinator
        previous = coordinator.progress_callback
        coordinator.progress_callback = _progress_observer(progress_callback, context.observer_failures)
        try:
            yield client, coordinator
        finally:
            coordinator.progress_callback = previous
        return
    with _Coordinator(
        context, progress_callback=_progress_observer(progress_callback, context.observer_failures),
        status_callback=_status_observer(download_status_callback, context.observer_failures, summary),
    ) as coordinator:
        context.coordinator = coordinator
        try:
            yield client, coordinator
        finally:
            context.coordinator = None


def _full_blob(
    repo: str, request: BlobDownload, policy: DownloadOptions, *,
    transfer_id: str | None = None,
) -> _FullBlob:
    """Preflight a sink, optionally retaining an artifact row's existing identity."""
    path = request.output_path
    if path is not None:
        try:
            path = os.fspath(path)
        except TypeError:
            raise ValueError("Invalid blob output path") from None
        if not isinstance(path, str):
            raise ValueError("Blob output path must be text")
    if request.output_dir_fd is not None and (
        type(request.output_dir_fd) is not int or request.output_dir_fd < 0
    ):
        raise ValueError("output_dir_fd must be a nonnegative directory handle")
    return _FullBlob(
        repo, request.digest, path, request.chunk_size, total_size=request.total_size,
        max_bytes=request.max_bytes, output_dir_fd=request.output_dir_fd,
        transfer_id=transfer_id,
        reason="sequential" if policy.concurrency == 1 else "ranges_disabled",
    )


def _job(blob: _FullBlob) -> _Job[BlobInfo]:
    """Retain singular output-error taxonomy without wrapping observer/transport errors."""
    def run(worker: _Worker) -> BlobInfo:
        """Convert only worker-side filesystem failures; observers run on the caller."""
        try:
            return blob.run(worker)
        except requests.exceptions.RequestException:
            raise
        except OSError as error:
            raise BlobError("Cannot write to blob output path") from error

    return replace(blob.job(), run=run)


def _receive_blob(
    client: RegistryClient, policy: DownloadOptions, request: BlobDownload, repo: str, *,
    progress_callback: ProgressCallback | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
    transfer_id: str | None = None,
) -> BlobInfo:
    """Share singular receipt with artifact rows without changing either public schema.

    Validate before acquisition, inherit an enclosing coordinator/status observer,
    and return only after the verified sink and its caller-side cleanup finish.
    """
    _validate_observers(progress_callback, download_status_callback)
    blob = _full_blob(repo, request, policy, transfer_id=transfer_id)
    results: list[BlobInfo] = []
    with _coordinated(
        client, policy, progress_callback=progress_callback,
        download_status_callback=download_status_callback,
    ) as (_, coordinator):
        coordinator.run([[_job(blob)]], on_result=results.append)
    return results[0]


def _sinks(blobs: list[_FullBlob]) -> None:
    """Reject path aliases and existing hard-link identities before any file is opened.

    Parent identities also correlate handle-relative names with ordinary paths.
    Names are conservatively Unicode-normalized/case-folded, so portable aliases
    are rejected even on a case-sensitive filesystem.
    This is preflight, not a reservation or protection from hostile path changes;
    callers retain singular downloads' trusted-namespace obligation.

    :raises BlobError: For filesystem failures inspecting output paths or handles.
    """
    paths: set[tuple[int, int, str]] = set()
    files: set[tuple[int, int]] = set()
    for blob in blobs:
        output = blob.output
        if output.path is None:
            continue
        try:
            if output.dir_fd is None:
                path = output.path.resolve()
                parent = path.parent.stat()
            else:
                path = output.path
                parent = os.fstat(output.dir_fd)
            key = (parent.st_dev, parent.st_ino, unicodedata.normalize("NFD", path.name).casefold())
            if key in paths:
                raise ValueError("Batch downloads require distinct output paths")
            paths.add(key)
            try:
                current = os.stat(path, dir_fd=output.dir_fd)
            except FileNotFoundError:
                continue
        except OSError as error:
            raise BlobError("Cannot write to blob output path") from error
        identity = (current.st_dev, current.st_ino)
        if identity in files:
            raise ValueError("Batch downloads require distinct output file identities")
        files.add(identity)


def get_blobs(
    client: RegistryClient,
    repo: str,
    downloads: Iterable[BlobDownload],
    *,
    download_options: DownloadOptions | None = None,
    progress_callback: ProgressCallback | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> BlobBatchResult:
    """Verify a bounded batch under one C cap, returning results in input order.

    :param client: Borrowed supported pooled transport; legacy is unsupported.
    :param repo: One strict relative registry repository for the batch.
    :param downloads: At most 100,000 distinct requests, consumed once before effects.
    :param download_options: Shared immutable policy; defaults select pooled C=4.
    :param progress_callback: Caller-thread blob observer, with stable transfer IDs.
    :param download_status_callback: Caller-thread immutable status/metrics observer.
    :returns: All verified infos in input order and aggregate final counters.
    :raises ValueError: For invalid inputs, duplicate digests or colliding sinks.
    :raises BlobError: For invalid digests, failed verification, sink inspection
        or output writes.

    First observed failure cancels/drains peers and re-raises the original error,
    including exact observer exceptions; no partial result is returned. Earlier
    verified files may remain. Bounds/sink checks precede auth/pool/output effects.
    Other transport/filesystem failures retain their native taxonomy. Separate
    calls do not share a process-global cap. Ranges await the range engine.
    """
    policy = resolve_download_options(client, download_options, requires_pooled=True)
    _validate_observers(progress_callback, download_status_callback)
    _validate_inputs(repo, "batch", "application/octet-stream", None, None, 65_536)
    try:
        entries = iter(downloads)
    except TypeError:
        raise ValueError("downloads must be an iterable of BlobDownload") from None
    blobs: list[_FullBlob] = []
    digests: set[str] = set()
    for request in entries:
        if len(blobs) == _MAX_DOWNLOADS:
            raise ValueError("Batch downloads exceed the 100000 request limit")
        if not isinstance(request, BlobDownload):
            raise ValueError("Batch entries must be BlobDownload")
        blob = _full_blob(repo, request, policy)
        if blob.digest in digests:
            raise ValueError("Batch downloads require distinct digests")
        digests.add(blob.digest)
        blobs.append(blob)
    _sinks(blobs)
    results: dict[str, BlobInfo] = {}
    summary: list[DownloadStats] = []

    with _coordinated(
        client, policy, progress_callback=progress_callback,
        download_status_callback=download_status_callback, summary=summary,
    ) as (_, coordinator):
        coordinator.run(
            [[_job(blob)] for blob in blobs],
            on_result=lambda info: results.__setitem__(info.digest, info),
        )
    return BlobBatchResult(
        tuple(results[blob.digest] for blob in blobs), summary[0],
    )

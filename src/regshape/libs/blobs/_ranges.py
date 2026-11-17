"""Negotiate real byte ranges and assemble bounded private spools in hash order.

Workers own response/spool lifetimes. The invoking coordinator alone commits
contiguous bytes, resets retained outputs and publishes verified metadata.
"""

import hashlib
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import requests

from regshape.libs._http import ClosingResponse, close_response, report_secondary_failure
from regshape.libs.blobs._download_worker import (
    _FullBlob, _content_length, _decimal, _retry_delay,
)
from regshape.libs.blobs.download import DownloadOptions
from regshape.libs.errors import AuthError, BlobError
from regshape.libs.manifests._stream import entity_chunks
from regshape.libs.manifests.download import _DownloadFile
from regshape.libs.models.blob import BlobInfo
from regshape.libs.progress import TransferPhase
from regshape.libs.transport._coordinator import (
    _Coordinator, _DownloadCancelled, _Job, _Status, _StrategyFallback, _Worker,
)

_CONTENT_RANGE = re.compile(
    r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19}|\*)", re.IGNORECASE,
)
_UNSATISFIED_RANGE = re.compile(r"bytes \*/([0-9]{1,19})", re.IGNORECASE)
_STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"')


class _RangeScratch:
    """Own a private temporary directory; never recursively delete an unproven namespace."""

    def __init__(self, root: str | os.PathLike[str] | None) -> None:
        """Create beneath a trusted root only after all invocation inputs are preflighted."""
        self.root = str(Path(root or tempfile.gettempdir()).absolute())
        self.path = Path(tempfile.mkdtemp(prefix="regshape-ranges-", dir=self.root))
        self.identity = self.path.lstat()

    def verify(self) -> None:
        """Reject directory replacement before spool reads, creation or removal."""
        if not os.path.samestat(self.path.lstat(), self.identity):
            raise OSError("Range scratch directory was replaced")

    def close(self) -> None:
        """Remove only the proven empty directory after every range owner has stopped."""
        self.verify()
        self.path.rmdir()


class _RangedBlob:
    """Retain one ordered hash/output and one replaceable logical range attempt.

    The first real range is a scheduling barrier. Later disjoint parts share
    the coordinator's C slots and W committed-prefix window, including spools
    that completed out of order.
    """

    def __init__(self, full: _FullBlob, options: DownloadOptions) -> None:
        """Borrow prevalidated receipt/output state without acquiring files or sockets."""
        self.full, self.options = full, options
        self.total = full.total_size
        self.etag: str | None = None
        self.content_type = "application/octet-stream"
        self.scratch: _RangeScratch | None = None
        self.cancelled = threading.Event()
        self.hasher = hashlib.new(full.algorithm)
        self.accepted = 0
        self.finished_full = False
        self.fallen_back = False

    def jobs(self) -> Iterator[_Job["_RangePart"]]:
        """Negotiate range zero before lazily filling the bounded consecutive window."""
        self.scratch = _RangeScratch(self.options.scratch_dir)
        first = _RangePart(self, 0, self.options.range_size - 1, first=True)
        yield first.job()
        if self.finished_full:
            return
        if self.total is None:
            raise RuntimeError("Range negotiation did not establish a size")
        for start in range(self.options.range_size, self.total, self.options.range_size):
            end = min(start + self.options.range_size, self.total) - 1
            yield _RangePart(self, start, end).job()

    def fallback(self, reason: str) -> Iterable[_Job["_RangePart"]]:
        """Reset a drained sink/hash and return exactly one fresh unconditional full GET."""
        if self.fallen_back:
            raise BlobError("Blob already used its one range strategy fallback")
        self.full.output.reset()
        self.hasher = hashlib.new(self.full.algorithm)
        self.accepted = 0
        self.fallen_back = True
        self.full.attempt += 1
        self.full.reason = reason
        if self.total is not None:
            self.full.total_size = self.total
        part = _RangePart(self, 0, 0, full=True)
        return (part.job(),)

    def finalize(self, accepted: bool) -> None:
        """Close/remove failed owned output and then the now-empty identity-checked scratch."""
        primary: BaseException | None = None
        try:
            self.full.output.cleanup(accepted)
        except BaseException as error:
            primary = error
        try:
            if self.scratch is not None:
                self.scratch.close()
        except BaseException as error:
            if primary is None:
                raise
            report_secondary_failure(primary, error, "Range scratch cleanup")
        if primary is not None:
            raise primary


class _RangePart:
    """Own one bounded spool, or a full-response result after capability fallback."""

    def __init__(
        self, blob: _RangedBlob, start: int, end: int, *,
        first: bool = False, full: bool = False,
    ) -> None:
        """Retain client-generated offsets and private state; registry strings never name files."""
        self.blob, self.start, self.end = blob, start, end
        self.first, self.full = first, full
        self.spool: _DownloadFile | None = None
        self.info: BlobInfo | None = None
        self.receiving = False

    def job(self) -> _Job["_RangePart"]:
        """Reserve scratch until ordered consumption, with a first-range negotiation barrier."""
        blob = self.blob
        return _Job(
            blob.full.transfer_id, blob.full.digest, self.run, self.cleanup,
            scratch_bytes=0 if self.full else self.end - self.start + 1,
            attempt=blob.full.attempt, strategy="full" if self.full else "ranged",
            reason=blob.full.reason if self.full else "range_enabled",
            terminal=self.full or not self.first and self.end + 1 == blob.total,
            validate_result=self.validate, barrier=self.first,
            cancelled=None if self.full else blob.cancelled,
            fallback=None if self.full else blob.fallback,
            finalize=blob.finalize,
            scratch_root=blob.scratch.root if blob.scratch is not None else None,
        )

    def validate(self) -> None:
        """Revalidate the closed worker result after observer delivery, before assembly/publication."""
        if self.info is not None:
            self.blob.full.output.verify_identity()
        elif self.spool is not None:
            self._scratch().verify()
            self.spool.verify_identity()

    def _scratch(self) -> _RangeScratch:
        """Require caller-created scratch instead of trusting a worker-supplied path."""
        if self.blob.scratch is None:
            raise RuntimeError("Range scratch is not open")
        return self.blob.scratch

    def cleanup(self, accepted: bool) -> None:
        """Remove this owner's proven spool after response close, on consumption, discard or retry."""
        if self.spool is None:
            return
        primary: BaseException | None = None
        try:
            self.spool.close()
        except BaseException as error:
            primary = error
        try:
            self._scratch().verify()
            self.spool._remove_failed()
            self.spool = None
        except BaseException as error:
            if primary is None:
                raise
            report_secondary_failure(primary, error, "Range spool removal")
        if primary is not None:
            raise primary

    def _headers(self, response: requests.Response) -> str | None:
        """Validate 206 metadata before accepting or negotiating a part.

        :returns: A fixed capability-fallback reason, or None for a usable binding.
        :raises BlobError: For independently malformed or contradictory metadata,
            even when the response also presents a changed validator.
        """
        headers = response.headers
        if headers.get("Content-Type", "").lower().startswith("multipart/"):
            raise BlobError("Multipart blob ranges are not supported")
        raw = headers.get("Content-Range", "")
        match = _CONTENT_RANGE.fullmatch(raw.strip()) if len(raw) <= 80 else None
        if match is None:
            raise BlobError("Malformed blob Content-Range")
        start, end = _decimal(match[1]), _decimal(match[2])
        total = None if match[3] == "*" else _decimal(match[3])
        length = _content_length(headers)
        if (
            start > end or start < self.start or end > self.end
            or total is not None and end >= total
            or length is not None and length != end - start + 1
        ):
            raise BlobError("Contradictory blob range bounds or length")
        blob = self.blob
        if total is not None:
            trusted = blob.full.total_size if self.first else blob.total
            if trusted is not None and total != trusted:
                raise BlobError("Blob range contradicts its trusted size")
            if blob.full.max_bytes is not None and total > blob.full.max_bytes:
                raise BlobError("Blob exceeds max_bytes")
        encoding = headers.get("Content-Encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            if self.first:
                return "nonidentity_encoding"
            raise BlobError("Blob range content coding changed")
        etag = headers.get("ETag", "")
        if len(etag) > 4096 or _STRONG_ETAG.fullmatch(etag) is None:
            if self.first:
                return "no_strong_validator"
            raise BlobError("Blob range has no valid strong ETag")
        if total is None:
            if self.first:
                return "unknown_size"
            raise BlobError("Blob range lost its complete length")
        if not self.first and etag != blob.etag:
            return "etag_changed"
        expected_end = min(self.end, total - 1)
        if start != self.start or end != expected_end:
            return "short_range"
        if self.first:
            blob.total = total
            blob.etag = etag
            blob.content_type = headers.get("Content-Type", "application/octet-stream")
            if total < 4 * blob.options.range_size:
                return "small_blob"
        return None

    def _error(self, worker: _Worker, response: requests.Response) -> None:
        """Read bounded registry error metadata, preserving existing authentication/domain taxonomy."""
        from regshape.libs.blobs.operations import _raise_for_blob_error
        from regshape.libs.manifests.operations import _BufferedManifestText

        payload = bytearray()
        chunks = iter(entity_chunks(response, self.blob.full.chunk_size))
        while True:
            worker.check_cancelled()
            self.receiving = True
            chunk = next(chunks, None)
            self.receiving = False
            if chunk is None:
                break
            if len(payload) + len(chunk) > 65_536:
                raise BlobError("Registry error body exceeds metadata limit")
            payload.extend(chunk)
        buffered = _BufferedManifestText(bytes(payload), response.encoding)
        buffered.status_code = response.status_code
        _raise_for_blob_error(
            buffered, worker.owner.transport.borrowed.config.registry,
            self.blob.full.repo, self.blob.full.digest,
        )
        raise BlobError("Blob range requires HTTP 206", status_code=response.status_code)

    def _receive(self, worker: _Worker, response: requests.Response) -> None:
        """Write exactly the declared identity bytes without committing any out-of-order prefix."""
        reason = self._headers(response)
        if reason is not None:
            raise _StrategyFallback(reason)
        scratch = self._scratch()
        scratch.verify()
        self.spool = _DownloadFile(scratch.path / uuid4().hex)
        self.spool.__enter__()
        size = 0
        limit = min(self.end + 1, self.blob.total or 0) - self.start
        chunks = iter(entity_chunks(response, self.blob.full.chunk_size))
        primary: BaseException | None = None
        try:
            while True:
                worker.check_cancelled()
                started = time.perf_counter()
                self.receiving = True
                chunk = next(chunks, None)
                self.receiving = False
                worker.stats = replace(
                    worker.stats,
                    receive_seconds=worker.stats.receive_seconds + time.perf_counter() - started,
                )
                if chunk is None:
                    break
                worker.stats = replace(
                    worker.stats, bytes_received=worker.stats.bytes_received + len(chunk),
                )
                worker.check_cancelled()
                if size + len(chunk) > limit:
                    raise BlobError("Blob range body exceeds its declared length")
                started = time.perf_counter()
                self.spool.write(chunk)
                worker.stats = replace(
                    worker.stats,
                    write_seconds=worker.stats.write_seconds + time.perf_counter() - started,
                )
                size += len(chunk)
            if size != limit:
                raise BlobError("Truncated blob range body")
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                self.spool.close()
            except BaseException as error:
                if primary is None:
                    raise
                report_secondary_failure(primary, error, "Range spool close")

    def run(self, worker: _Worker) -> "_RangePart":
        """Receive a part with one retry owner; reuse only a first ignored-Range 200.

        :param worker: Isolated client/counters and cancellation-aware event producer.
        :returns: This part with a closed validated spool or verified full response.
        :raises _StrategyFallback: For a capability limitation requiring caller-side drain/reset.
        :raises BlobError: For malformed protocol or failed size/digest verification.
        Transport/authentication/cancellation and spool errors propagate without
        treating sink or validation failures as retryable network failures.
        """
        if worker.client is None:
            raise RuntimeError("Range job requires an isolated client")
        blob = self.blob
        if self.full:
            self.info = blob.full.run(worker)
            return self
        from regshape.libs.blobs.operations import _bounded_blob_redirect

        policy = worker.owner.transport.retry_config
        headers = {
            "Range": f"bytes={self.start}-{self.end}", "Accept-Encoding": "identity",
        }
        if not self.first:
            if blob.etag is None:
                raise RuntimeError("Later ranges require an established validator")
            headers["If-Range"] = blob.etag
        if self.first:
            blob.full._progress(worker, TransferPhase.PREPARING, blob.total)
        for retry in range(policy.max_retries + 1):
            worker.check_cancelled()
            retry_response: requests.Response | None = None
            try:
                self.receiving = True
                response = worker.client.get(
                    f"/v2/{blob.full.repo}/blobs/{blob.full.digest}",
                    stream=True, headers=headers, response_hook=_bounded_blob_redirect,
                )
                self.receiving = False
                if response.status_code == 200 and self.first:
                    try:
                        if (
                            "Content-Range" in response.headers
                            or response.headers.get("Content-Type", "").lower().startswith("multipart/")
                        ):
                            raise BlobError("Ignored range has unexpected partial metadata")
                        worker.emit(_Status("fallback", "full", "range_ignored"))
                    except BaseException as error:
                        close_response(response, primary_error=error)
                        raise
                    self.info = blob.full.run(worker, initial_response=response)
                    blob.finished_full = True
                    return self
                with ClosingResponse(response):
                    if response.status_code in policy.status_codes and retry < policy.max_retries:
                        retry_response = response
                    elif response.status_code == 206:
                        self._receive(worker, response)
                        return self
                    elif response.status_code == 200:
                        declared = _content_length(response.headers)
                        if (
                            "Content-Range" in response.headers
                            or response.headers.get("Content-Type", "").lower().startswith("multipart/")
                            or response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity")
                            or declared is not None and declared != blob.total
                        ):
                            raise BlobError("Malformed late full blob response")
                        etag = response.headers.get("ETag")
                        raise _StrategyFallback(
                            "etag_changed" if etag is not None and etag != blob.etag
                            else "range_ignored",
                        )
                    elif response.status_code == 416:
                        raw = response.headers.get("Content-Range", "")
                        match = _UNSATISFIED_RANGE.fullmatch(raw.strip()) if len(raw) <= 80 else None
                        if self.first and match is not None and _decimal(match[1]) == 0:
                            if blob.total not in (None, 0):
                                raise BlobError("Zero-size range contradicts its trusted size")
                            blob.total = 0
                            raise _StrategyFallback("small_blob")
                        raise BlobError("Unsatisfiable blob range", status_code=416)
                    else:
                        self._error(worker, response)
            except (
                AuthError, BlobError, requests.exceptions.ContentDecodingError,
                _StrategyFallback, _DownloadCancelled,
            ):
                raise
            except requests.exceptions.ChunkedEncodingError as error:
                raise BlobError("Truncated or malformed blob range body") from error
            except policy.exceptions:
                if not self.receiving or retry >= policy.max_retries:
                    raise
            self.cleanup(False)
            worker.wait(_retry_delay(worker, retry, retry_response, "ranged"))
        raise RuntimeError("Range retry budget exhausted without a result")

    def consume(self, coordinator: _Coordinator) -> BlobInfo | None:
        """Commit this stopped part on the invoking thread, hashing only in original byte order.

        :returns: Verified metadata for the final part/full response, otherwise None.
        :raises BlobError: For changed part bounds or failed digest verification.
        :raises OSError: For an unproven spool/output identity or failed sink I/O.
        Observer exceptions retain their identity and stop assembly immediately.
        """
        if self.info is not None:
            return self.info
        blob = self.blob
        if self.spool is None or self.start != blob.accepted or blob.total is None:
            raise RuntimeError("Range assembly requires the exact committed prefix")
        blob.full.output.open()
        with self.spool.path.open("rb") as source:
            if self.spool.identity is None or not os.path.samestat(
                os.fstat(source.fileno()), self.spool.identity,
            ):
                raise OSError("Range spool was replaced before assembly")
            while True:
                chunk = source.read(blob.full.chunk_size)
                if not chunk:
                    break
                if blob.accepted + len(chunk) > min(self.end + 1, blob.total):
                    raise BlobError("Blob assembly exceeds its declared range or trusted size")
                started = time.perf_counter()
                blob.full.output.write(chunk)
                written = time.perf_counter() - started
                started = time.perf_counter()
                blob.hasher.update(chunk)
                hashed = time.perf_counter() - started
                blob.accepted += len(chunk)
                coordinator.accept(
                    blob.full.transfer_id, len(chunk), blob.total,
                    hash_seconds=hashed, write_seconds=written,
                )
        if blob.accepted != self.end + 1:
            raise BlobError("Blob range spool changed during assembly")
        if blob.accepted != blob.total:
            return None
        previous = coordinator.last_progress[blob.full.transfer_id]
        coordinator.last_progress[blob.full.transfer_id] = replace(
            previous, phase=TransferPhase.VERIFYING,
        )
        coordinator._call(coordinator.progress_callback, coordinator.last_progress[blob.full.transfer_id])
        if f"{blob.full.algorithm}:{blob.hasher.hexdigest()}" != blob.full.digest:
            blob.full.output.invalid_digest = True
            raise BlobError("Blob digest verification failed")
        blob.full.output.close()
        blob.full.output.verify_identity()
        return BlobInfo(blob.full.digest, blob.content_type, blob.accepted)

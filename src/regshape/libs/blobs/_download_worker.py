"""Verify full blobs in exclusive worker-owned sinks without central payload copying.

The invoking coordinator alone consumes observations, cleans discarded files and
performs shared store bookkeeping/publication. No complete-blob spool is created.
Once any file/hash byte commits, body failures cannot replay even in verify-only
mode. Ordinary unbounded outputs retain existing overwrite/partial-file semantics.
"""

import hashlib
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from requests.exceptions import ContentDecodingError

from regshape.libs._http import (
    ClosingResponse,
    download_total,
    report_secondary_failure,
)
from regshape.libs.errors import AuthError, BlobError
from regshape.libs.manifests._stream import entity_chunks
from regshape.libs.manifests.download import (
    _DownloadFile,
    _digest_algorithm,
    _validate_inputs,
)
from regshape.libs.models.blob import BlobInfo
from regshape.libs.progress import TransferDirection, TransferPhase, TransferProgress
from regshape.libs.transport._coordinator import _Job, _Status, _Worker
from regshape.libs.transport.middleware import RetryMiddleware
from regshape.libs.transport.models import RegistryResponse


class _BlobOutput:
    """Own one isolated optional sink; cleanup requires a retained file identity."""

    def __init__(self, path: str | None, bounded: bool, dir_fd: int | None) -> None:
        """Retain trusted caller paths without opening or truncating them."""
        self.path = Path(path) if path is not None else None
        self.bounded = bounded
        self.dir_fd = dir_fd
        self.exclusive: _DownloadFile | None = None
        self.file: BinaryIO | None = None
        self.identity: os.stat_result | None = None
        self.closed = False
        self.invalid_digest = False

    def open(self) -> None:
        """Open only after acceptable response headers; never reopen on a zero-byte retry."""
        if self.path is None or self.file is not None:
            return
        if self.bounded:
            self.exclusive = _DownloadFile(self.path, self.dir_fd)
            self.exclusive.__enter__()
            self.file = self.exclusive.file
            self.identity = self.exclusive.identity
        else:
            self.file = self.path.open("wb")
            self.identity = os.fstat(self.file.fileno())

    def write(self, chunk: bytes) -> None:
        """Write completely to the isolated sink, or discard when this is verify-only."""
        if self.file is not None and self.file.write(chunk) != len(chunk):
            raise OSError("Short write while saving blob")

    def verify_identity(self) -> None:
        """Reject a replaced destination; never use/remove an unproven replacement."""
        if self.path is None:
            return
        if self.identity is None:
            raise OSError("Blob output identity was not established")
        current = os.stat(self.path, dir_fd=self.dir_fd, follow_symlinks=False)
        if not os.path.samestat(current, self.identity):
            raise OSError("Blob output was replaced; refusing to use or remove it")

    def close(self) -> None:
        """Finalize the worker's handle once; close errors are operation failures."""
        if self.file is not None and not self.closed:
            self.closed = True
            self.file.close()

    def cleanup(self, accepted: bool) -> None:
        """Called only after joining; remove failed bounded/invalid outputs by identity.

        Ordinary transport/disk/callback failures retain unbounded partial files.
        A digest mismatch removes this invocation's invalid file even after overwrite.
        Removal errors propagate to the coordinator's secondary-error reporting.
        """
        primary: BaseException | None = None
        try:
            self.close()
        except BaseException as error:
            primary = error
        if self.file is not None and (
            self.invalid_digest or self.bounded and not accepted
        ):
            try:
                self.verify_identity()
                if self.path is not None:
                    os.unlink(self.path, dir_fd=self.dir_fd)
                    self.file = None
            except BaseException as secondary:
                if primary is None:
                    raise
                report_secondary_failure(primary, secondary, "Blob output removal")
        if primary is not None:
            raise primary


class _FullBlob:
    """One validated, single-use full-stream job reusable by blob/artifact facades.

    Inputs follow singular receive semantics; max_bytes is checked before each
    write/hash. A bounded verify-only receive needs no saved output. No shared
    ContentStore or SafeDirectory object enters the worker.
    """

    def __init__(
        self,
        repo: str,
        digest: str,
        output_path: str | None = None,
        chunk_size: int = 65_536,
        *,
        total_size: int | None = None,
        max_bytes: int | None = None,
        output_dir_fd: int | None = None,
        transfer_id: str | None = None,
        attempt: int = 1,
        reason: str | None = None,
    ) -> None:
        """Validate all receive inputs before network/filesystem effects.

        :param total_size: Trusted entity size, including zero, or None.
        :param max_bytes: Optional positive pre-write entity cap.
        :param output_dir_fd: Borrowed safe parent for a bounded single filename.
        :param transfer_id: Stable identity across an explicit reset/fallback.
        :param attempt: Logical attempt, not the pre-commit transport retry number.
        :raises BlobError: For an unsupported/malformed digest or impossible byte budget.
        :raises ValueError: For invalid sizes, repository, path or attempt.
        """
        try:
            self.algorithm = _digest_algorithm(digest)
        except ValueError as error:
            raise BlobError("Unsupported or malformed blob digest") from error
        _validate_inputs(repo, digest, "application/octet-stream", None, None, 65_536)
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        if total_size is not None and (type(total_size) is not int or total_size < 0):
            raise ValueError("total_size must be a nonnegative integer or None")
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise ValueError("max_bytes must be a positive integer or None")
        if max_bytes is not None and total_size is not None and total_size > max_bytes:
            raise BlobError("Blob exceeds max_bytes")
        if output_dir_fd is not None and (max_bytes is None or output_path is None):
            raise ValueError("output_dir_fd requires bounded file output")
        if output_path is not None and (not output_path or "\x00" in output_path):
            raise ValueError("Invalid blob output path")
        if output_dir_fd is not None and output_path is not None:
            _DownloadFile(Path(output_path), output_dir_fd)
        self.repo, self.digest = repo, digest
        self.chunk_size = min(chunk_size, 65_536)
        self.total_size, self.max_bytes = total_size, max_bytes
        self.transfer_id = transfer_id or uuid4().hex
        self.attempt, self.reason = attempt, reason
        self.output = _BlobOutput(output_path, max_bytes is not None, output_dir_fd)
        self.committed = False
        self.started = False
        self.job()

    def job(self) -> _Job[BlobInfo]:
        """Describe zero-spool work with invoking-thread identity validation and cleanup."""
        return _Job(
            self.transfer_id,
            self.digest,
            self.run,
            self.output.cleanup,
            attempt=self.attempt,
            reason=self.reason,
            terminal=True,
            validate_result=self.output.verify_identity,
        )

    def _progress(
        self, worker: _Worker, phase: TransferPhase, total: int | None
    ) -> None:
        """Queue the stable row identity/attempt with current accepted bytes."""
        worker.emit(
            TransferProgress(
                self.transfer_id,
                self.digest,
                TransferDirection.DOWNLOAD,
                phase,
                worker.stats.bytes_accepted,
                total,
                self.attempt,
            )
        )

    def run(self, worker: _Worker) -> BlobInfo:
        """Receive/hash/write locally, retrying only before any payload commitment.

        The worker closes the response and sink before returning a verified result.
        Configured retry exceptions apply only to request/body receive failures;
        authentication, sink, validation and cleanup failures are never retried.
        The caller later performs identity-safe failed-output cleanup. Cancellation
        is checked between reads, not by a fictitious whole-read deadline.
        """
        if self.started or worker.client is None:
            raise RuntimeError("Full blob jobs require one isolated worker execution")
        self.started = True
        from regshape.libs.blobs.operations import (
            _bounded_blob_redirect,
            _raise_for_blob_error,
        )
        from regshape.libs.manifests.operations import _BufferedManifestText

        client = worker.client
        policy = worker.owner.transport.retry_config
        hasher = hashlib.new(self.algorithm)
        self._progress(worker, TransferPhase.PREPARING, self.total_size)
        retryable = policy.exceptions
        primary: BaseException | None = None
        try:
            for retry in range(policy.max_retries + 1):
                worker.check_cancelled()
                status_code: int | None = None
                retry_after = 0.0
                closing = False
                body_finished = False
                receiving = False
                try:
                    receiving = True
                    response = client.get(
                        f"/v2/{self.repo}/blobs/{self.digest}",
                        stream=True,
                        headers={"Accept-Encoding": "identity"},
                        response_hook=_bounded_blob_redirect,
                    )
                    receiving = False
                    with ClosingResponse(response):
                        status_code = response.status_code
                        if (
                            status_code in policy.status_codes
                            and retry < policy.max_retries
                        ):
                            closing = True
                        else:
                            if (
                                status_code != 200
                                or "Content-Range" in response.headers
                            ):
                                if (
                                    status_code == 206
                                    or "Content-Range" in response.headers
                                ):
                                    raise BlobError(
                                        "Unexpected partial full-blob response"
                                    )
                                payload = bytearray()
                                error_limit = min(self.max_bytes or 65_536, 65_536)
                                error_chunks = iter(
                                    entity_chunks(
                                        response, min(error_limit + 1, 65_536)
                                    )
                                )
                                while True:
                                    receiving = True
                                    error_chunk = next(error_chunks, None)
                                    receiving = False
                                    if error_chunk is None:
                                        break
                                    worker.check_cancelled()
                                    if len(payload) + len(error_chunk) > error_limit:
                                        raise BlobError(
                                            "Registry error body exceeds metadata limit"
                                        )
                                    payload.extend(error_chunk)
                                buffered = _BufferedManifestText(
                                    bytes(payload), response.encoding
                                )
                                buffered.status_code = status_code
                                _raise_for_blob_error(
                                    buffered,
                                    client.config.registry,
                                    self.repo,
                                    self.digest,
                                )
                                raise BlobError(
                                    "Full blob receive requires HTTP 200",
                                    status_code=status_code,
                                )
                            total = self.total_size
                            if total is None:
                                total = download_total(response.headers)
                            if (
                                self.max_bytes is not None
                                and total is not None
                                and total > self.max_bytes
                            ):
                                raise BlobError("Blob exceeds max_bytes")
                            self.output.open()
                            self._progress(worker, TransferPhase.TRANSFERRING, total)
                            chunks = iter(entity_chunks(response, self.chunk_size))
                            while True:
                                worker.check_cancelled()
                                received_at = time.perf_counter()
                                try:
                                    receiving = True
                                    chunk = next(chunks, None)
                                    receiving = False
                                finally:
                                    worker.stats = replace(
                                        worker.stats,
                                        receive_seconds=worker.stats.receive_seconds
                                        + time.perf_counter()
                                        - received_at,
                                    )
                                if chunk is None:
                                    break
                                if not chunk:
                                    continue
                                worker.stats = replace(
                                    worker.stats,
                                    bytes_received=worker.stats.bytes_received
                                    + len(chunk),
                                )
                                worker.check_cancelled()
                                size = worker.stats.bytes_accepted + len(chunk)
                                if self.max_bytes is not None and size > self.max_bytes:
                                    raise BlobError("Blob exceeds max_bytes")
                                if total is not None and size > total:
                                    raise BlobError("Blob exceeds its trusted size")
                                self.committed = True
                                if self.output.file is not None:
                                    written_at = time.perf_counter()
                                    self.output.write(chunk)
                                    worker.stats = replace(
                                        worker.stats,
                                        write_seconds=worker.stats.write_seconds
                                        + time.perf_counter()
                                        - written_at,
                                    )
                                hashed_at = time.perf_counter()
                                hasher.update(chunk)
                                worker.stats = replace(
                                    worker.stats,
                                    hash_seconds=worker.stats.hash_seconds
                                    + time.perf_counter()
                                    - hashed_at,
                                    bytes_accepted=size,
                                )
                                self._progress(
                                    worker, TransferPhase.TRANSFERRING, total
                                )
                            if (
                                total is not None
                                and worker.stats.bytes_accepted != total
                            ):
                                raise BlobError("Blob does not match its trusted size")
                            self._progress(worker, TransferPhase.VERIFYING, total)
                            if f"{self.algorithm}:{hasher.hexdigest()}" != self.digest:
                                self.output.invalid_digest = True
                                raise BlobError("Blob digest verification failed")
                            body_finished = True
                            closing = True
                    if body_finished:
                        worker.check_cancelled()
                        self.output.close()
                        if self.output.path is not None:
                            self.output.verify_identity()
                        info = BlobInfo(
                            self.digest,
                            response.headers.get(
                                "Content-Type", "application/octet-stream"
                            ),
                            worker.stats.bytes_accepted,
                        )
                        return info
                    # Parse server delay only after the discarded response releases its slot.
                    retry_after = (
                        (
                            RetryMiddleware._retry_after(
                                RegistryResponse.from_requests_response(
                                    response, stream=True
                                ),
                            )
                            or 0.0
                        )
                        if status_code in (429, 503)
                        else 0.0
                    )
                except (AuthError, BlobError, ContentDecodingError):
                    raise
                except retryable:
                    if (
                        not receiving
                        or self.committed
                        or closing
                        or body_finished
                        or retry >= policy.max_retries
                    ):
                        raise
                    status_code = None
                worker.stats = replace(
                    worker.stats, retry_count=worker.stats.retry_count + 1
                )
                worker.emit(
                    _Status(
                        "retry",
                        "full",
                        "http_status" if status_code is not None else "transport_error",
                        status_code,
                    )
                )
                self._progress(worker, TransferPhase.RETRYING, self.total_size)
                worker.wait(max(policy.backoff_factor * 2**retry, retry_after or 0.0))
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                self.output.close()
            except BaseException as secondary:
                if primary is None:
                    raise
                report_secondary_failure(primary, secondary, "Blob worker output close")
        raise RuntimeError("Full blob retry budget exhausted without a result")

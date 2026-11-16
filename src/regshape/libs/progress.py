"""Immutable transfer observations, independent of terminal rendering and telemetry.

Blob events require a validated digest; manifest events use opaque invocation
identity without implying digest verification. Neither contains request URLs,
headers, credentials, payloads, or exception messages.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Literal, final
from uuid import uuid4

from regshape.libs._http import report_secondary_failure
from regshape.libs.errors import BlobError

logger = logging.getLogger(__name__)
_DIGEST_RE = re.compile(r"(?:sha256:[a-f0-9]{64}|sha512:[a-f0-9]{128})")


class TransferDirection(str, Enum):
    """Direction of payload movement relative to the caller."""

    DOWNLOAD = "download"
    UPLOAD = "upload"


class TransferPhase(str, Enum):
    """Transfer lifecycle states; completed requires successful verification."""

    PREPARING = "preparing"
    TRANSFERRING = "transferring"
    RETRYING = "retrying"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TransferProgress:
    """One immutable observation, not an instruction to redraw the terminal.

    :param transfer_id: Opaque identity shared by events from one invocation.
    :param digest: Valid SHA-256 or SHA-512 blob digest.
    :param direction: Upload or download relative to the caller.
    :param phase: Current lifecycle phase.
    :param bytes_completed: Absolute processed/accepted bytes, not a delta.
    :param total_bytes: Trusted payload size, or ``None`` when unknown.
    :param attempt: Logical transfer attempt, starting at one. Transport-internal
        retries of a buffered chunk are not separate logical attempts.
    :raises ValueError: If identity, counts, or bounds are invalid.
    :raises TypeError: If direction or phase is not the declared enum type.
    """

    transfer_id: str
    digest: str
    direction: TransferDirection
    phase: TransferPhase
    bytes_completed: int
    total_bytes: int | None
    attempt: int = 1

    def __post_init__(self) -> None:
        """Validate public observations without echoing potentially sensitive input."""
        if not isinstance(self.transfer_id, str) or not self.transfer_id:
            raise ValueError("Progress requires a non-empty transfer identity")
        if not isinstance(self.digest, str) or not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("Progress requires a valid SHA-256 or SHA-512 digest")
        if not isinstance(self.direction, TransferDirection) or not isinstance(self.phase, TransferPhase):
            raise TypeError("Progress requires a valid direction and phase")
        for value in (self.bytes_completed, self.attempt):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Progress counters must be non-negative integers")
        if self.attempt < 1:
            raise ValueError("Progress attempts start at one")
        if self.total_bytes is not None:
            if isinstance(self.total_bytes, bool) or not isinstance(self.total_bytes, int) or self.total_bytes < 0:
                raise ValueError("Progress total must be a non-negative integer or None")
            if self.bytes_completed > self.total_bytes:
                raise ValueError("Progress cannot exceed its trusted total")


ProgressCallback = Callable[[TransferProgress], None]


class ManifestKind(str, Enum):
    """Advertised media-type classification, not validation of the payload."""

    UNKNOWN = "unknown"
    MANIFEST = "manifest"
    INDEX = "index"


class ManifestPhase(str, Enum):
    """Receive lifecycle; RECEIVED does not imply parsing, verification or output success."""

    PREPARING = "preparing"
    TRANSFERRING = "transferring"
    RECEIVED = "received"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ManifestProgress:
    """Immutable manifest receipt observation, independent of tags and digest headers.

    :param transfer_id: Nonempty opaque identity, stable for one fetch.
    :param kind: Advertised manifest/index kind, or UNKNOWN.
    :param phase: Receive lifecycle state.
    :param bytes_completed: Absolute consumed payload bytes before text decoding.
    :param total_bytes: Comparable advisory size, or None; never below the count.
    :raises ValueError: For invalid identity, enum values or integer bounds.
    """

    transfer_id: str
    kind: ManifestKind
    phase: ManifestPhase
    bytes_completed: int
    total_bytes: int | None

    def __post_init__(self) -> None:
        """Validate snapshots without including potentially sensitive values in errors."""
        if not isinstance(self.transfer_id, str) or not self.transfer_id:
            raise ValueError("Manifest progress requires a non-empty transfer identity")
        if not isinstance(self.kind, ManifestKind) or not isinstance(self.phase, ManifestPhase):
            raise ValueError("Manifest progress requires a valid kind and phase")  # noqa: TRY004 - manifest event contract
        counters = (
            (self.bytes_completed,) if self.total_bytes is None
            else (self.bytes_completed, self.total_bytes)
        )
        for value in counters:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Manifest progress counters must be non-negative integers")
        if self.total_bytes is not None and self.bytes_completed > self.total_bytes:
            raise ValueError("Manifest progress cannot exceed its advisory total")


ManifestProgressCallback = Callable[[ManifestProgress], None]


@final
class _ManifestProgress:
    """Track one fetch and disable notifications after an observer raises."""

    def __init__(self, callback: ManifestProgressCallback | None) -> None:
        """Validate the optional callback before network access; None remains silent."""
        if callback is not None and not callable(callback):
            raise TypeError("Manifest progress callback must be callable")
        self.callback = callback
        self.transfer_id = uuid4().hex if callback is not None else ""
        self.kind = ManifestKind.UNKNOWN
        self.bytes_completed = 0
        self.total_bytes: int | None = None
        self.callback_failed = False

    def __enter__(self) -> "_ManifestProgress":
        """Emit preparation before issuing the request."""
        self.emit(ManifestPhase.PREPARING)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Notify failure once while preserving transport, callback and cancellation errors."""
        if exc is not None and not self.callback_failed:
            phase = (
                ManifestPhase.CANCELLED
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit))
                else ManifestPhase.FAILED
            )
            try:
                self.emit(phase)
            except BaseException as observer_error:  # noqa: BLE001 - preserve primary error
                report_secondary_failure(exc, observer_error, "Manifest failure observer")
        return False

    def emit(self, phase: ManifestPhase) -> None:
        """Deliver an immutable snapshot synchronously; never recursively notify a failed observer."""
        if self.callback is None or self.callback_failed:
            return
        event = ManifestProgress(
            self.transfer_id, self.kind, phase, self.bytes_completed, self.total_bytes,
        )
        delivered = False
        try:
            self.callback(event)
            delivered = True
        finally:
            if not delivered:
                self.callback_failed = True

    def discard_total(self) -> None:
        """Invalidate inconsistent metadata once, before publishing a misleading bound."""
        self.total_bytes = None
        logger.warning("Manifest length metadata does not match consumed bytes; progress total is unknown")

    def advance(self, amount: int) -> None:
        """Count a consumed chunk, discarding an advisory total before an overrun event."""
        self.bytes_completed += amount
        if self.total_bytes is not None and self.bytes_completed > self.total_bytes:
            self.discard_total()
        self.emit(ManifestPhase.TRANSFERRING)

    def finish_body(self) -> None:
        """Discard a short size hint after successful EOF, not after a transport failure."""
        if self.total_bytes is not None and self.bytes_completed != self.total_bytes:
            self.discard_total()


@final
class _TransferProgress:
    """Track one operation and preserve primary errors if a failure observer fails.

    Callers explicitly mark successful completion only after all checks and
    cleanup succeed. A callback exception disables further notifications and
    propagates unchanged; it is not recursively reported to the same callback.
    """

    def __init__(
        self,
        digest: str,
        direction: TransferDirection,
        callback: ProgressCallback | None,
        total_bytes: int | None,
    ) -> None:
        """Validate opt-in inputs before requests; absent callbacks remain silent."""
        if callback is not None and not callable(callback):
            raise TypeError("Progress callback must be callable")
        if callback is not None and (
            not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest)
        ):
            raise BlobError("Progress requires a valid SHA-256 or SHA-512 digest")
        self.digest = digest
        self.direction = direction
        self.callback = callback
        self.transfer_id = uuid4().hex if callback is not None else ""
        self.bytes_completed = 0
        self.attempt = 1
        self.total_bytes: int | None = None
        self.callback_failed = False
        self._terminal = False
        self.set_total(total_bytes)

    def set_total(self, total_bytes: int | None) -> None:
        """Accept a trusted total, rejecting invalid input rather than clamping it."""
        if total_bytes is not None and (
            isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0
        ):
            raise BlobError("Transfer total_size must be a non-negative integer or None")
        self.total_bytes = total_bytes

    def __enter__(self) -> "_TransferProgress":
        """Notify preparation before network access; observer failure aborts immediately."""
        self.emit(TransferPhase.PREPARING)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Report failure/cancellation once, never replacing an existing exception."""
        if exc is not None and not self._terminal and not self.callback_failed:
            self._terminal = True
            phase = (
                TransferPhase.CANCELLED
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit))
                else TransferPhase.FAILED
            )
            try:
                self.emit(phase)
            except BaseException as observer_error:  # noqa: BLE001 - preserve the primary failure
                # Only a secondary observer error is handled here; the primary
                # exception is propagated by returning False.
                logger.error(
                    "Progress observer failed during failure notification (%s); "
                    "preserving the original transfer error",
                    type(observer_error).__name__,
                )
        return False

    def emit(self, phase: TransferPhase) -> None:
        """Notify an observer synchronously and disable it if it raises."""
        if self.callback is None or self.callback_failed:
            return
        event = TransferProgress(
            self.transfer_id, self.digest, self.direction, phase,
            self.bytes_completed, self.total_bytes,
            self.attempt,
        )
        delivered = False
        try:
            self.callback(event)
            delivered = True
        finally:
            if not delivered:
                self.callback_failed = True

    def check_increment(self, amount: int) -> None:
        """Reject an oversized chunk before accepting it; retain the last valid count."""
        if amount < 0:
            raise BlobError("Transfer progress cannot move backwards")
        if self.total_bytes is not None and self.bytes_completed + amount > self.total_bytes:
            raise BlobError("Transfer byte count exceeds the trusted total_size")

    def advance(self, amount: int) -> None:
        """Record one processed download chunk or successfully accepted upload chunk."""
        self.check_increment(amount)
        self.bytes_completed += amount
        self.emit(TransferPhase.TRANSFERRING)

    def retry(self) -> None:
        """Replace an unconfirmed whole-body attempt, never accumulate retransmissions."""
        self.bytes_completed = 0
        self.attempt += 1
        self.emit(TransferPhase.RETRYING)

    def check_complete(self) -> None:
        """Reject a short transfer before verification/commit is reported successful."""
        if self.total_bytes is not None and self.bytes_completed != self.total_bytes:
            raise BlobError("Transfer byte count does not match the trusted total_size")

    def complete(self) -> None:
        """Emit successful completion once; a failing terminal observer still propagates."""
        self.check_complete()
        self._terminal = True
        self.emit(TransferPhase.COMPLETED)

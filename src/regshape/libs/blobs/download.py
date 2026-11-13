"""Define immutable download policy, batch inputs/results and safe observations.

Scheduling and receipt belong to the batch/coordinator layers. These records
contain no request URLs, headers, payloads, credentials or exception messages.
Backend selection performs no credential lookup, network request or write.
"""

import logging
import math
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

from regshape.libs.models.blob import BlobInfo
from regshape.libs.progress import TransferDirection, TransferPhase, TransferProgress
from regshape.libs.transport.client import RegistryClient

logger = logging.getLogger(__name__)

DownloadEvent = Literal[
    "strategy", "fallback", "retry", "completed", "failed", "cancelled", "summary"
]
DownloadStrategy = Literal["full", "ranged"]
DownloadReason = Literal[
    "sequential", "ranges_disabled", "range_enabled", "small_blob", "unknown_size",
    "no_strong_validator", "range_ignored", "nonidentity_encoding", "short_range",
    "etag_changed", "http_status", "transport_error",
]


@dataclass(frozen=True)
class BlobDownload:
    """Describe one batch receive; all entries are validated before acquisition.

    :param digest: Strict lowercase SHA-256/SHA-512 content address.
    :param output_path: Trusted standalone filename, or None to hash/discard.
    :param total_size: Optional trusted entity length, including zero.
    :param max_bytes: Optional positive pre-write cap; saved output is exclusive.
    :param output_dir_fd: Borrowed safe directory for a bounded single filename.
    :param chunk_size: Positive streaming buffer bytes, not HTTP range size.

    Ownership, overwrite and partial-file rules match get_blob. Frozen requests
    do not reserve paths or copy/close borrowed directory handles.
    """

    digest: str
    output_path: str | None = None
    total_size: int | None = None
    max_bytes: int | None = None
    output_dir_fd: int | None = None
    chunk_size: int = 65_536


@dataclass(frozen=True)
class DownloadStats:
    """Snapshot of one blob or aggregate operation, independent of verification.

    Counts and seconds are nonnegative. Physical requests include auth/redirects;
    received bytes count consumed blob entities, including discarded attempts,
    not wire traffic. Accepted bytes describe the current committed attempt and
    may reset on fallback. Peaks never decrease within a transfer. Work/wait
    durations sum across jobs and can exceed elapsed wall time.
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

    def __post_init__(self) -> None:
        """Reject booleans, negative counters and nonfinite durations."""
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("_seconds"):
                valid = (
                    type(value) in (int, float) and value >= 0
                    and (type(value) is int or math.isfinite(value))
                )
            else:
                valid = type(value) is int and value >= 0
            if not valid:
                raise ValueError("Download statistics require nonnegative finite values")


@dataclass(frozen=True)
class DownloadStatus:
    """Safe caller-thread strategy, retry or lifecycle snapshot.

    :param transfer_id: Blob identity matching progress, or None for summary.
    :param digest: Strict blob address, or None for summary.
    :param event: Fixed lifecycle code; completed requires verification/cleanup.
    :param strategy: Selected full/ranged strategy, or None before selection.
    :param reason: Fixed safe reason; etag_changed survives verified completion.
    :param http_status: Optional HTTP code, never registry text.
    :param attempt: Logical attempt starting at one, or None for summary.
    :param stats: Frozen per-blob or aggregate counters.
    :param scratch_root: Effective caller-owned scratch path, or None if unused.
    :raises ValueError: For invalid identity, codes, attempt or statistics.

    Failure events do not replace the original raised exception. Observers may
    raise to cancel; the exact exception propagates after workers drain.
    """

    transfer_id: str | None
    digest: str | None
    event: DownloadEvent
    strategy: DownloadStrategy | None
    reason: DownloadReason | None
    http_status: int | None
    attempt: int | None
    stats: DownloadStats
    scratch_root: str | None = None

    def __post_init__(self) -> None:
        """Validate machine-readable fields without echoing untrusted input."""
        if self.event not in (
            "strategy", "fallback", "retry", "completed", "failed", "cancelled", "summary"
        ) or self.strategy not in (None, "full", "ranged"):
            raise ValueError("Invalid download status")
        if self.reason not in (
            None, "sequential", "ranges_disabled", "range_enabled", "small_blob",
            "unknown_size", "no_strong_validator", "range_ignored",
            "nonidentity_encoding", "short_range", "etag_changed", "http_status",
            "transport_error",
        ):
            raise ValueError("Invalid download reason")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("Invalid download HTTP status")
        if not isinstance(self.stats, DownloadStats):
            raise ValueError("Download status requires DownloadStats")
        if self.scratch_root is not None and not isinstance(self.scratch_root, str):
            raise ValueError("Invalid download scratch root")
        if self.event == "summary":
            if any(value is not None for value in (self.transfer_id, self.digest, self.attempt)):
                raise ValueError("Download summary has no blob identity")
        else:
            if self.transfer_id is None or self.digest is None or self.attempt is None:
                raise ValueError("Download blob status requires identity and attempt")
            TransferProgress(
                self.transfer_id, self.digest, TransferDirection.DOWNLOAD,
                TransferPhase.PREPARING, 0, None, self.attempt,
            )


@dataclass(frozen=True)
class BlobBatchResult:
    """All verified BlobInfo records in input order, with aggregate final stats.

    No instance is returned on partial failure. Earlier successful standalone
    files may remain: batch download is not atomic multi-file publication.
    BlobInfo retains its existing schema and mutability.
    """

    blobs: tuple[BlobInfo, ...]
    stats: DownloadStats


@dataclass(frozen=True)
class DownloadOptions:
    """Policy shared by singular, artifact and batch downloads.

    :param concurrency: Global request ceiling, 1..32 (default 4).
    :param ranges_per_blob: Per-blob ceiling, 1..concurrency (default 1).
    :param range_size: Range bytes, 64 KiB..64 MiB (default 8 MiB).
    :param max_retries: Pooled retry override, 0..10; None preserves an explicit
        client retry policy or selects three modern retries.
    :param scratch_dir: Existing caller-trusted temporary directory, or None.
        Validation checks existence, not the caller's trust precondition.
    :param transport: Pooled by default; legacy requires serial ceilings,
        no retry override and no scratch directory.
    :raises ValueError: For invalid types, bounds, paths or relationships.
    """

    concurrency: int = 4
    ranges_per_blob: int = 1
    range_size: int = 8_388_608
    max_retries: int | None = None
    scratch_dir: str | os.PathLike[str] | None = None
    transport: Literal["pooled", "legacy"] = "pooled"

    def __post_init__(self) -> None:
        """Validate and pin a supplied scratch path without creating any resources."""
        self.validate()
        if self.scratch_dir is not None:
            object.__setattr__(self, "scratch_dir", Path(self.scratch_dir).absolute())

    def validate(self) -> None:
        """Recheck bounds and scratch existence without mutating this immutable policy."""
        for name, value, lower, upper in (
            ("concurrency", self.concurrency, 1, 32),
            ("ranges_per_blob", self.ranges_per_blob, 1, self.concurrency),
            ("range_size", self.range_size, 65_536, 67_108_864),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer in {lower}..{upper}")
        if self.max_retries is not None and (
            type(self.max_retries) is not int or not 0 <= self.max_retries <= 10
        ):
            raise ValueError("max_retries must be None or an integer in 0..10")
        if self.transport not in ("pooled", "legacy"):
            raise ValueError("transport must be pooled or legacy")
        if self.transport == "legacy" and (
            self.concurrency != 1 or self.ranges_per_blob != 1
            or self.max_retries is not None or self.scratch_dir is not None
        ):
            raise ValueError("legacy requires C=W=1, max_retries=None and scratch_dir=None")
        if self.scratch_dir is not None:
            try:
                path = Path(self.scratch_dir)
            except TypeError:
                raise ValueError("scratch_dir must be a directory path") from None
            if not path.is_dir():
                raise ValueError("scratch_dir must be an existing caller-trusted directory")

    @classmethod
    def sequential(cls) -> "DownloadOptions":
        """Return serial scheduling on modern pooled transport, not a transport rollback."""
        return cls(concurrency=1, ranges_per_blob=1)

    @classmethod
    def legacy(cls) -> "DownloadOptions":
        """Return the original borrowed-client serial transport with no new resources."""
        return cls(concurrency=1, ranges_per_blob=1, transport="legacy")


def resolve_download_options(
    client: RegistryClient,
    options: DownloadOptions | None,
    *,
    requires_pooled: bool = False,
) -> DownloadOptions:
    """Preflight backend support without constructing a pool or resolving credentials.

    Omitted options preserve unsupported existing custom callers with one logged
    notice. Explicit pooled or new batch/status consumers instead fail closed.
    Nested calls inherit their operation's policy and check the borrowed client's
    fork contract, not custom state already installed on an isolated worker.

    :param client: Borrowed transport or existing custom singular/artifact client.
    :param options: Explicit immutable policy, or None for compatibility selection.
    :param requires_pooled: True for consumers without legacy compatibility.
    :returns: Validated effective policy.
    :raises ValueError: For unsupported customization or invalid legacy combinations.
    """
    if options is not None and not isinstance(options, DownloadOptions):
        raise ValueError("download_options must be DownloadOptions or None")
    from regshape.libs.transport.download import DownloadTransport

    context = getattr(client, "_download_context", None)
    if isinstance(context, DownloadTransport):
        if options is not None and options != context.options:
            raise ValueError("Nested downloads must use their operation's download policy")
        client = context.borrowed
        policy = context.options
    else:
        policy = options or DownloadOptions()
    # Recheck paths if the directory was removed after policy construction.
    policy.validate()
    if policy.transport == "legacy":
        if requires_pooled:
            raise ValueError("legacy transport does not support batch/status downloads")
        return policy
    supported = issubclass(type(client), RegistryClient) and client.supports_download_fork()
    if supported:
        return policy
    if options is not None or requires_pooled:
        raise ValueError("Pooled downloads require an explicit safe fork_for_download override")
    logger.warning("Custom download client retained on original serial transport")
    return DownloadOptions.legacy()

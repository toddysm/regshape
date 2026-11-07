"""Validate immutable download policy and select a backend before acquisition.

Scheduling, range receipt and public batch/status facades are separate layers.
This module performs no credential lookup, network request or filesystem write.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from regshape.libs.transport.client import RegistryClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadOptions:
    """Policy shared by singular, artifact and future batch downloads.

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

    :param client: Borrowed transport or existing custom singular/artifact client.
    :param options: Explicit immutable policy, or None for compatibility selection.
    :param requires_pooled: True for consumers without legacy compatibility.
    :returns: Validated effective policy.
    :raises ValueError: For unsupported customization or invalid legacy combinations.
    """
    if options is not None and not isinstance(options, DownloadOptions):
        raise ValueError("download_options must be DownloadOptions or None")
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

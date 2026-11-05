"""Terminal-free artifact policy, observations and completed-pull results.

Limits are independent positive integer budgets, not estimates of available
resources. Archive input, header, expanded-stream and metadata caps are separate.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from enum import Enum

from regshape.libs.models.descriptor import Descriptor

_DIGEST = re.compile(r"(sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128})")


def digest_parts(digest: str) -> tuple[str, str]:
    """Return safe algorithm/hex path components or raise ValueError, without echoing input."""
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError("Digest must be lowercase sha256:64-hex or sha512:128-hex")
    algorithm, _, encoded = digest.partition(":")
    return algorithm, encoded


class ArtifactFormat(str, Enum):
    """Requested complete-closure output: OCI directory or single-member gzip/POSIX tar."""

    OCI = "oci"
    OCI_TAR_GZIP = "oci-tar-gzip"


@dataclass(frozen=True)
class ArtifactLimits:
    """Independent byte/count/depth caps shared by strict artifact operations.

    Depth starts at zero for each selected root, excluding the local wrapper.
    Total bytes count distinct content addresses, not generated layout metadata.
    Every value must be a positive, non-boolean integer.
    """

    max_manifest_bytes: int = 16 * 1024 * 1024
    max_layout_marker_bytes: int = 64 * 1024
    max_objects: int = 100_000
    max_edges: int = 500_000
    max_depth: int = 64
    max_total_bytes: int = 1024**4
    max_json_depth: int = 128
    max_archive_bytes: int = 2 * 1024**4
    max_archive_input_bytes: int = 2 * 1024**4
    max_gzip_header_bytes: int = 64 * 1024
    max_archive_entries: int = 200_010
    max_archive_metadata_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        """Reject invalid budgets before any filesystem or network operation."""
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")


@dataclass(frozen=True)
class ArtifactProgress:
    """Immutable context around an existing manifest/blob transfer observation.

    Identity survives root kind/digest discovery. Counts are absolute bytes for
    one attempt, and embedded/local counts are not network traffic. Role is the
    first encountered config/layer role, not a second content identity.
    """

    transfer_id: str
    kind: str
    phase: str
    digest: str | None
    role: str | None
    bytes_completed: int
    total_bytes: int | None
    attempt: int = 1
    source: str = "network"

    def __post_init__(self) -> None:
        """Raise ValueError for unsupported states or inconsistent byte counters."""
        if not isinstance(self.transfer_id, str) or not self.transfer_id:
            raise ValueError("Artifact progress needs a nonempty identity")
        if self.kind not in ("unknown", "index", "manifest", "blob"):
            raise ValueError("Invalid artifact kind")
        if self.phase not in (
            "preparing", "transferring", "received", "verifying", "stored",
            "skipped", "failed", "cancelled",
        ):
            raise ValueError("Invalid artifact phase")
        if self.digest is not None:
            digest_parts(self.digest)
        if self.role not in (None, "config", "layer"):
            raise ValueError("Invalid artifact role")
        if self.source not in ("network", "embedded", "local"):
            raise ValueError("Invalid artifact source")
        counters = [self.bytes_completed, self.attempt]
        if self.total_bytes is not None:
            counters.append(self.total_bytes)
        for value in counters:
            if type(value) is not int or value < 0:
                raise ValueError("Artifact counters must be nonnegative integers")
        if self.attempt < 1:
            raise ValueError("Artifact attempts start at one")
        if self.total_bytes is not None and self.bytes_completed > self.total_bytes:
            raise ValueError("Artifact bytes exceed total")


ArtifactProgressCallback = Callable[[ArtifactProgress], None]


@dataclass(frozen=True)
class ArtifactPullResult:
    """Completed publication, never returned for incomplete or failed output.

    Object counts are unique addresses; reused_references counts repeated edges.
    Byte counts exclude generated metadata and unsuccessful retry bodies.
    The record is frozen; root uses the existing mutable Descriptor value type.
    """

    source: str
    resolved_reference: str
    root: Descriptor
    output_path: str
    output_format: ArtifactFormat
    indexes: int
    manifests: int
    blobs: int
    downloaded_objects: int
    embedded_objects: int
    reused_references: int
    bytes_downloaded: int
    bytes_stored: int

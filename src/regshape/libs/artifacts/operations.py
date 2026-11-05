"""Download complete artifact graphs into safely published OCI directories.

The client is borrowed, observers are synchronous and terminal-free, and a
successful result means publication completed. Archive output and push are not
implemented here. Snapshots expose verified private files for those later layers.
"""

import json
import re
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Literal, final
from urllib.parse import urlsplit
from uuid import uuid4

from regshape.libs._http import report_secondary_failure
from regshape.libs.artifacts.graph import (
    ACCEPT,
    INDEX_TYPES,
    OCI_INDEX,
    REF_NAME,
    Edge,
    VerifiedGraph,
    layout_roots,
    parse_document,
    verify_graph,
)
from regshape.libs.artifacts.models import (
    ArtifactFormat,
    ArtifactLimits,
    ArtifactProgress,
    ArtifactProgressCallback,
    ArtifactPullResult,
)
from regshape.libs.artifacts.storage import (
    INCOMPLETE,
    ContentStore,
    SafeDirectory,
    require_safe_filesystem,
)
from regshape.libs.blobs import get_blob
from regshape.libs.errors import ArtifactError
from regshape.libs.manifests import download_manifest
from regshape.libs.manifests.download import _validate_inputs
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.progress import ManifestProgress, TransferProgress
from regshape.libs.transport import RegistryClient

_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def validate_tag(value: str) -> str:
    """Return a literal valid ASCII publication tag, rejecting rather than sanitizing."""
    if not isinstance(value, str) or _TAG.fullmatch(value) is None:
        raise ArtifactError("Publication tag must match the registry ASCII tag grammar")
    return value


def _registry(client: RegistryClient) -> str:
    """Validate the borrowed client's registry authority without credentials or network calls."""
    registry = client.config.registry
    if (
        not isinstance(registry, str)
        or any(c.isspace() or ord(c) < 32 for c in registry)
        or any(c in registry for c in "/\\?#%@")
    ):
        raise ValueError("Invalid registry authority")
    parsed = urlsplit("//" + registry)
    if (
        not parsed.hostname or parsed.username is not None or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
    ):
        raise ValueError("Registry must be an authority without URL/userinfo components")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Invalid registry port") from error
    return registry


class _Observation:
    """Mutable invocation-local state that emits only frozen public observations."""

    def __init__(
        self, owner: "_Observers", *, digest: str | None = None,
        kind: str = "unknown", role: str | None = None, source: str = "network",
    ) -> None:
        """Reserve a stable logical identity before an unresolved-root request."""
        self.owner = owner
        self.identity = uuid4().hex
        self.digest = digest
        self.kind = kind
        self.role = role
        self.source = source
        self.phase = "preparing"
        self.count = 0
        self.total: int | None = None
        self.attempt = 1

    def emit(self, phase: str) -> None:
        """Synchronously deliver a snapshot, permanently disabling a failed observer."""
        self.phase = phase
        self.owner.current = self
        callback = self.owner.callback
        if callback is None or self.owner.failed:
            return
        event = ArtifactProgress(
            self.identity, self.kind, phase, self.digest, self.role,
            self.count, self.total, self.attempt, self.source,
        )
        delivered = False
        try:
            callback(event)
            delivered = True
        finally:
            if not delivered:
                self.owner.failed = True

    def manifest(self, event: ManifestProgress) -> None:
        """Adapt byte receipt without allocating another identity for discovered root metadata."""
        self.kind = event.kind.value
        self.count, self.total = event.bytes_completed, event.total_bytes
        self.emit(event.phase.value)

    def blob(self, event: TransferProgress) -> None:
        """Adapt verified blob receipt; stored success belongs to graph/storage verification."""
        self.count, self.total = event.bytes_completed, event.total_bytes
        self.attempt = event.attempt
        phase = {"completed": "received", "retrying": "transferring"}.get(
            event.phase.value, event.phase.value,
        )
        self.emit(phase)


class _Observers:
    """Own digest-to-row identity and primary-safe failure notification for one pull."""

    def __init__(self, callback: ArtifactProgressCallback | None) -> None:
        """Reject a non-callable observer before any filesystem or network effect."""
        if callback is not None and not callable(callback):
            raise TypeError("Artifact progress callback must be callable")
        self.callback = callback
        self.failed = False
        self.rows: dict[str, _Observation] = {}
        self.root = _Observation(self)
        self.current = self.root

    def row(self, edge: Edge, source: str = "network") -> _Observation:
        """Reuse the first role/identity associated with a unique digest."""
        if edge.digest not in self.rows:
            self.rows[edge.digest] = _Observation(
                self, digest=edge.digest, role=edge.role, source=source,
                kind=("index" if edge.media_type in INDEX_TYPES else "manifest") if edge.document else "blob",
            )
        return self.rows[edge.digest]

    def observe(self, edge: Edge, source: str, phase: str) -> None:
        """Emit local verification/storage after receipt with a truthful known total."""
        row = self.row(edge, source)
        row.source = source
        row.kind = ("index" if edge.media_type in INDEX_TYPES else "manifest") if edge.document else "blob"
        row.count = edge.size
        row.total = edge.size
        row.emit(phase)

    def failure(self, primary: BaseException) -> None:
        """Notify failure once without mutating/replacing immutable caller exceptions."""
        if self.failed or self.current.phase in ("failed", "cancelled", "stored"):
            return
        phase = "cancelled" if isinstance(primary, (KeyboardInterrupt, SystemExit, GeneratorExit)) else "failed"
        try:
            self.current.emit(phase)
        except BaseException as secondary:  # noqa: BLE001 - preserve primary observer/transport error
            report_secondary_failure(primary, secondary, "Artifact failure observer")


def _publish(
    parent: SafeDirectory, staging: SafeDirectory, name: str, wrapper: bytes,
) -> None:
    """Reserve exclusively, publish through owned handles, and remove the marker last.

    The complete output is retained after commit even if later handle cleanup or
    caller diagnostics fail. No atomic whole-directory visibility is promised.
    """
    with parent.mkdir(name) as destination:
        destination.write_bytes(INCOMPLETE, b"Artifact publication incomplete\n")
        staging.move_child("blobs", destination)
        destination.write_bytes("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        destination.write_bytes(".index-pending", wrapper)
        destination.move_file(".index-pending", destination, "index.json")
        staging.verify()
        destination.verify()
        destination.complete(INCOMPLETE)


def pull_artifact(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    output_format: ArtifactFormat = ArtifactFormat.OCI,
    limits: ArtifactLimits | None = None,
    progress_callback: ArtifactProgressCallback | None = None,
) -> ArtifactPullResult:
    """Pull every platform/subject/config/layer into a new OCI layout directory.

    :param client: Borrowed configured transport; no credential lookup occurs here.
    :param repo: Strict relative registry repository.
    :param reference: A literal tag or supported SHA-256/SHA-512 digest, fetched once.
    :param output_path: New destination with an existing no-symlink parent.
    :param output_format: OCI only in this implementation; archives fail preflight.
    :param limits: Independent policy caps; defaults apply when omitted.
    :param progress_callback: Optional synchronous observer; errors propagate unchanged.
    :returns: Frozen completed-publication record with the original root identity.
    :raises ArtifactError: On unsupported storage, invalid graph or exhausted budgets.
    :raises ValueError: On invalid inputs, before network or filesystem writes.

    Auth/manifest/blob/transport errors retain their taxonomy. Filesystem errors
    propagate as OSError. Owned partial output is cleaned where identity permits;
    cleanup failures report retained staging without masking the primary failure.
    """
    policy = limits if limits is not None else ArtifactLimits()
    if not isinstance(policy, ArtifactLimits):
        raise ValueError("limits must be ArtifactLimits")  # noqa: TRY004 - public input contract
    observers = _Observers(progress_callback)
    _validate_inputs(repo, reference, ACCEPT, None, None, policy.max_manifest_bytes)
    registry = _registry(client)
    if output_format != ArtifactFormat.OCI:
        raise ArtifactError("Only OCI directory output is implemented")
    marker = b'{"imageLayoutVersion":"1.0.0"}'
    if len(marker) > policy.max_layout_marker_bytes:
        raise ArtifactError("Generated oci-layout exceeds max_layout_marker_bytes")
    require_safe_filesystem()
    output = Path(output_path).absolute()
    if output.name in ("", ".", ".."):
        raise ValueError("Output must name a new directory")
    try:
        with SafeDirectory.open(output.parent) as parent:
            if parent.exists(output.name):
                raise ArtifactError("Artifact destination already exists")
            with parent.temporary() as staging:
                store = ContentStore(staging, writable=True)
                root_file = "root-" + uuid4().hex
                received = download_manifest(
                    client, repo, reference, ACCEPT, root_file,
                    max_bytes=min(policy.max_manifest_bytes, policy.max_total_bytes),
                    progress_callback=observers.root.manifest, output_dir_fd=staging.fd,
                )
                staging.register(root_file)
                parsed_root = parse_document(
                    staging.read_bytes(root_file, policy.max_manifest_bytes), policy,
                    header=received.media_type,
                )
                root = Edge(received.digest, received.size, parsed_root.media_type, True)
                observers.root.digest = root.digest
                observers.rows[root.digest] = observers.root
                store.promote(root_file, root.digest)

                def acquire(edge: Edge, remaining: int) -> tuple[str, str]:
                    """Receive one reserved unique object into private handle-relative storage."""
                    if edge.digest == root.digest:
                        return "network", received.media_type
                    row = observers.row(edge)
                    filename = "receive-" + uuid4().hex
                    header = ""
                    if edge.document:
                        result = download_manifest(
                            client, repo, edge.digest, ACCEPT, filename,
                            expected_digest=edge.digest, expected_size=edge.size,
                            max_bytes=min(policy.max_manifest_bytes, remaining),
                            progress_callback=row.manifest, output_dir_fd=staging.fd,
                        )
                        header = result.media_type
                    else:
                        get_blob(
                            client, repo, edge.digest, filename, total_size=edge.size,
                            max_bytes=max(1, min(edge.size, remaining)),
                            output_dir_fd=staging.fd, progress_callback=row.blob,
                        )
                    staging.register(filename)
                    store.promote(filename, edge.digest)
                    return "network", header

                graph = verify_graph(
                    (root,), store, policy, acquire=acquire,
                    observe=observers.observe, embedded_missing=True,
                )
                root_descriptor = Descriptor(
                    root.media_type, root.digest, root.size,
                    artifact_type=parsed_root.artifact_type,
                )
                if ":" not in reference:
                    root_descriptor.annotations = {REF_NAME: validate_tag(reference)}
                wrapper = json.dumps({
                    "schemaVersion": 2, "mediaType": OCI_INDEX,
                    "manifests": [root_descriptor.to_dict()],
                }, separators=(",", ":")).encode("utf-8")
                if len(wrapper) > policy.max_manifest_bytes:
                    raise ArtifactError("Generated wrapper exceeds max_manifest_bytes")
                parse_document(wrapper, policy, media_type=OCI_INDEX)
                _publish(parent, staging, output.name, wrapper)
                result = ArtifactPullResult(
                    source=f"{registry}/{repo}{'@' if ':' in reference else ':'}{reference}",
                    resolved_reference=f"{registry}/{repo}@{root.digest}",
                    root=root_descriptor, output_path=str(output), output_format=ArtifactFormat.OCI,
                    indexes=sum(item.kind == "index" for item in graph.objects),
                    manifests=sum(item.kind == "manifest" for item in graph.objects),
                    blobs=sum(item.kind == "blob" for item in graph.objects),
                    downloaded_objects=sum(item.source == "network" for item in graph.objects),
                    embedded_objects=sum(item.source == "embedded" for item in graph.objects),
                    reused_references=graph.reused_references,
                    bytes_downloaded=sum(item.size for item in graph.objects if item.source == "network"),
                    bytes_stored=graph.bytes_stored,
                )
            return result
    except BaseException as primary:
        observers.failure(primary)
        raise


@final
class DirectorySnapshot:
    """Context-managed private verified directory snapshot for archive/push consumers.

    Enter reads source metadata once and copies only reachable regular files.
    The source is closed before returning; consumers must read store, never
    reopen source paths. Exit removes the private copy and closes handles.
    """

    def __init__(self, path: str | Path, *, limits: ArtifactLimits | None = None) -> None:
        """Retain input/policy without effects; preparation happens on context entry."""
        self.path = path
        self.limits = limits if limits is not None else ArtifactLimits()
        if not isinstance(self.limits, ArtifactLimits):
            raise ValueError("limits must be ArtifactLimits")  # noqa: TRY004 - public input contract
        self.parent: SafeDirectory | None = None
        self.directory: SafeDirectory | None = None
        self.store: ContentStore
        self.graph: VerifiedGraph
        self.wrapper_bytes: bytes

    def __enter__(self) -> "DirectorySnapshot":
        """Build a bounded verified copy before any consumer can perform registry writes."""
        require_safe_filesystem()
        try:
            with SafeDirectory.open(self.path) as source:
                self.wrapper_bytes, roots = layout_roots(source, self.limits)
                self.parent = SafeDirectory.open(Path(tempfile.gettempdir()).resolve())
                self.directory = self.parent.temporary()
                self.store = ContentStore(self.directory, writable=True)
                source_store = ContentStore(source)

                def acquire(edge: Edge, remaining: int) -> tuple[str, str]:
                    """Copy a reserved source object; the graph hashes its private copy."""
                    self.store.copy_from(source_store, edge.digest, edge.size)
                    return "local", ""

                self.graph = verify_graph(roots, self.store, self.limits, acquire=acquire)
                if source.exists(INCOMPLETE):
                    raise ArtifactError("Source became incomplete during snapshot")
                source.verify()
                self.directory.write_bytes("index.json", self.wrapper_bytes)
                self.directory.write_bytes("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
            return self
        except BaseException as primary:
            self.__exit__(type(primary), primary, primary.__traceback__)
            raise

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Remove only owned snapshot entries, preserving a consumer's primary failure."""
        primary = exc
        try:
            if self.directory is not None:
                self.directory.__exit__(exc_type, exc, traceback)
        except BaseException as error:
            primary = error
            raise
        finally:
            if self.parent is not None:
                self.parent.close(primary)
        return False

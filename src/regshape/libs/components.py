"""Select one config or ordered layer from a pinned manifest and publish exact bytes.

The SDK borrows its client and reuses strict artifact parsing, shared blob
dispatch and identity-aware native storage. It never traverses indexes/subjects,
uses embedded data/URLs, interprets payloads or imports CLI frameworks.
"""

from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

from regshape.libs.artifacts.graph import INDEX_TYPES, MANIFEST_TYPES, Edge, parse_document, strict_json
from regshape.libs.artifacts.models import ArtifactLimits, ArtifactPhase, ArtifactPhaseCallback
from regshape.libs.artifacts.operations import _registry
from regshape.libs.artifacts.storage import SafeDirectory, require_archive_publication
from regshape.libs.blobs.batch import _coordinated, _guard_observer
from regshape.libs.blobs.download import DownloadOptions, DownloadStatus, resolve_download_options
from regshape.libs.blobs.operations import _get_blob_with_context
from regshape.libs.errors import ComponentError
from regshape.libs.manifests import DEFAULT_MANIFEST_ACCEPT
from regshape.libs.manifests.download import _download_manifest_with_context, _validate_inputs
from regshape.libs.progress import (
    ManifestProgressCallback, ProgressCallback, TransferPhase, TransferProgress,
)
from regshape.libs.refs import format_ref, reject_unsafe_reference
from regshape.libs.transport import RegistryClient

T = TypeVar("T")
Component = Literal["config", "layer"]
_EXISTING_OUTPUT = "Component destination already exists; choose a new output file"


@dataclass(frozen=True)
class ComponentDownloadResult:
    """Completed native publication with immutable scalar metadata only.

    source retains the supplied tag/digest; resolved_reference pins the verified
    manifest. size is exact blob bytes, media_type is descriptor metadata only,
    and output_path is absolute. No result is returned after any late failure,
    although a completely published file remains caller-owned.
    """

    source: str
    resolved_reference: str
    component: Component
    layer_index: int | None
    digest: str
    size: int
    media_type: str
    output_path: str


def _inputs(
    registry: str, repo: str, reference: str, output_path: str | Path,
    component: Component, layer_index: int | None, limits: ArtifactLimits | None,
) -> tuple[Path, ArtifactLimits]:
    """Validate source, selector, positive limits and file policy without effects.

    Component-specific validation raises ComponentError; invalid limits remain
    ValueError and unsupported native storage remains ArtifactError.
    """
    if component not in ("config", "layer"):
        raise ComponentError("component must be config or layer")
    if component == "layer":
        if type(layer_index) is not int or layer_index < 0:
            raise ComponentError("layer_index must be a nonnegative integer")
    elif layer_index is not None:
        raise ComponentError("Config selection does not accept layer_index")
    policy = limits if limits is not None else ArtifactLimits()
    if not isinstance(policy, ArtifactLimits):
        raise ValueError("limits must be ArtifactLimits")
    try:
        _registry(registry)
        _validate_inputs(repo, reference, DEFAULT_MANIFEST_ACCEPT, None, None, policy.max_manifest_bytes)
        reject_unsafe_reference(format_ref(registry, repo, reference))
    except ValueError as error:
        raise ComponentError("Invalid component source; use a registry, repository and tag or SHA digest") from error
    if not isinstance(output_path, (str, Path)) or str(output_path) in ("", "-"):
        raise ComponentError("Component output must name a new file; stdout is unsupported")
    output = Path(output_path).absolute()
    if output.name in ("", ".", "..") or "\x00" in str(output) or "\\" in output.as_posix():
        raise ComponentError("Component output must name a new file")
    require_archive_publication()
    return output, policy


def _owned_io(action: Callable[[], T], message: str) -> T:
    """Translate only a single operation-owned filesystem action, never a receiver/observer.

    Storage policy ArtifactError passes through; OSError is chained as the
    ComponentError cause. Cleanup contexts retain their own primary-safe policy.
    """
    try:
        return action()
    except OSError as error:
        raise ComponentError(message) from error


def preflight_component_download(
    registry: str, repo: str, reference: str, output_path: str | Path, *,
    component: Component, layer_index: int | None = None,
    limits: ArtifactLimits | None = None,
) -> None:
    """Check selection/source/output support before credentials or registry effects.

    Requires an absent file under a trusted, existing no-symlink parent on a
    supported native filesystem. A temporary private probe is removed on exit;
    preflight neither reserves the output nor creates credentials/client state.
    Download entry points repeat these checks under retained handles.

    :raises ComponentError: For source/index/file policy, an existing entry or
        operation-owned parent/probe I/O, with OSError causes preserved.
    :raises ArtifactError: For unsafe/unsupported native storage and cleanup policy.
    :raises ValueError: For invalid limits.
    """
    output, _ = _inputs(registry, repo, reference, output_path, component, layer_index, limits)
    with _owned_io(lambda: SafeDirectory.open(output.parent), "Cannot open component output parent") as parent:
        if _owned_io(lambda: parent.exists(output.name), "Cannot inspect component destination"):
            raise ComponentError(_EXISTING_OUTPUT)
        with _owned_io(lambda: SafeDirectory.private_namespace(parent), "Cannot prepare component staging"):
            pass


class _Observers:
    """Defer terminal blob/status success until publication and operation cleanup.

    All adapters share callable-failure guards; observations remain immutable and
    caller-thread. Receipt completion announces COMMITTING, not operation success.
    """

    def __init__(
        self, progress: ProgressCallback | None, manifest: ManifestProgressCallback | None,
        phase: ArtifactPhaseCallback | None, status: Callable[[DownloadStatus], None] | None,
    ) -> None:
        """Reject non-callables before effects and initialize invocation-local deferred records."""
        for callback in (progress, manifest, phase, status):
            if callback is not None and not callable(callback):
                raise TypeError("Component observers must be callable")
        failures: set[tuple[int, int]] = set()
        self.progress = _guard_observer(progress, failures)
        self.manifest = _guard_observer(manifest, failures)
        self.phase = _guard_observer(phase, failures)
        self.status = _guard_observer(status, failures)
        self.completed: TransferProgress | None = None
        self.statuses: list[DownloadStatus] = []

    def blob(self, event: TransferProgress) -> None:
        """Emit actual bytes and receipt COMMITTING; save terminal success without delivery."""
        if event.phase == TransferPhase.COMPLETED:
            self.completed = event
            event = replace(event, phase=TransferPhase.COMMITTING)
        if self.progress is not None:
            self.progress(event)

    def download_status(self, event: DownloadStatus) -> None:
        """Delay completed/summary records; preserve live strategy/retry/failure observations."""
        if event.event in ("completed", "summary"):
            self.statuses.append(event)
        elif self.status is not None:
            self.status(event)

    def finish(self) -> None:
        """Deliver terminal success after all owned contexts close; late errors retain output."""
        if self.completed is not None and self.progress is not None:
            self.progress(self.completed)
        if self.status is not None:
            for event in self.statuses:
                self.status(event)


def _select(raw: bytes, policy: ArtifactLimits, header: str, component: Component, index: int | None) -> Edge:
    """Strictly validate one simple manifest and select ordered role edges, excluding subjects.

    Missing config is a component selection error; other strict document and
    descriptor failures keep ArtifactError. Embedded/URL fields never supply bytes.
    """
    document = strict_json(raw, policy, policy.max_manifest_bytes)
    declared = document.get("mediaType", header.partition(";")[0].strip().lower())
    if declared in MANIFEST_TYPES and document.get("config") is None:
        raise ComponentError("Manifest has no config descriptor")
    parsed = parse_document(raw, policy, header=header)
    if parsed.media_type in INDEX_TYPES:
        raise ComponentError("Index targets are unsupported; select an explicit child manifest")
    if component == "config":
        return next(edge for edge in parsed.children if edge.role == "config")
    layers = tuple(edge for edge in parsed.children if edge.role == "layer")
    if index is None or index >= len(layers):
        raise ComponentError(f"layer_index is out of range; manifest has {len(layers)} layers")
    return layers[index]


def _publish(staging: SafeDirectory, name: str, parent: SafeDirectory, output: str) -> None:
    """Publish verified bytes without replacement; never remove the public name on any failure."""
    try:
        staging.publish_file(name, parent, output)
    except FileExistsError as error:
        raise ComponentError(_EXISTING_OUTPUT) from error
    except OSError as error:
        raise ComponentError("Cannot publish component output") from error


def _download(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    component: Component, layer_index: int | None, limits: ArtifactLimits | None,
    chunk_size: int, progress_callback: ProgressCallback | None,
    manifest_progress_callback: ManifestProgressCallback | None,
    phase_callback: ArtifactPhaseCallback | None,
    download_options: DownloadOptions | None,
    download_status_callback: Callable[[DownloadStatus], None] | None,
) -> ComponentDownloadResult:
    """Own one manifest/selected blob staging lifetime around borrowed transport.

    Budget reservation precedes blob receipt; transport teardown precedes public
    publication, so its failure still cleans staging. Only native publication
    transfers complete output to the caller. Receiver/callback errors are not wrapped.
    """
    observers = _Observers(
        progress_callback, manifest_progress_callback, phase_callback, download_status_callback,
    )
    policy = resolve_download_options(
        client, download_options, requires_pooled=download_status_callback is not None,
    )
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    output, budget = _inputs(
        client.config.registry, repo, reference, output_path, component, layer_index, limits,
    )
    with ExitStack() as owned:
        parent = owned.enter_context(_owned_io(
            lambda: SafeDirectory.open(output.parent), "Cannot open component output parent",
        ))
        if _owned_io(lambda: parent.exists(output.name), "Cannot inspect component destination"):
            raise ComponentError(_EXISTING_OUTPUT)
        namespace = owned.enter_context(_owned_io(
            lambda: SafeDirectory.private_namespace(parent), "Cannot prepare component staging",
        ))
        staging = owned.enter_context(_owned_io(namespace.temporary, "Cannot open component staging"))
        manifest_name, blob_name = "manifest-" + uuid4().hex, "blob-" + uuid4().hex
        acquisition = (
            nullcontext((client, None)) if policy.transport == "legacy"
            else _coordinated(
                client, policy,
                download_status_callback=observers.download_status if observers.status is not None else None,
            )
        )
        with acquisition as (worker, _):
            received = _download_manifest_with_context(
                worker, repo, reference, DEFAULT_MANIFEST_ACCEPT, manifest_name,
                max_bytes=min(budget.max_manifest_bytes, budget.max_total_bytes),
                progress_callback=observers.manifest, output_dir_fd=staging.fd,
                on_received=lambda receiver: _owned_io(
                    lambda: receiver.handoff(staging), "Cannot register received manifest",
                ),
            )
            raw = _owned_io(
                lambda: staging.read_bytes(manifest_name, budget.max_manifest_bytes),
                "Cannot read received manifest",
            )
            selected = _select(raw, budget, received.media_type, component, layer_index)
            reserved = 0 if selected.digest == received.digest else selected.size
            if reserved > budget.max_total_bytes - received.size:
                raise ComponentError("Selected component exceeds manifest-plus-blob max_total_bytes")
            _get_blob_with_context(
                worker, repo, selected.digest, blob_name, chunk_size,
                total_size=selected.size, max_bytes=max(1, selected.size),
                output_dir_fd=staging.fd, progress_callback=observers.blob,
                download_options=policy,
                staging=(staging, output.parent / namespace.name / staging.name),
            )
        if observers.phase is not None:
            observers.phase(ArtifactPhase.PUBLISHING)
        _publish(staging, blob_name, parent, output.name)
        result = ComponentDownloadResult(
            format_ref(client.config.registry, repo, reference),
            format_ref(client.config.registry, repo, received.digest),
            component, layer_index, selected.digest, selected.size, selected.media_type, str(output),
        )
    observers.finish()
    return result


def download_config(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    limits: ArtifactLimits | None = None, chunk_size: int = 65536,
    progress_callback: ProgressCallback | None = None,
    manifest_progress_callback: ManifestProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ComponentDownloadResult:
    """Save the exact config blob of one pinned simple manifest to a new file.

    :param client: Borrowed transport, never closed by this operation.
    :param repo: Strict relative OCI repository.
    :param reference: Tag or strict lowercase SHA-256/SHA-512 manifest digest.
    :param output_path: Absent file under a trusted supported Linux/macOS parent;
        stdout, existing entries and symlink parents are refused.
    :param limits: Positive artifact caps, counting unique manifest-plus-blob bytes.
    :param chunk_size: Positive non-boolean integer streaming bytes.
    :param progress_callback: Caller-thread immutable blob bytes; terminal completion
        follows publication/cleanup. Exceptions propagate unchanged.
    :param manifest_progress_callback: Caller-thread immutable single-manifest receipt.
    :param phase_callback: Publishing notification before the irreversible hard link.
    :param download_options: Shared pool/range/retry/legacy policy; explicit scratch wins.
    :param download_status_callback: Caller-thread immutable pooled status, with
        completed/summary delayed until operation cleanup; legacy is unsupported.
    :returns: Frozen scalar-only completed publication metadata.
    :raises ComponentError: On selection/output policy, total budget or owned I/O.
    :raises ArtifactError: On strict parsing, unsafe native storage or identity failure.
    :raises ValueError: On invalid limits, chunk size or download policy.
    :raises TypeError: On non-callable observers.

    Receiver auth/manifest/blob/transport/I/O and exact observer exceptions pass
    through. No embedded/URL fallback or transforms occur. Owned staging is
    removed by identity; secondary cleanup failures are reported without replacing
    the primary. Complete public output remains after any late error/cancellation.
    """
    return _download(
        client, repo, reference, output_path, component="config", layer_index=None,
        limits=limits, chunk_size=chunk_size, progress_callback=progress_callback,
        manifest_progress_callback=manifest_progress_callback, phase_callback=phase_callback,
        download_options=download_options, download_status_callback=download_status_callback,
    )


def download_layer(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    layer_index: int, limits: ArtifactLimits | None = None, chunk_size: int = 65536,
    progress_callback: ProgressCallback | None = None,
    manifest_progress_callback: ManifestProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ComponentDownloadResult:
    """Save the zero-based ordered layer of one pinned simple manifest, excluding subjects.

    :param layer_index: Required nonnegative integer, never coerced; layer 0 works
        for a single-layer artifact. Empty/out-of-range selections raise ComponentError.

    All other parameters, result, receiver errors, borrowed ownership, native
    publication, deferred observers and cleanup caveats match download_config.
    Bytes are opaque: no extraction, decompression, execution or format conversion.
    Invalid selection fails before effects when possible; indexes require an
    explicit child-manifest reference and never select a platform implicitly.
    """
    return _download(
        client, repo, reference, output_path, component="layer", layer_index=layer_index,
        limits=limits, chunk_size=chunk_size, progress_callback=progress_callback,
        manifest_progress_callback=manifest_progress_callback, phase_callback=phase_callback,
        download_options=download_options, download_status_callback=download_status_callback,
    )

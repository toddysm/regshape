"""Receive a reserved artifact plan using the shared blob/range coordinator.

Only isolated sink handles reach workers. The invoking thread routes progress,
registers and promotes verified files; final graph verification and publication
remain in artifact orchestration. This module owns no executor or transport.
"""

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from regshape.libs.artifacts.graph import Edge, GraphPlan, Observe
from regshape.libs.artifacts.storage import ContentStore
from regshape.libs.blobs import BlobDownload
from regshape.libs.blobs._ranges import _RangePart
from regshape.libs.blobs.batch import _consume, _coordinated, _full_blob, _source
from regshape.libs.models.blob import BlobInfo
from regshape.libs.progress import ProgressCallback, TransferPhase, TransferProgress
from regshape.libs.transport import RegistryClient
from regshape.libs.transport.download import DownloadTransport


def receive_graph_blobs(
    client: RegistryClient,
    repo: str,
    plan: GraphPlan,
    store: ContentStore,
    *,
    staging_path: Path,
    identity: Callable[[Edge], str],
    progress_callback: ProgressCallback,
    observe: Observe,
) -> None:
    """Receive each unique deferred blob after all graph limits have been reserved.

    :param client: Root worker borrowing the invocation's open coordinator.
    :param repo: Validated source repository.
    :param plan: Complete unverified discovery result, in dependency-first order.
    :param store: Private writable store; bookkeeping stays on the invoking thread.
    :param staging_path: Diagnostic/default range-scratch location for store.directory.
    :param identity: Reserve/retrieve the first artifact row's stable transfer ID.
    :param progress_callback: Caller-thread adapter routing by blob digest.
    :param observe: Caller-thread verifying observer after sink validation/promotion.
        Stored success remains deferred to final graph verification by the caller.
    :raises RuntimeError: If no invocation coordinator is available.

    Worker, observer and storage failures propagate after draining active jobs;
    no result, wrapper or public destination is produced here.
    """
    context = getattr(client, "_download_context", None)
    if not isinstance(context, DownloadTransport) or context.coordinator is None:
        raise RuntimeError("Artifact acquisition requires its invocation coordinator")
    staging = store.directory
    policy = context.options
    pending: dict[str, tuple[Edge, str]] = {}
    sources = []
    for item in plan.objects:
        if item.kind != "blob" or item.source != "network":
            continue
        edge = Edge(item.digest, item.size, item.media_type, False, item.role)
        filename = "receive-" + uuid4().hex
        blob = _full_blob(
            repo,
            BlobDownload(
                edge.digest,
                filename,
                edge.size,
                max(1, edge.size),
                staging.fd,
            ),
            policy,
            transfer_id=identity(edge),
        )
        pending[edge.digest] = edge, filename
        sources.append(_source(blob, policy, staging=(staging, staging_path)))

    verified: set[str] = set()

    def progress(event: TransferProgress) -> None:
        """Promote after source cleanup, leaving the row verifying until final graph validation."""
        if event.phase != TransferPhase.COMPLETED:
            progress_callback(event)
            return
        if event.digest not in verified:
            raise RuntimeError("Artifact completion requires a verified blob result")
        edge, filename = pending.pop(event.digest)
        staging.register(filename)
        store.promote(filename, edge.digest)
        progress_callback(event)
        observe(edge, "network", "verifying")

    with _coordinated(client, policy, progress_callback=progress) as (_, coordinator):

        def receive(result: BlobInfo | _RangePart) -> None:
            """Record a verified full blob/final part; promotion waits for source cleanup."""
            info = _consume(result, coordinator)
            if info is not None:
                verified.add(info.digest)

        coordinator.run(sources, on_result=receive)

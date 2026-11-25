"""Pure evidence-based summaries of an already inspected local layout.

Every index occurrence and source descriptor block is retained. Only contiguous
complete manifests are grouped; no filesystem, payload, registry or CLI access
occurs here. Storage totals borrow the inspector's distinct-address observations.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from regshape.libs.artifacts.graph import DOCKER_MANIFEST, OCI_MANIFEST
from regshape.libs.layout.inspection_models import (
    IndexNode,
    LayoutInspectionResult,
    LayoutSummaryResult,
    ManifestNode,
    OpaqueNode,
    SummaryCategory,
    SummaryGroup,
    SummaryIndexNode,
    SummaryLayoutNode,
    SummaryStatistics,
    UnknownNode,
)

_SIGNATURE = "application/vnd.cncf.notary.signature"
_LIFECYCLE = "application/vnd.microsoft.artifact.lifecycle"
_ATTESTATION = "application/vnd.in-toto+json"
_ATTESTATION_KEY = "vnd.docker.reference.type"
_IMAGE_CONFIGS = {
    OCI_MANIFEST: "application/vnd.oci.image.config.v1+json",
    DOCKER_MANIFEST: "application/vnd.docker.container.image.v1+json",
}


def _eligible(node: ManifestNode) -> bool:
    """Require a complete manifest subtree, keeping every error-bearing owner expanded."""
    descriptors = (node.incoming, node.subject, node.config, *(node.layers or ()))
    return (
        node.schema_version == 2
        and node.digest is not None
        and node.source_position is not None
        and node.config is not None
        and node.layers is not None
        and node.children is not None
        and len(node.children) == len(node.layers) + 1
        and not node.errors
        and not any(item.errors for item in descriptors if item is not None)
        and not any(item.errors for item in node.children)
    )


def _classify(node: ManifestNode) -> SummaryCategory:
    """Classify source evidence, never payloads, trust or an unknown/unknown platform.

    Conflicting descriptor/document declarations already carry inspection errors
    and remain expanded. An attestation annotation cannot override another own
    artifact type. Descriptor-only artifact types are not promoted to own fields.
    """
    incoming = node.incoming
    annotation = (
        incoming is not None
        and incoming.annotations is not None
        and incoming.annotations.get(_ATTESTATION_KEY) == "attestation-manifest"
    )
    own_type = node.artifact_type
    if annotation and own_type not in (None, _ATTESTATION):
        return "unclassified"
    if own_type == _SIGNATURE:
        return "signatures"
    if own_type == _LIFECYCLE:
        return "lifecycle"
    if own_type == _ATTESTATION or annotation:
        return "attestations"
    if own_type is not None:
        return "artifacts"
    if incoming is not None and incoming.artifact_type is None:
        platform = incoming.platform
        media_type = node.media_type or incoming.media_type
        if (
            media_type in _IMAGE_CONFIGS
            and node.config is not None
            and node.config.media_type == _IMAGE_CONFIGS[media_type]
            and platform is not None
            and all(
                isinstance(platform.get(key), str)
                and platform[key] not in ("", "unknown")
                for key in ("os", "architecture")
            )
        ):
            return "platform-images"
    return "unclassified"


def _group(
    members: list[ManifestNode],
    sizes: Mapping[str, int],
) -> SummaryGroup:
    """Count occurrence shapes and distinct contained addresses for one eligible run."""
    objects: set[str] = set()
    manifests: set[str] = set()
    distribution: dict[str, int] = {}
    artifact_types = {member.artifact_type for member in members}
    for member in members:
        digest = cast(str, member.digest)
        manifests.add(digest)
        objects.add(digest)
        objects.update(cast(str, child.digest) for child in member.children or ())
        count = str(len(member.layers or ()))
        distribution[count] = distribution.get(count, 0) + 1
    return SummaryGroup(
        category=_classify(members[0]),
        artifact_type=artifact_types.pop() if len(artifact_types) == 1 else None,
        positions=tuple(cast(int, member.source_position) for member in members),
        members=tuple(members),
        unique_manifests=len(manifests),
        config_references=len(members),
        layer_references=sum(len(member.layers or ()) for member in members),
        layer_count_distribution=MappingProxyType(distribution),
        unique_objects=len(objects),
        stored_bytes=sum(sizes[digest] for digest in objects),
    )


def _summarize_index(
    node: IndexNode,
    indexes: Mapping[int, SummaryIndexNode],
    sizes: Mapping[str, int],
) -> SummaryIndexNode:
    """Replace eligible contiguous runs without reordering or concealing failed branches."""
    children: list[
        SummaryIndexNode | ManifestNode | OpaqueNode | UnknownNode | SummaryGroup
    ] = []
    run: list[ManifestNode] = []
    key: tuple[SummaryCategory, str | None] | None = None
    for child in node.children or ():
        category = (
            _classify(child)
            if isinstance(child, ManifestNode) and _eligible(child)
            else None
        )
        current = (
            (category, child.artifact_type if category == "artifacts" else None)
            if category is not None and isinstance(child, ManifestNode)
            else None
        )
        if run and (
            current != key
            or child.source_position != cast(int, run[-1].source_position) + 1
        ):
            children.append(_group(run, sizes))
            run = []
        key = current
        if current is not None and isinstance(child, ManifestNode):
            run.append(child)
        else:
            children.append(
                indexes[id(child)] if isinstance(child, IndexNode) else child
            )
    if run:
        children.append(_group(run, sizes))
    return SummaryIndexNode(
        location=node.location,
        source=node.source,
        digest=node.digest,
        incoming=node.incoming,
        source_position=node.source_position,
        errors=node.errors,
        schema_version=node.schema_version,
        media_type=node.media_type,
        artifact_type=node.artifact_type,
        subject=node.subject,
        annotations=node.annotations,
        manifests=node.manifests,
        referrer_list=node.referrer_list,
        children=tuple(children) if node.children is not None else None,
    )


def summarize_layout(result: LayoutInspectionResult) -> LayoutSummaryResult:
    """Summarize an inspection in memory without IO, printing or expanded serialization.

    :param result: An SDK inspection result with cached storage/source-slot facts.
    :returns: All indexes, eligible manifest groups and observed totals; source errors,
        completeness and serialization budgets are preserved. Members retain full
        source evidence, including absent subjects/platform fields.
    :raises ValueError: If result is not a LayoutInspectionResult.
    """
    if not isinstance(result, LayoutInspectionResult):
        raise ValueError("result must be a LayoutInspectionResult")
    indexes: dict[int, SummaryIndexNode] = {}
    stack = [(node, False) for node in reversed(result.root.children)]
    while stack:
        node, visited = stack.pop()
        if not visited:
            stack.append((node, True))
            stack.extend(
                (child, False)
                for child in reversed(node.children or ())
                if isinstance(child, IndexNode)
            )
        else:
            indexes[id(node)] = _summarize_index(node, indexes, result._storage.sizes)
    storage = result._storage
    root = SummaryLayoutNode(
        location=result.root.location,
        image_layout_version=result.root.image_layout_version,
        statistics=SummaryStatistics(
            len(storage.sizes),
            storage.object_references,
            storage.subject_references,
            sum(storage.sizes.values()),
        ),
        statistics_complete=result.complete,
        children=tuple(indexes[id(node)] for node in result.root.children),
        errors=result.root.errors,
    )
    return LayoutSummaryResult(root, result.diagnostics, result.limits)

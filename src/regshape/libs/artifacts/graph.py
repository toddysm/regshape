"""Strict, iterative content-graph verification below layout and CLI orchestration.

Every descriptor occurrence is checked, including embedded data on reused edges.
Original document files, not parsed-model serialization, remain authoritative.
The graph includes explicit subjects but never reverse referrers discovery.
"""

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from regshape.libs.artifacts.models import ArtifactLimits, digest_parts
from regshape.libs.artifacts.storage import INCOMPLETE, ContentStore, SafeDirectory
from regshape.libs.errors import ArtifactError

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
INDEX_TYPES = (OCI_INDEX, DOCKER_INDEX)
MANIFEST_TYPES = (OCI_MANIFEST, DOCKER_MANIFEST)
DOCUMENT_TYPES = INDEX_TYPES + MANIFEST_TYPES
ACCEPT = ", ".join(DOCUMENT_TYPES)
REF_NAME = "org.opencontainers.image.ref.name"


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys rather than silently selecting a value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-JSON NaN/infinity tokens accepted by Python's default decoder."""
    raise ArtifactError("Non-finite JSON numbers are not supported")


def strict_json(raw: bytes, limits: ArtifactLimits, cap: int) -> dict[str, Any]:
    """Decode bounded strict UTF-8 JSON after a string-aware nesting precheck.

    Caps include whitespace. Malformed input, duplicate keys, excessive nesting,
    non-object roots and the interpreter's own parser limits become ArtifactError.
    """
    if len(raw) > cap:
        raise ArtifactError("JSON exceeds metadata byte cap")
    depth = 0
    quoted = False
    escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > limits.max_json_depth:
                raise ArtifactError("JSON exceeds max_json_depth")
        elif char in (93, 125):
            depth -= 1
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ArtifactError("Invalid UTF-8 JSON document") from error
    if not isinstance(value, dict):
        raise ArtifactError("Document must be a JSON object")
    return value


@dataclass(frozen=True)
class Edge:
    """One validated descriptor occurrence, retaining local metadata for this traversal."""

    digest: str
    size: int
    media_type: str
    document: bool
    role: str | None = None
    data: str | None = None


def descriptor(value: Any, *, document: bool, role: str | None = None) -> Edge:
    """Validate integrity/interpretation fields without coercion or path normalization."""
    if not isinstance(value, dict):
        raise ArtifactError("Descriptor must be an object")
    digest = value.get("digest")
    if not isinstance(digest, str):
        raise ArtifactError("Descriptor digest must be a string")
    try:
        digest_parts(digest)
    except ValueError as error:
        raise ArtifactError("Invalid descriptor digest") from error
    size = value.get("size")
    media_type = value.get("mediaType")
    if type(size) is not int or size < 0:
        raise ArtifactError("Descriptor size must be a nonnegative integer")
    if not isinstance(media_type, str) or not media_type or any(ord(c) < 33 for c in media_type):
        raise ArtifactError("Descriptor mediaType must be a nonempty media type")
    if document and media_type not in DOCUMENT_TYPES:
        raise ArtifactError("Unsupported required manifest/index media type")
    if "data" in value and not isinstance(value["data"], str):
        raise ArtifactError("Embedded descriptor data must be base64 text")
    if "urls" in value and (
        not isinstance(value["urls"], list)
        or not all(isinstance(url, str) for url in value["urls"])
    ):
        raise ArtifactError("Descriptor urls must be strings")
    if "annotations" in value and (
        not isinstance(value["annotations"], dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in value["annotations"].items())
    ):
        raise ArtifactError("Descriptor annotations must be string pairs")
    return Edge(value["digest"], size, media_type, document, role, value.get("data"))


def embedded_bytes(edge: Edge, limits: ArtifactLimits) -> bytes | None:
    """Strictly decode and verify inline data before storage, including on repeated edges."""
    if edge.data is None:
        return None
    if len(edge.data) > limits.max_manifest_bytes:
        raise ArtifactError("Embedded metadata exceeds max_manifest_bytes")
    if len(edge.data) != 4 * ((edge.size + 2) // 3):
        raise ArtifactError("Embedded size does not match descriptor")
    if edge.size > limits.max_total_bytes or (
        edge.document and edge.size > limits.max_manifest_bytes
    ):
        raise ArtifactError("Embedded content exceeds byte cap")
    try:
        decoded = base64.b64decode(edge.data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ArtifactError("Invalid embedded base64") from error
    if len(decoded) != edge.size or base64.b64encode(decoded).decode("ascii") != edge.data:
        raise ArtifactError("Noncanonical or incorrectly sized embedded base64")
    algorithm, _ = digest_parts(edge.digest)
    if f"{algorithm}:{hashlib.new(algorithm, decoded).hexdigest()}" != edge.digest:
        raise ArtifactError("Embedded digest mismatch")
    return decoded


def parse_document(
    raw: bytes, limits: ArtifactLimits, *, media_type: str | None = None,
    header: str = "",
) -> tuple[str, tuple[Edge, ...]]:
    """Resolve agreeing declarations and strict schema-2 shape; return subject-first edges."""
    document = strict_json(raw, limits, limits.max_manifest_bytes)
    declarations = [media_type] if media_type is not None else []
    if "mediaType" in document:
        declarations.append(document["mediaType"])
    header_type = header.partition(";")[0].strip().lower()
    if header_type not in ("", "application/json", "application/octet-stream"):
        declarations.append(header_type)
    if not declarations or any(value not in DOCUMENT_TYPES for value in declarations):
        raise ArtifactError("Unsupported or missing document media type")
    if len(set(declarations)) != 1:
        raise ArtifactError("Conflicting document media types")
    effective = declarations[0]
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
        raise ArtifactError("schemaVersion must be integer 2")
    children: list[Edge] = []
    if "subject" in document:
        children.append(descriptor(document["subject"], document=True))
    if effective in INDEX_TYPES:
        if "config" in document or "layers" in document or not isinstance(document.get("manifests"), list):
            raise ArtifactError("Index requires manifests, without config/layers")
        if len(document["manifests"]) + len(children) > limits.max_edges:
            raise ArtifactError("Graph exceeds max_edges")
        children.extend(descriptor(value, document=True) for value in document["manifests"])
    else:
        if "manifests" in document or not isinstance(document.get("layers"), list):
            raise ArtifactError("Manifest requires config/layers, without manifests")
        if len(document["layers"]) + len(children) + 1 > limits.max_edges:
            raise ArtifactError("Graph exceeds max_edges")
        children.append(descriptor(document.get("config"), document=False, role="config"))
        children.extend(descriptor(value, document=False, role="layer") for value in document["layers"])
    return effective, tuple(children)


@dataclass(frozen=True)
class GraphObject:
    """One verified address and its ordered dependencies, without retaining payload bytes."""

    digest: str
    size: int
    media_type: str
    kind: str
    role: str | None
    source: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedGraph:
    """Verified unique objects in dependency-first order plus every root occurrence."""

    roots: tuple[Edge, ...]
    objects: tuple[GraphObject, ...]
    reused_references: int
    bytes_stored: int


# Acquisition stores one new object's exact bytes, returning source and optional
# response Content-Type. It never runs for reused references or embedded content.
Acquire = Callable[[Edge, int], tuple[str, str]]
Observe = Callable[[Edge, str, str], None]


def verify_graph(
    roots: tuple[Edge, ...], store: ContentStore, limits: ArtifactLimits, *,
    acquire: Acquire | None = None, observe: Observe | None = None,
    embedded_missing: bool = False,
) -> VerifiedGraph:
    """Iteratively verify the full closure, with unique reservations and per-edge checks.

    Root descriptors count toward max_edges and have depth zero. Completed
    subtrees carry heights so reuse on a deeper path cannot bypass max_depth.
    Acquire receives the remaining byte budget before any new object is stored.
    In local validation inline data does not excuse a missing digest file.
    """
    objects: dict[str, GraphObject] = {}
    heights: dict[str, int] = {}
    visiting: set[str] = set()
    ordered: list[GraphObject] = []
    stack: list[tuple[Edge, int, bool]] = [(edge, 0, False) for edge in reversed(roots)]
    total = 0
    edges = len(roots)
    if edges > limits.max_edges:
        raise ArtifactError("Graph exceeds max_edges")
    reused = 0
    while stack:
        edge, depth, exiting = stack.pop()
        if exiting:
            item = objects[edge.digest]
            heights[edge.digest] = max((heights[digest] + 1 for digest in item.children), default=0)
            visiting.remove(edge.digest)
            ordered.append(item)
            continue
        if depth > limits.max_depth:
            raise ArtifactError("Graph exceeds max_depth")
        if edge.digest in visiting:
            raise ArtifactError("Cycle in artifact graph")
        if edge.digest in objects:
            embedded_bytes(edge, limits)
            existing = objects[edge.digest]
            if (
                edge.size != existing.size or edge.media_type != existing.media_type
                or edge.document != (existing.kind != "blob")
            ):
                raise ArtifactError("Repeated descriptor has incompatible size/type")
            if depth + heights[edge.digest] > limits.max_depth:
                raise ArtifactError("Reused subtree exceeds max_depth")
            reused += 1
            continue
        if len(objects) >= limits.max_objects:
            raise ArtifactError("Graph exceeds max_objects")
        if edge.size > limits.max_total_bytes - total:
            raise ArtifactError("Graph exceeds max_total_bytes")
        if edge.document and edge.size > limits.max_manifest_bytes:
            raise ArtifactError("Document exceeds max_manifest_bytes")
        inline = embedded_bytes(edge, limits)
        source, header = "local", ""
        if inline is not None and embedded_missing:
            store.put(edge.digest, inline)
            source = "embedded"
        elif acquire is not None:
            source, header = acquire(edge, limits.max_total_bytes - total)
        if observe is not None:
            observe(edge, source, "verifying")
        raw = store.verify(
            edge.digest, edge.size, cap=limits.max_manifest_bytes if edge.document else None,
        )
        children: tuple[Edge, ...] = ()
        media_type = edge.media_type
        if raw is not None:
            media_type, children = parse_document(raw, limits, media_type=media_type, header=header)
        edges += len(children)
        if edges > limits.max_edges:
            raise ArtifactError("Graph exceeds max_edges")
        kind = "index" if media_type in INDEX_TYPES and edge.document else (
            "manifest" if edge.document else "blob"
        )
        objects[edge.digest] = GraphObject(
            edge.digest, edge.size, media_type, kind, edge.role, source,
            tuple(child.digest for child in children),
        )
        total += edge.size
        if observe is not None:
            observe(edge, source, "stored")
        visiting.add(edge.digest)
        stack.append((edge, depth, True))
        stack.extend((child, depth + 1, False) for child in reversed(children))
    return VerifiedGraph(roots, tuple(ordered), reused, total)


def layout_roots(directory: SafeDirectory, limits: ArtifactLimits) -> tuple[bytes, tuple[Edge, ...]]:
    """Read wrapper metadata once under separate small caps; reject incomplete outputs."""
    if directory.exists(INCOMPLETE):
        raise ArtifactError("Incomplete artifact layout")
    marker = strict_json(
        directory.read_bytes("oci-layout", limits.max_layout_marker_bytes),
        limits, limits.max_layout_marker_bytes,
    )
    if marker.get("imageLayoutVersion") != "1.0.0":
        raise ArtifactError("Unsupported imageLayoutVersion")
    raw = directory.read_bytes("index.json", limits.max_manifest_bytes)
    _, roots = parse_document(raw, limits, media_type=OCI_INDEX)
    # Wrapper subjects would not be published as artifact roots and have no
    # defined authoring semantics. Reject rather than silently changing meaning.
    wrapper = strict_json(raw, limits, limits.max_manifest_bytes)
    if "subject" in wrapper:
        raise ArtifactError("Local wrapper subjects are unsupported")
    return raw, roots


def validate_directory(path: str, limits: ArtifactLimits) -> VerifiedGraph:
    """Read-only recursive validation with safe opens and bounded marker/object reads."""
    with SafeDirectory.open(path) as directory:
        _, roots = layout_roots(directory, limits)
        result = verify_graph(roots, ContentStore(directory), limits)
        directory.verify()
        if directory.exists(INCOMPLETE):
            raise ArtifactError("Layout became incomplete during validation")
        return result

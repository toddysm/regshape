"""Immutable local-layout occurrences, provenance, budgets and bounded JSON views.

Source metadata is shared read-only between occurrences. Serialization emits
the selected version-1 expanded or summary display, not SDK-only provenance,
and preflights destination-encoded JSON bytes before allocating an independent result.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

from regshape.libs.errors import LayoutInspectionError

JsonValue: TypeAlias = (
    str
    | bool
    | int
    | float
    | None
    | Mapping[str, "JsonValue"]
    | tuple["JsonValue", ...]
)
DiagnosticCode: TypeAlias = Literal[
    "INVALID_LAYOUT",
    "UNSUPPORTED_PLATFORM",
    "MISSING_OBJECT",
    "UNREADABLE_OBJECT",
    "MALFORMED_DOCUMENT",
    "INVALID_DESCRIPTOR",
    "UNSUPPORTED_TYPE",
    "CONFLICTING_DESCRIPTOR",
    "CYCLE",
    "LIMIT_REACHED",
    "CHANGED_DURING_READ",
    "OUTPUT_LIMIT",
]


@dataclass(frozen=True)
class InspectionLimits:
    """Independent inclusive budgets; every value is a positive non-boolean int.

    Metadata counts distinct raw documents plus the marker, descriptors count
    source slots, and occurrences include layout/entry point and repeated nodes.
    Depth starts at zero for the layout. Output counts the selected encoded
    representation, including escapes, indentation, LF and generated ANSI.
    """

    max_layout_marker_bytes: int = 65_536
    max_document_bytes: int = 16_777_216
    max_total_metadata_bytes: int = 67_108_864
    max_json_depth: int = 128
    max_objects: int = 100_000
    max_descriptors: int = 500_000
    max_depth: int = 64
    max_occurrences: int = 100_000
    max_diagnostics: int = 1_000
    max_output_bytes: int = 67_108_864

    def __post_init__(self) -> None:
        """Reject invalid budgets before inspection or serialization performs IO."""
        for item in fields(self):
            if (
                type(getattr(self, item.name)) is not int
                or getattr(self, item.name) <= 0
            ):
                raise ValueError(f"{item.name} must be a positive integer")


@dataclass(frozen=True)
class SourceLocation:
    """Relative source document and RFC 6901 pointer, never an inferred OCI field."""

    document: str
    pointer: str = ""

    def __str__(self) -> str:
        """Format provenance as document#pointer, including the document root."""
        return f"{self.document}#{self.pointer}"


@dataclass(frozen=True)
class InspectionDiagnostic:
    """Structural failure at its source owner and, when known, affected file."""

    code: DiagnosticCode
    message: str
    source: SourceLocation
    location: str | None = None


@dataclass(frozen=True)
class DescriptorView:
    """One source slot, including independently valid fields of invalid descriptors.

    Size/type/artifact evidence and provenance are SDK facts, not display fields.
    None means absent or invalid; present empty maps remain immutable empty maps.
    Invalid digest text never acquires a target path.
    """

    source: SourceLocation
    digest: str | None = None
    platform: Mapping[str, JsonValue] | None = None
    annotations: Mapping[str, JsonValue] | None = None
    errors: tuple[InspectionDiagnostic, ...] = ()
    size: int | None = None
    media_type: str | None = None
    artifact_type: str | None = None


@dataclass(frozen=True)
class ReferrerListEvidence:
    """Occurrence-local tag declaration, not an intrinsic or verified subject.

    A sha512 suffix is supplied only for one unique reachable metadata candidate.
    Unresolved associations are not structural errors.
    """

    tag: str
    source: SourceLocation
    algorithm: Literal["sha256", "sha512"]
    prefix: str
    status: Literal["resolved", "unresolved"]
    digest: str | None = None


@dataclass(frozen=True, kw_only=True)
class _Node:
    """Shared occurrence identity; incoming descriptor metadata stays source-owned."""

    location: str
    source: SourceLocation
    digest: str | None = None
    incoming: DescriptorView | None = None
    source_position: int | None = None
    errors: tuple[InspectionDiagnostic, ...] = ()


@dataclass(frozen=True, kw_only=True)
class _IndexNode(_Node):
    """Source-owned index fields shared by expanded and summarized occurrences."""

    kind: Literal["index"] = field(default="index", init=False)
    schema_version: int | None = None
    media_type: str | None = None
    artifact_type: str | None = None
    subject: DescriptorView | None = None
    annotations: Mapping[str, JsonValue] | None = None
    manifests: tuple[DescriptorView, ...] | None = None
    referrer_list: ReferrerListEvidence | None = None


@dataclass(frozen=True, kw_only=True)
class IndexNode(_IndexNode):
    """One entry-point/stored index occurrence; None children denote unparsed input."""

    children: tuple[ObjectNode, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class ManifestNode(_Node):
    """A manifest with ordered opaque children and source-owned descriptor blocks."""

    kind: Literal["manifest"] = field(default="manifest", init=False)
    schema_version: int | None = None
    media_type: str | None = None
    artifact_type: str | None = None
    subject: DescriptorView | None = None
    annotations: Mapping[str, JsonValue] | None = None
    config: DescriptorView | None = None
    layers: tuple[DescriptorView, ...] | None = None
    children: tuple[OpaqueNode, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class OpaqueNode(_Node):
    """Config/layer/content occurrence: location only, no payload read or decoding."""

    kind: Literal["config", "layer", "content"]
    position: int | None = None


@dataclass(frozen=True, kw_only=True)
class UnknownNode(_Node):
    """Unsupported required document type, retaining safe identity and explicit errors."""

    kind: Literal["unknown"] = field(default="unknown", init=False)


ObjectNode: TypeAlias = IndexNode | ManifestNode | OpaqueNode | UnknownNode


@dataclass(frozen=True)
class LayoutNode:
    """Absolute selected layout location and exactly one entry-point index."""

    location: str
    image_layout_version: str
    children: tuple[IndexNode, ...]
    errors: tuple[InspectionDiagnostic, ...] = ()
    kind: Literal["layout"] = field(default="layout", init=False)


SummaryCategory: TypeAlias = Literal[
    "platform-images",
    "attestations",
    "signatures",
    "lifecycle",
    "artifacts",
    "unclassified",
]


@dataclass(frozen=True)
class SummaryGroup:
    """A contiguous run of complete manifests, retaining borrowed source evidence.

    Members retain their own metadata, incoming descriptors and explicit subjects;
    they are not serialized as a hidden expanded tree. Counts describe displayed
    occurrences except unique totals, which are not additive across groups.
    """

    category: SummaryCategory
    positions: tuple[int, ...]
    members: tuple[ManifestNode, ...]
    unique_manifests: int
    config_references: int
    layer_references: int
    layer_count_distribution: Mapping[str, int]
    unique_objects: int
    stored_bytes: int
    artifact_type: str | None = None
    kind: Literal["group"] = field(default="group", init=False)

    @property
    def references(self) -> int:
        """Number of displayed manifest occurrences, not distinct stored files."""
        return len(self.members)


@dataclass(frozen=True, kw_only=True)
class SummaryIndexNode(_IndexNode):
    """An unchanged index owner with ordered grouped or expanded child occurrences."""

    children: (
        tuple[
            SummaryIndexNode | ManifestNode | OpaqueNode | UnknownNode | SummaryGroup,
            ...,
        ]
        | None
    ) = None


@dataclass(frozen=True)
class SummaryStatistics:
    """Observed containment storage and distinct-document slot counts.

    Subjects are separate reference facts, never additional contained objects.
    Completeness is supplied by the owning result, not inferred from these totals.
    """

    unique_objects: int
    object_references: int
    subject_references: int
    stored_bytes: int


@dataclass(frozen=True)
class SummaryLayoutNode:
    """Selected layout and all index ancestry, plus observed summary-only statistics."""

    location: str
    image_layout_version: str
    statistics: SummaryStatistics
    statistics_complete: bool
    children: tuple[SummaryIndexNode, ...]
    errors: tuple[InspectionDiagnostic, ...] = ()
    kind: Literal["layout"] = field(default="layout", init=False)


@dataclass(frozen=True)
class _StorageFacts:
    """Call-local observed address sizes and source-slot counts for SDK summaries.

    Existing regular contained files count once, even when their metadata fails.
    Missing/unlisted/subject-only addresses do not acquire stored bytes.
    """

    sizes: Mapping[str, int]
    object_references: int
    subject_references: int


@dataclass(frozen=True)
class LayoutInspectionResult:
    """Immutable expanded result; complete means structural, not cryptographic success.

    Diagnostics follow discovery order and refer to the same node/descriptor
    errors. Handles are closed before this value escapes the inspector.
    """

    root: LayoutNode
    diagnostics: tuple[InspectionDiagnostic, ...]
    limits: InspectionLimits
    _storage: _StorageFacts = field(repr=False)

    @property
    def complete(self) -> bool:
        """Whether inspection found no structural errors or traversal limits."""
        return not self.diagnostics

    def to_dict(self) -> dict[str, object]:
        """Return an independent version-1 display copy after JSON-byte preflight.

        :raises LayoutInspectionError: When the complete encoded view exceeds
            max_output_bytes; no partial copy is returned.
        """
        return _view_dict(self.root, self.limits.max_output_bytes)

    def to_json(self, *, encoding: str = "ascii") -> str:
        """Return two-space ASCII JSON plus LF, preflighted before string allocation.

        :param encoding: Destination text encoding for byte-budget accounting only;
            the returned JSON string remains ASCII-safe. Includes encoding state/BOM.
        :raises ValueError: If encoding does not name a text-to-bytes codec.
        :raises LayoutInspectionError: When the encoded output exceeds its cap.
        """
        _preflight(self.root, self.limits.max_output_bytes, encoding)
        return "".join(_json_output_chunks(self.root))


@dataclass(frozen=True)
class LayoutSummaryResult:
    """Immutable selected summary; inspection errors and limits remain authoritative."""

    root: SummaryLayoutNode
    diagnostics: tuple[InspectionDiagnostic, ...]
    limits: InspectionLimits

    @property
    def complete(self) -> bool:
        """Whether the source inspection found no structural errors or limits."""
        return not self.diagnostics

    def to_dict(self) -> dict[str, object]:
        """Return independent summary JSON fields, preflighting only the selected view.

        :raises LayoutInspectionError: If encoded summary bytes exceed the output cap.
        """
        return _view_dict(self.root, self.limits.max_output_bytes)

    def to_json(self, *, encoding: str = "ascii") -> str:
        """Return two-space ASCII summary JSON and LF, never a partial document.

        :param encoding: Destination text encoding for byte-budget accounting only;
            the returned JSON string remains ASCII-safe. Includes encoding state/BOM.
        :raises ValueError: If encoding does not name a text-to-bytes codec.
        :raises LayoutInspectionError: If encoded summary bytes exceed the output cap.
        """
        _preflight(self.root, self.limits.max_output_bytes, encoding)
        return "".join(_json_output_chunks(self.root))


def _view_dict(root: LayoutNode | SummaryLayoutNode, cap: int) -> dict[str, object]:
    """Preflight a selected view before iteratively allocating its independent copy."""
    _preflight(root, cap)
    result: dict[str, object] = {}
    stack: list[tuple[object, object]] = [(_Projection(root), result)]
    while stack:
        source, target = stack.pop()
        if isinstance(source, Mapping) and isinstance(target, dict):
            items = (
                source._items() if isinstance(source, _Projection) else source.items()
            )
            for key, value in items:
                target[key] = _copy_slot(value, stack)
        elif isinstance(source, (tuple, _ViewSequence)) and isinstance(target, list):
            target.extend(_copy_slot(value, stack) for value in source)
    return result


def _copy_slot(value: object, stack: list[tuple[object, object]]) -> object:
    """Allocate one independent container and schedule its contents iteratively."""
    if isinstance(value, Mapping):
        mapping: dict[str, object] = {}
        stack.append((value, mapping))
        return mapping
    if isinstance(value, (tuple, _ViewSequence)):
        sequence: list[object] = []
        stack.append((value, sequence))
        return sequence
    return value


def _error_items(
    errors: tuple[InspectionDiagnostic, ...],
) -> tuple[Mapping[str, JsonValue], ...]:
    """Project diagnostics without SDK-only affected locations or a second collection."""
    return tuple(
        MappingProxyType(
            {
                "code": item.code,
                "message": item.message,
                "source": str(item.source),
            }
        )
        for item in errors
    )


class _Projection(Mapping[str, object]):
    """Lazy ordered display mapping; expanding it never copies a descendant tree."""

    def __init__(
        self,
        value: LayoutNode
        | ObjectNode
        | DescriptorView
        | SummaryLayoutNode
        | SummaryIndexNode
        | SummaryGroup,
    ) -> None:
        """Borrow an immutable occurrence/descriptor for its wire-field projection."""
        self.value = value

    def _items(self) -> Iterator[tuple[str, object]]:
        """Yield version-1 keys in fixed order, retaining source map/array ordering."""
        value = self.value
        if isinstance(value, SummaryGroup):
            yield "kind", value.kind
            yield "category", value.category
            if value.artifact_type is not None:
                yield "artifactType", value.artifact_type
            yield "positions", value.positions
            yield "references", value.references
            yield "uniqueManifests", value.unique_manifests
            yield "configReferences", value.config_references
            yield "layerReferences", value.layer_references
            yield "layerCountDistribution", value.layer_count_distribution
            yield "uniqueObjects", value.unique_objects
            yield "storedBytes", value.stored_bytes
            return
        if isinstance(value, DescriptorView):
            if value.digest is not None:
                yield "digest", value.digest
            if value.platform is not None:
                yield "platform", value.platform
            if value.annotations is not None:
                yield "annotations", value.annotations
        else:
            yield "kind", value.kind
            if isinstance(value, OpaqueNode) and value.position is not None:
                yield "position", value.position
            if (
                not isinstance(value, (LayoutNode, SummaryLayoutNode))
                and value.digest is not None
            ):
                yield "digest", value.digest
            yield "location", value.location
            if isinstance(value, (LayoutNode, SummaryLayoutNode)):
                yield "imageLayoutVersion", value.image_layout_version
            if isinstance(value, SummaryLayoutNode):
                stats = value.statistics
                yield (
                    "statistics",
                    {
                        "uniqueObjects": stats.unique_objects,
                        "objectReferences": stats.object_references,
                        "subjectReferences": stats.subject_references,
                        "storedBytes": stats.stored_bytes,
                    },
                )
                yield "statisticsComplete", value.statistics_complete
            if isinstance(value, (_IndexNode, ManifestNode)):
                for key, item in (
                    ("schemaVersion", value.schema_version),
                    ("mediaType", value.media_type),
                    ("artifactType", value.artifact_type),
                ):
                    if item is not None:
                        yield key, item
                if value.subject is not None:
                    yield "subject", _Projection(value.subject)
                if value.annotations is not None:
                    yield "annotations", value.annotations
                if isinstance(value, _IndexNode) and value.manifests is not None:
                    yield "manifests", _ViewSequence(value.manifests)
                if (
                    isinstance(value, SummaryIndexNode)
                    and value.referrer_list is not None
                ):
                    evidence = value.referrer_list
                    association = {
                        "tag": evidence.tag,
                        "source": str(evidence.source),
                        "status": evidence.status,
                    }
                    if evidence.digest is not None:
                        association["digest"] = evidence.digest
                    else:
                        association["algorithm"] = evidence.algorithm
                        association["prefix"] = evidence.prefix
                    yield "association", association
                if isinstance(value, ManifestNode):
                    if value.config is not None:
                        yield "config", _Projection(value.config)
                    if value.layers is not None:
                        yield "layers", _ViewSequence(value.layers)
            if (
                isinstance(
                    value,
                    (
                        LayoutNode,
                        IndexNode,
                        ManifestNode,
                        SummaryLayoutNode,
                        SummaryIndexNode,
                    ),
                )
                and value.children is not None
            ):
                yield "children", _ViewSequence(value.children)
        if value.errors:
            yield "errors", _error_items(value.errors)

    def __iter__(self) -> Iterator[str]:
        """Iterate stable display keys, omitting absent optional fields."""
        return (key for key, _ in self._items())

    def __len__(self) -> int:
        """Count local keys only; never materialize descendants."""
        return sum(1 for _ in self._items())

    def __getitem__(self, key: str) -> object:
        """Resolve a projected key or raise KeyError for an omitted field."""
        for name, value in self._items():
            if name == key:
                return value
        raise KeyError(key)


class _ViewSequence:
    """Borrow an occurrence tuple; projection wrappers are allocated only as visited."""

    def __init__(
        self,
        values: tuple[DescriptorView, ...]
        | tuple[ObjectNode | SummaryIndexNode | SummaryGroup, ...],
    ) -> None:
        """Retain immutable local values without copying a wide array."""
        self.values = values

    def __iter__(self) -> Iterator[_Projection]:
        """Yield one lazy display view at a time."""
        return (_Projection(item) for item in self.values)


def _string_chunks(value: str) -> Iterator[str]:
    """Escape ASCII JSON strings in small chunks instead of allocating amplified values."""
    yield '"'
    for start in range(0, len(value), 1024):
        yield json.dumps(value[start : start + 1024], ensure_ascii=True)[1:-1]
    yield '"'


def _json_chunks(value: object) -> Iterator[str]:
    """Stream pretty JSON iteratively with bounded string-escaping allocations.

    Iterator frames keep wide containers lazy; nesting does not use Python
    recursion. Unsupported scalars/nonfinite floats raise ValueError.
    """
    stack: list[tuple[str, object, int]] = [("value", value, 0)]
    while stack:
        action, current, depth = stack.pop()
        if action == "text":
            yield str(current)
        elif action == "map":
            map_iterator = cast(Iterator[tuple[str, object]], current)
            key, child = next(map_iterator, (None, None))
            if key is None:
                yield "\n" + "  " * (depth - 1) + "}"
            else:
                yield ",\n" + "  " * depth
                yield from _string_chunks(key)
                yield ": "
                stack.extend([("map", map_iterator, depth), ("value", child, depth)])
        elif action == "array":
            array_iterator = cast(Iterator[object], current)
            entry = next(array_iterator, _END)
            if entry is _END:
                yield "\n" + "  " * (depth - 1) + "]"
            else:
                yield ",\n" + "  " * depth
                stack.extend(
                    [("array", array_iterator, depth), ("value", entry, depth)]
                )
        elif isinstance(current, Mapping):
            map_iterator = iter(
                current._items()
                if isinstance(current, _Projection)
                else current.items()
            )
            first = next(map_iterator, None)
            if first is None:
                yield "{}"
            else:
                key, child = first
                yield "{\n" + "  " * (depth + 1)
                yield from _string_chunks(key)
                yield ": "
                stack.extend(
                    [("map", map_iterator, depth + 1), ("value", child, depth + 1)]
                )
        elif isinstance(current, (tuple, _ViewSequence)):
            array_iterator = iter(current)
            entry = next(array_iterator, _END)
            if entry is _END:
                yield "[]"
            else:
                yield "[\n" + "  " * (depth + 1)
                stack.extend(
                    [("array", array_iterator, depth + 1), ("value", entry, depth + 1)]
                )
        elif isinstance(current, str):
            yield from _string_chunks(current)
        else:
            yield json.dumps(current, ensure_ascii=True, allow_nan=False)


_END = object()


def _json_output_chunks(root: LayoutNode | SummaryLayoutNode) -> Iterator[str]:
    """Yield the SDK projection and final LF, deferring tree access until iteration."""
    yield from _json_chunks(_Projection(root))
    yield "\n"


def _utf7_lengths(chunks: Iterable[str]) -> Iterator[int]:
    """Count whole-string UTF-7 without buffering shifted UTF-16/base64 runs.

    Python's UTF-7 incremental encoder closes each chunk's shift, so summing it
    overcounts. Retain only the run's UTF-16 unit count and account for unpadded
    six-bit groups and the terminator required before base64 characters or '-'.
    """
    units = 0
    for chunk in chunks:
        size = 0
        for char in chunk:
            direct = char in "\t\r\n" or (" " <= char <= "}" and char not in "+\\")
            if direct:
                if units:
                    size += int(char.isalnum() or char in "/-")
                    units = 0
                size += 1
            elif char == "+" and not units:
                size += 2
            else:
                if not units:
                    size += 1
                previous = (units * 16 + 5) // 6
                units += 1 + (ord(char) > 0xFFFF)
                size += (units * 16 + 5) // 6 - previous
        yield size
    if units:
        yield 1


def encoded_chunk_lengths(chunks: Iterable[str], encoding: str) -> Iterator[int]:
    """Count destination bytes as if chunks were joined, without joining them.

    Includes state/BOM and final codec flush; UTF-7 shift state crosses chunk
    boundaries. Consumes caller-supplied chunks lazily so budget owners can stop.

    :yields: Additional encoded bytes, including initial/final codec state.
    :raises ValueError: On first iteration, before consuming chunks, for unknown/non-text codecs.
    :raises UnicodeError: During iteration if the destination codec rejects text.
    """
    try:
        encoder = codecs.getincrementalencoder(encoding)()
        initial = encoder.encode("")
        canonical = codecs.lookup(encoding).name
    except (LookupError, TypeError) as error:
        raise ValueError("encoding must name a text-to-bytes codec") from error
    if not isinstance(initial, bytes):
        raise ValueError("encoding must name a text-to-bytes codec")
    if canonical == "utf-7":
        yield from _utf7_lengths(chunks)
        return
    yield len(initial)
    for chunk in chunks:
        yield len(encoder.encode(chunk))
    yield len(encoder.encode("", final=True))


def _preflight(
    root: LayoutNode | SummaryLayoutNode, cap: int, encoding: str = "ascii"
) -> None:
    """Count final encoded bytes including state/BOM and LF; stop before materialization."""
    count = 0
    for size in encoded_chunk_lengths(_json_output_chunks(root), encoding):
        count += size
        if count > cap:
            raise LayoutInspectionError(
                (
                    InspectionDiagnostic(
                        "OUTPUT_LIMIT",
                        f"max_output_bytes cap {cap} exceeded",
                        SourceLocation("index.json"),
                        root.location,
                    ),
                )
            )

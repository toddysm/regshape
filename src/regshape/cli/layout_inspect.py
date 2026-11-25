"""Render SDK-owned expanded/summary occurrences without IO, classification or recounting.

Text escapes untrusted strings before adding SGR spans. A counting pass enforces
the selected stream's encoded byte budget before materializing or writing stdout;
JSON delegates its shape and serialization to the SDK. Requested scenario
telemetry borrows an encoding-safe stderr sink without a stdout fallback.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from typing import Literal, NoReturn, TextIO, cast

import click
from click.utils import PacifyFlushWrapper

from regshape.libs.decorators import get_telemetry_config
from regshape.libs.layout import (
    DescriptorView,
    IndexNode,
    InspectionDiagnostic,
    LayoutInspectionError,
    LayoutInspectionResult,
    LayoutNode,
    LayoutSummaryResult,
    ManifestNode,
    OpaqueNode,
    SourceLocation,
    SummaryGroup,
    SummaryIndexNode,
    SummaryLayoutNode,
)
from regshape.libs.layout.inspection_models import (
    JsonValue,
    ObjectNode,
    encoded_chunk_lengths,
)


def _escape(
    value: str, encoding: str, ascii_only: bool, *, json_encoded: bool = False
) -> Iterator[str]:
    """Escape unsafe terminal text in bounded chunks, preserving generated JSON syntax.

    JSON-encoded chunks retain existing backslashes and use JSON Unicode escapes
    for unsafe/unencodable characters; raw strings retain reversible text escapes.
    """
    for start in range(0, len(value), 512):
        escaped = []
        for char in value[start : start + 512]:
            if char == "\\" and json_encoded:
                escaped.append(char)
                continue
            if char in "\\\n\t\r":
                escaped.append(
                    {"\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}[char]
                )
                continue
            unsafe = unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp")
            try:
                char.encode("ascii" if ascii_only else encoding)
            except UnicodeEncodeError:
                unsafe = True
            if unsafe:
                if json_encoded:
                    escaped.append(json.dumps(char, ensure_ascii=True)[1:-1])
                else:
                    code = ord(char)
                    escaped.append(
                        f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
                    )
            else:
                escaped.append(char)
        yield "".join(escaped)


def _compact_json(value: JsonValue) -> Iterator[str]:
    """Yield compact platform/annotation JSON without copying maps or whole strings."""
    if isinstance(value, Mapping):
        yield "{"
        for position, (key, item) in enumerate(value.items()):
            if position:
                yield ", "
            yield from _compact_json(key)
            yield ": "
            yield from _compact_json(item)
        yield "}"
    elif isinstance(value, tuple):
        yield "["
        for position, item in enumerate(value):
            if position:
                yield ", "
            yield from _compact_json(item)
        yield "]"
    elif isinstance(value, str):
        yield '"'
        for start in range(0, len(value), 512):
            yield json.dumps(value[start : start + 512], ensure_ascii=False)[1:-1]
        yield '"'
    else:
        yield json.dumps(value, allow_nan=False)


class _Tree:
    """One immutable presentation policy, reusable for counting and materialization passes."""

    def __init__(self, encoding: str, ascii_only: bool, color: bool) -> None:
        """Select encodable connectors; forced ASCII also escapes every non-ASCII value.

        :raises ValueError: For unknown or non-text destination codecs.
        """
        self.encoding = encoding
        self.ascii_only = ascii_only
        self.color = color
        try:
            "│├└─".encode(encoding)
            unicode_tree = not ascii_only
        except UnicodeEncodeError:
            unicode_tree = False
        except (LookupError, TypeError) as error:
            raise ValueError("encoding must name a text-to-bytes codec") from error
        self.stem = "│" if unicode_tree else "|"
        self.branch = "├── " if unicode_tree else "|-- "
        self.last = "└── " if unicode_tree else "`-- "

    def _span(self, chunks: Iterable[str], style: str) -> Iterator[str]:
        """Wrap renderer-owned styling around already-safe text, with an independent reset."""
        if self.color:
            yield f"\x1b[{style}m"
        yield from chunks
        if self.color:
            yield "\x1b[0m"

    def _safe(
        self, chunks: Iterable[str], *, json_encoded: bool = False
    ) -> Iterator[str]:
        """Escape text before styling, without re-escaping renderer-generated JSON."""
        for chunk in chunks:
            yield from _escape(
                chunk, self.encoding, self.ascii_only, json_encoded=json_encoded
            )

    def _end(self) -> Iterator[str]:
        """End a line without carrying its final style into subsequent output."""
        if self.color:
            yield "\x1b[0m"
        yield "\n"

    def _field(
        self,
        prefix: str,
        label: str,
        value: str | Iterable[str] | None = None,
        *,
        json_encoded: bool = False,
        style: str = "36",
        slot: str = "",
    ) -> Iterator[str]:
        """Emit a label/value line; None denotes a section and generated JSON keeps its escapes."""
        yield from self._span((prefix,), "90")
        if slot:
            yield from self._span((slot,), "36")
        yield from self._span(self._safe((label,)), style)
        yield from self._span((":",), style)
        if value is not None:
            yield " "
            yield from self._span(
                self._safe(
                    (value,) if isinstance(value, str) else value,
                    json_encoded=json_encoded,
                ),
                "39",
            )
        yield from self._end()

    def _annotations(
        self, prefix: str, annotations: Mapping[str, JsonValue]
    ) -> Iterator[str]:
        """Retain present empty maps and source annotation order without decoding string values."""
        yield from self._field(
            prefix, "Annotations (annotations)", "{}" if not annotations else None
        )
        for key, value in annotations.items():
            yield from self._field(
                prefix + "  ",
                key,
                value if isinstance(value, str) else _compact_json(value),
                json_encoded=not isinstance(value, str),
            )

    def _errors(
        self,
        prefix: str,
        errors: tuple[InspectionDiagnostic, ...],
        position: str = "",
    ) -> Iterator[str]:
        """Show explicit owner-local failures and provenance, indexing digest-less source slots."""
        for index, error in enumerate(errors):
            label = (
                "Missing file (MISSING_OBJECT)"
                if error.code == "MISSING_OBJECT"
                else f"Error ({error.code})"
            )
            yield from self._field(
                prefix,
                label,
                error.message,
                style="1;31",
                slot=position if index == 0 else "",
            )
            yield from self._field(prefix + "  ", "Source", str(error.source))

    def _descriptor(
        self, prefix: str, descriptor: DescriptorView, position: int | None = None
    ) -> Iterator[str]:
        """Print selected descriptor fields under their source owner, never as object metadata."""
        slot = "" if position is None else f"[{position}] "
        if descriptor.digest is not None:
            yield from self._field(prefix, slot + "Digest (digest)", descriptor.digest)
        else:
            yield from self._errors(prefix, descriptor.errors, slot)
        nested = prefix + "  "
        if descriptor.platform is not None:
            yield from self._field(
                nested,
                "Platform (platform)",
                _compact_json(descriptor.platform),
                json_encoded=True,
            )
        if descriptor.annotations is not None:
            yield from self._annotations(nested, descriptor.annotations)
        if descriptor.digest is not None:
            yield from self._errors(nested, descriptor.errors)

    def _properties(
        self,
        node: (
            LayoutNode
            | ObjectNode
            | SummaryLayoutNode
            | SummaryIndexNode
            | SummaryGroup
        ),
        prefix: str,
    ) -> Iterator[str]:
        """Render source fields or exact SDK summary counts, never inspecting group members."""
        if isinstance(node, SummaryGroup):
            yield from self._field(prefix, "Category", node.category)
            if node.artifact_type is not None:
                yield from self._field(
                    prefix, "Artifact type (artifactType)", node.artifact_type
                )
            yield from self._field(
                prefix, "Positions", _compact_json(node.positions), json_encoded=True
            )
            for label, count in (
                ("References", node.references),
                ("Unique manifests", node.unique_manifests),
                ("Config references", node.config_references),
                ("Layer references", node.layer_references),
            ):
                yield from self._field(prefix, label, str(count))
            yield from self._field(
                prefix,
                "Layer count distribution",
                _compact_json(node.layer_count_distribution),
                json_encoded=True,
            )
            yield from self._field(prefix, "Unique objects", str(node.unique_objects))
            yield from self._field(prefix, "Stored bytes", str(node.stored_bytes))
            return
        yield from self._field(prefix, "Location", node.location)
        if isinstance(node, (LayoutNode, SummaryLayoutNode)):
            yield from self._field(
                prefix,
                "Layout format version (imageLayoutVersion)",
                node.image_layout_version,
            )
        if isinstance(node, SummaryLayoutNode):
            for label, count in (
                ("Unique objects", node.statistics.unique_objects),
                ("Object references", node.statistics.object_references),
                ("Subject references", node.statistics.subject_references),
                ("Stored bytes", node.statistics.stored_bytes),
            ):
                yield from self._field(prefix, label, str(count))
            yield from self._field(
                prefix, "Statistics complete", str(node.statistics_complete).lower()
            )
        if isinstance(node, (IndexNode, SummaryIndexNode, ManifestNode)):
            for label, value in (
                ("Document schema version (schemaVersion)", node.schema_version),
                ("Media type (mediaType)", node.media_type),
                ("Artifact type (artifactType)", node.artifact_type),
            ):
                if value is not None:
                    yield from self._field(prefix, label, str(value))
            if node.subject is not None:
                yield from self._field(prefix, "Subject (subject)")
                yield from self._descriptor(prefix + "  ", node.subject)
            if node.annotations is not None:
                yield from self._annotations(prefix, node.annotations)
            if (
                isinstance(node, (IndexNode, SummaryIndexNode))
                and node.manifests is not None
            ):
                yield from self._field(
                    prefix,
                    "Referenced objects (manifests)",
                    "[]" if not node.manifests else None,
                )
                for position, descriptor in enumerate(node.manifests):
                    yield from self._descriptor(prefix + "  ", descriptor, position)
            if isinstance(node, SummaryIndexNode) and node.referrer_list is not None:
                evidence = node.referrer_list
                yield from self._field(prefix, "Tag-schema association")
                for label, value in (
                    ("Tag", evidence.tag),
                    ("Source", str(evidence.source)),
                    ("Status", evidence.status),
                ):
                    yield from self._field(prefix + "  ", label, value)
                if evidence.digest is not None:
                    yield from self._field(prefix + "  ", "Digest", evidence.digest)
                else:
                    yield from self._field(
                        prefix + "  ", "Algorithm", evidence.algorithm
                    )
                    yield from self._field(prefix + "  ", "Prefix", evidence.prefix)
            if isinstance(node, ManifestNode):
                if node.config is not None:
                    yield from self._field(prefix, "Configuration (config)")
                    yield from self._descriptor(prefix + "  ", node.config)
                if node.layers is not None:
                    yield from self._field(
                        prefix, "Layers (layers)", "[]" if not node.layers else None
                    )
                    for position, descriptor in enumerate(node.layers):
                        yield from self._descriptor(prefix + "  ", descriptor, position)
        yield from self._errors(prefix, node.errors)

    def _node(
        self,
        node: (
            LayoutNode
            | ObjectNode
            | SummaryLayoutNode
            | SummaryIndexNode
            | SummaryGroup
        ),
        prefix: str = "",
        last: bool = True,
    ) -> Iterator[str]:
        """Walk only SDK-provided occurrences; stems distinguish internal nodes from leaves."""
        if isinstance(node, (LayoutNode, SummaryLayoutNode)):
            name, style = "OCI layout", "39"
            base = prefix
        else:
            name, style = {
                "index": ("Index", "1;35"),
                "manifest": ("Manifest", "1;36"),
                "group": ("Manifest group", "1;36"),
                "config": ("Config", "1;33"),
                "layer": ("Layer", "1;34"),
                "content": ("Content", "1;34"),
                "unknown": ("Unknown", "1;39"),
            }[node.kind]
            if isinstance(node, OpaqueNode) and node.position is not None:
                name += f" {node.position}"
            yield from self._span(
                (prefix + (self.last if last else self.branch),), "90"
            )
            base = prefix + ("    " if last else self.stem + "   ")
        yield from self._span((name,), style)
        if (
            not isinstance(node, (LayoutNode, SummaryLayoutNode, SummaryGroup))
            and node.digest is not None
        ):
            yield " "
            yield from self._span(self._safe((node.digest,)), "39")
        yield from self._end()
        children = (
            node.children
            if isinstance(
                node,
                (
                    LayoutNode,
                    SummaryLayoutNode,
                    IndexNode,
                    SummaryIndexNode,
                    ManifestNode,
                ),
            )
            else None
        )
        yield from self._properties(
            node, base + (self.stem + "  " if children else "  ")
        )
        if children:
            yield from self._span((base + self.stem,), "90")
            yield from self._end()
            for position, child in enumerate(children):
                yield from self._node(child, base, position == len(children) - 1)
                if (
                    position < len(children) - 1
                    and isinstance(child, (IndexNode, SummaryIndexNode, ManifestNode))
                    and child.children
                ):
                    yield from self._span((base + self.stem,), "90")
                    yield from self._end()

    def chunks(self, root: LayoutNode | SummaryLayoutNode) -> Iterator[str]:
        """Yield the selected SDK view without expanding groups or allocating another tree."""
        yield from self._node(root)


def _preflight(
    chunks: Iterable[str],
    encoding: str,
    result: LayoutInspectionResult | LayoutSummaryResult,
) -> None:
    """Count final encoded bytes/state; stop at the first over-budget chunk."""
    size = 0
    for amount in encoded_chunk_lengths(chunks, encoding):
        size += amount
        if size > result.limits.max_output_bytes:
            break
    if size > result.limits.max_output_bytes:
        raise LayoutInspectionError(
            (
                InspectionDiagnostic(
                    "OUTPUT_LIMIT",
                    f"max_output_bytes cap {result.limits.max_output_bytes} exceeded",
                    SourceLocation("index.json"),
                ),
            )
        )


def render_inspection(
    result: LayoutInspectionResult | LayoutSummaryResult,
    *,
    encoding: str,
    ascii_only: bool,
    color: bool,
) -> str:
    """Return bounded selected-view text; the caller owns stream/color policy and delivery.

    :raises LayoutInspectionError: Before materialization if encoded output exceeds
        the result's inclusive max_output_bytes budget.
    :raises ValueError: For invalid/non-stream destination codecs, including IDNA/Punycode.
    """
    tree = _Tree(encoding, ascii_only, color)
    _preflight(tree.chunks(result.root), encoding, result)
    return "".join(tree.chunks(result.root))


class _FailedFlushStream(PacifyFlushWrapper):
    """Non-owning Click proxy for a process stream whose delivery already failed.

    Inherit Click's borrowed-stream constructor/attribute forwarding, but never
    retry its known-failed buffer during shutdown. The primary error is reported
    when stderr is writable, before this guard; healthy streams retain normal flushing.
    """

    def flush(self) -> None:
        """Do not retry already-reported failed output or close its borrowed stream."""
        return None


def _delivery_failure(
    error: OSError | ValueError, *, stdout_failed: bool, stderr_failed: bool
) -> NoReturn:
    """Preserve exit 1 by protecting only failed process streams from shutdown retries.

    All known-failed streams use Click's non-owning proxy with flushing disabled,
    including mixed stdout/stderr failures with different errors. No stream is
    closed or replaced at the descriptor level; absent streams remain absent.
    """
    if stdout_failed and sys.stdout is not None:
        sys.stdout = cast(TextIO, _FailedFlushStream(sys.stdout))
    if stderr_failed and sys.stderr is not None:
        sys.stderr = cast(TextIO, _FailedFlushStream(sys.stderr))
    raise click.exceptions.Exit(1) from error


def _text_stream(name: Literal["stdout", "stderr"]) -> TextIO:
    """Borrow a Click stream without its implicit ASCII-to-UTF-8 upgrade.

    The process stream's encoding governs fallback and byte budgets; only an
    unspecified encoding defaults to UTF-8. No stream ownership is transferred.

    :raises OSError: If Python started without the requested process stream.
    """
    stream = sys.stdout if name == "stdout" else sys.stderr
    if stream is None:
        raise OSError(f"{name} is unavailable")
    return click.get_text_stream(name, encoding=stream.encoding or "utf-8")


class _InspectionTelemetryStream:
    """Borrow safe process stderr lazily for library-generated telemetry.

    Preserve generated JSON escapes and structural newlines. No borrowed stream
    is closed, and an absent/failed stderr never redirects telemetry to stdout.
    """

    def write(self, text: str) -> int:
        """Deliver bounded escaped chunks and flush before claiming success.

        :returns: Original character count, even when encoding-safe escapes expand it.
        :raises click.exceptions.Exit: With status 1 on absent/failed stderr.
        """
        try:
            stream = _text_stream("stderr")
            encoding = stream.encoding or "utf-8"
            for start in range(0, len(text), 512):
                for position, line in enumerate(text[start : start + 512].split("\n")):
                    if position and stream.write("\n") != 1:
                        raise OSError("Short telemetry write")
                    for chunk in _escape(line, encoding, False, json_encoded=True):
                        if stream.write(chunk) != len(chunk):
                            raise OSError("Short telemetry write")
            stream.flush()
        except (OSError, ValueError) as error:
            _delivery_failure(error, stdout_failed=False, stderr_failed=True)
        return len(text)

    def flush(self) -> None:
        """Flush borrowed stderr through the same controlled failure path, without closing it."""
        self.write("")


def bind_inspection_telemetry() -> None:
    """Select safe stderr for a requested scenario without taking over library telemetry lifetime."""
    config = get_telemetry_config()
    if config.time_scenarios_enabled:
        config.output = _InspectionTelemetryStream()


def emit_inspection_diagnostics(
    diagnostics: tuple[InspectionDiagnostic, ...], *, details: bool = False
) -> None:
    """Write safe plain stderr in SDK discovery order; root errors omit occurrence details.

    :raises click.exceptions.Exit: With status 1 if stderr itself cannot be delivered.
    """
    try:
        stream = _text_stream("stderr")
        encoding = stream.encoding or "utf-8"
        for error in diagnostics:
            parts = [f"{error.code}: ", error.message]
            if details:
                if error.location is not None:
                    parts.extend(("; Location: ", error.location))
                parts.extend(("; Source: ", str(error.source)))
            for part in parts:
                for chunk in _escape(part, encoding, False):
                    if stream.write(chunk) != len(chunk):
                        raise OSError("Short diagnostic write")
            if stream.write("\n") != 1:
                raise OSError("Short diagnostic write")
        stream.flush()
    except (OSError, ValueError) as error:
        _delivery_failure(error, stdout_failed=False, stderr_failed=True)


def write_inspection(
    result: LayoutInspectionResult | LayoutSummaryResult,
    *,
    as_json: bool,
    ascii_only: bool,
) -> None:
    """Serialize the selected SDK view, preflight stdout, deliver it and repeat diagnostics.

    Nonterminals and nonempty NO_COLOR suppress SGR, even with Click color forced.
    JSON is the SDK serializer verbatim. Borrowed streams are flushed, not closed.

    :raises LayoutInspectionError: If selected output exceeds its byte cap, before writing.
    :raises click.exceptions.Exit: With status 1 on invalid codec or output delivery failure.
    """
    try:
        stream = _text_stream("stdout")
        encoding = stream.encoding or "utf-8"
        if as_json:
            content = result.to_json(encoding=encoding)
        else:
            content = render_inspection(
                result,
                encoding=encoding,
                ascii_only=ascii_only,
                color=stream.isatty() and not os.environ.get("NO_COLOR"),
            )
        if stream.write(content) != len(content):
            raise OSError("Short stdout write")
        stream.flush()
    except (OSError, ValueError) as error:
        try:
            stderr = _text_stream("stderr")
            message = "".join(_escape(str(error), stderr.encoding or "utf-8", False))
            stderr.write(f"OUTPUT_WRITE_FAILED: {message}\n")
            stderr.flush()
        except (OSError, ValueError) as diagnostic_error:
            _delivery_failure(diagnostic_error, stdout_failed=True, stderr_failed=True)
        _delivery_failure(error, stdout_failed=True, stderr_failed=False)
    emit_inspection_diagnostics(result.diagnostics, details=True)

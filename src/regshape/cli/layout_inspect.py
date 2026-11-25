"""Render SDK-owned layout occurrences without reading files or rebuilding edges.

Text escapes untrusted strings before adding SGR spans. A counting pass enforces
the selected stream's encoded byte budget before materializing or writing stdout;
JSON delegates its shape and serialization to the SDK.
"""

from __future__ import annotations

import codecs
import json
import os
import unicodedata
from collections.abc import Iterable, Iterator, Mapping

import click

from regshape.libs.layout import (
    DescriptorView,
    IndexNode,
    InspectionDiagnostic,
    LayoutInspectionError,
    LayoutInspectionResult,
    LayoutNode,
    ManifestNode,
    OpaqueNode,
    SourceLocation,
)
from regshape.libs.layout.inspection_models import JsonValue, ObjectNode


def _escape(value: str, encoding: str, ascii_only: bool) -> Iterator[str]:
    """Reversibly escape controls, backslashes and unencodable characters in bounded chunks."""
    for start in range(0, len(value), 512):
        escaped = []
        for char in value[start : start + 512]:
            if char in "\\\n\t\r":
                escaped.append(
                    {"\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}[char]
                )
                continue
            unsafe = unicodedata.category(char) in ("Cc", "Cf", "Cs")
            try:
                char.encode("ascii" if ascii_only else encoding)
            except UnicodeEncodeError:
                unsafe = True
            if unsafe:
                code = ord(char)
                escaped.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
            else:
                escaped.append(char)
        yield "".join(escaped)


def _compact_json(value: JsonValue) -> Iterator[str]:
    """Yield compact, source-ordered platform JSON without copying maps or whole strings."""
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
        """Select encodable connectors; forced ASCII also escapes every non-ASCII value."""
        self.encoding = encoding
        self.ascii_only = ascii_only
        self.color = color
        try:
            "│├└─".encode(encoding)
            unicode_tree = not ascii_only
        except UnicodeEncodeError:
            unicode_tree = False
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

    def _safe(self, chunks: Iterable[str]) -> Iterator[str]:
        """Escape borrowed text chunks before they reach a styled span."""
        for chunk in chunks:
            yield from _escape(chunk, self.encoding, self.ascii_only)

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
        style: str = "36",
        slot: str = "",
    ) -> Iterator[str]:
        """Emit an unpadded label/colon and optional full value; None denotes a section."""
        yield from self._span((prefix,), "90")
        if slot:
            yield from self._span((slot,), "36")
        yield from self._span(self._safe((label,)), style)
        yield from self._span((":",), style)
        if value is not None:
            yield " "
            yield from self._span(
                self._safe((value,) if isinstance(value, str) else value), "39"
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
                nested, "Platform (platform)", _compact_json(descriptor.platform)
            )
        if descriptor.annotations is not None:
            yield from self._annotations(nested, descriptor.annotations)
        if descriptor.digest is not None:
            yield from self._errors(nested, descriptor.errors)

    def _properties(self, node: LayoutNode | ObjectNode, prefix: str) -> Iterator[str]:
        """Render intrinsic fields and embedded descriptors in the approved block order."""
        yield from self._field(prefix, "Location", node.location)
        if isinstance(node, LayoutNode):
            yield from self._field(
                prefix,
                "Layout format version (imageLayoutVersion)",
                node.image_layout_version,
            )
        if isinstance(node, (IndexNode, ManifestNode)):
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
            if isinstance(node, IndexNode) and node.manifests is not None:
                yield from self._field(
                    prefix,
                    "Referenced objects (manifests)",
                    "[]" if not node.manifests else None,
                )
                for position, descriptor in enumerate(node.manifests):
                    yield from self._descriptor(prefix + "  ", descriptor, position)
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
        self, node: LayoutNode | ObjectNode, prefix: str = "", last: bool = True
    ) -> Iterator[str]:
        """Walk only SDK-provided occurrences; stems distinguish internal nodes from leaves."""
        if isinstance(node, LayoutNode):
            name, style = "OCI layout", "39"
            base = prefix
        else:
            name, style = {
                "index": ("Index", "1;35"),
                "manifest": ("Manifest", "1;36"),
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
        if not isinstance(node, LayoutNode) and node.digest is not None:
            yield " "
            yield from self._span(self._safe((node.digest,)), "39")
        yield from self._end()
        children = (
            node.children
            if isinstance(node, (LayoutNode, IndexNode, ManifestNode))
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
                    and isinstance(child, (IndexNode, ManifestNode))
                    and child.children
                ):
                    yield from self._span((base + self.stem,), "90")
                    yield from self._end()

    def chunks(self, root: LayoutNode) -> Iterator[str]:
        """Yield one full expanded view without IO or an independent tree allocation."""
        yield from self._node(root)


def _preflight(
    chunks: Iterable[str], encoding: str, result: LayoutInspectionResult
) -> None:
    """Count final encoded bytes, including stateful encoding overhead; stop at cap plus one."""
    encoder = codecs.getincrementalencoder(encoding)()
    size = 0
    for chunk in chunks:
        size += len(encoder.encode(chunk))
        if size > result.limits.max_output_bytes:
            break
    else:
        size += len(encoder.encode("", final=True))
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
    result: LayoutInspectionResult, *, encoding: str, ascii_only: bool, color: bool
) -> str:
    """Return bounded expanded text; the caller owns stream/color policy and delivery.

    :raises LayoutInspectionError: Before materialization if encoded output exceeds
        the result's inclusive max_output_bytes budget.
    """
    tree = _Tree(encoding, ascii_only, color)
    _preflight(tree.chunks(result.root), encoding, result)
    return "".join(tree.chunks(result.root))


def emit_inspection_diagnostics(
    diagnostics: tuple[InspectionDiagnostic, ...], *, details: bool = False
) -> None:
    """Write safe plain stderr in SDK discovery order; root errors omit occurrence details.

    :raises click.exceptions.Exit: With status 1 if stderr itself cannot be delivered.
    """
    stream = click.get_text_stream("stderr")
    encoding = stream.encoding or "utf-8"
    try:
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
    except (OSError, UnicodeError) as error:
        raise click.exceptions.Exit(1) from error


def write_inspection(
    result: LayoutInspectionResult, *, as_json: bool, ascii_only: bool
) -> None:
    """Serialize the selected SDK view, preflight stdout, deliver it and repeat diagnostics.

    Nonterminals and nonempty NO_COLOR suppress SGR, even with Click color forced.
    JSON is the SDK serializer verbatim. Borrowed streams are flushed, not closed.

    :raises LayoutInspectionError: If selected output exceeds its byte cap, before writing.
    :raises click.exceptions.Exit: With status 1 on output delivery failure.
    """
    stream = click.get_text_stream("stdout")
    encoding = stream.encoding or "utf-8"
    if as_json:
        content = result.to_json()
        _preflight(
            (content[start : start + 512] for start in range(0, len(content), 512)),
            encoding,
            result,
        )
    else:
        content = render_inspection(
            result,
            encoding=encoding,
            ascii_only=ascii_only,
            color=stream.isatty() and not os.environ.get("NO_COLOR"),
        )
    try:
        if stream.write(content) != len(content):
            raise OSError("Short stdout write")
        stream.flush()
    except (OSError, UnicodeError) as error:
        stderr = click.get_text_stream("stderr")
        try:
            message = "".join(_escape(str(error), stderr.encoding or "utf-8", False))
            stderr.write(f"OUTPUT_WRITE_FAILED: {message}\n")
            stderr.flush()
        except (OSError, UnicodeError) as diagnostic_error:
            raise click.exceptions.Exit(1) from diagnostic_error
        raise click.exceptions.Exit(1) from error
    emit_inspection_diagnostics(result.diagnostics, details=True)

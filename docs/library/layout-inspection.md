# Local layout inspection SDK

<!-- doc-type: reference -->

This additive software development kit (SDK) API targets unreleased **0.2
development builds**. Import from `regshape.libs.layout`; no command-line
interface (CLI), registry client, credentials or container daemon is needed.
It inspects a local Open Container Initiative (OCI) image-layout directory.
Archives, unlisted blobs, payload extraction and cryptographic verification
are not supported by this inspector.

## `inspect_layout`

```python
import sys

from regshape.libs.layout import InspectionLimits, LayoutInspectionError, inspect_layout

try:
    result = inspect_layout("./layout", limits=InspectionLimits())
except LayoutInspectionError as error:
    for diagnostic in error.diagnostics:
        print(diagnostic.code, diagnostic.message, diagnostic.source, file=sys.stderr)
else:
    # Incomplete descendants still have a trustworthy partial hierarchy.
    print(result.to_json(), end="")
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message, diagnostic.source, file=sys.stderr)
```

Signature: `inspect_layout(path: str | pathlib.Path, *, limits:
InspectionLimits | None = None) -> LayoutInspectionResult`.

The path must identify a directory with a valid `oci-layout` marker and
`index.json`. Linux/macOS with handle-relative, no-follow regular-file opens
are supported. Windows or unavailable safety primitives produce
`UNSUPPORTED_PLATFORM` before reads. No write access, private 0700 namespace
or publication primitives are required. Root/descendant symlinks and parent
traversal are rejected. Supply stable files: detected metadata changes are
errors, but the call is not an atomic filesystem snapshot.

The inspector opens only bounded marker/index/manifest metadata. Configs,
layers and content stay opaque: only their existence and stat size are observed.
Each valid target address is observed once, including cached failures; each
distinct supported metadata document is read/parsed once. Repeated occurrences
still expand in source order and retain their own descriptor comparisons.
`read_index`, authoring and recursive verification APIs remain unchanged.

## Results and source ownership

`LayoutInspectionResult.root` is an immutable `LayoutNode` with exactly one
entry-point `IndexNode`. Its descendants are `IndexNode`, `ManifestNode`,
`OpaqueNode` (config/layer/content) or `UnknownNode`. Every occurrence retains
its relative location and full digest where applicable; the layout location
is absolute. Tuples preserve source ordering. Read-only metadata maps preserve
present empty annotations/platform extensions without copying them per reuse.

Descriptors belong to their source document's `manifests`, `config`, `layers`
or `subject` block. `DescriptorView.source` is a `SourceLocation` with relative
document and RFC 6901 JSON pointer. `incoming` and `source_position` identify
the referring slot on a child; they are SDK provenance, not extra JSON fields.
Invalid slots retain their position and independently valid display properties.
They never manufacture a digest or unsafe child path.

Explicit subjects stay full source descriptors. They are not containment
children: their target is not followed, statted, or included in stored-object
totals merely because it is a subject. Reverse referrers are never discovered.

`IndexNode.referrer_list` optionally holds occurrence-local
`ReferrerListEvidence` for an actual entry-point tag-schema declaration.
SHA-256 tags identify their full declared subject. SHA-512 prefixes resolve
only from a unique matching full digest in reachable descriptor/subject
metadata; zero or multiple candidates remain unresolved, not corrupted.
An explicit member subject conflicting with a resolved declaration is an
error, never rewritten. Recognition does not invent absent member subjects
or add hidden expanded JSON fields.

## Diagnostics and limits

`result.complete` means **structural inspection** encountered no errors or
traversal limits. It is not proof of digest authenticity, signature trust,
payload validity or registry completeness.

Invalid arguments/budget types raise `ValueError` before IO. Invalid roots
raise `LayoutInspectionError` with `INVALID_LAYOUT` and the specific typed
cause when applicable; no tree is returned. Descendant failures return
`complete == False` and source-local errors, with traversal-ordered typed
`result.diagnostics`. Codes distinguish missing/unreadable objects, malformed
documents, invalid descriptors, unsupported required types, conflicting
descriptors, containment cycles, detected mutation and limits.

`InspectionLimits` is frozen and accepts only positive non-boolean integers.
`None` selects defaults, not unlimited operation. All caps are inclusive:
raw marker bytes, per-document bytes, aggregate distinct metadata bytes,
JSON container depth (including ignored extensions), attempted addresses,
source descriptor slots (including subjects), display depth, occurrences,
diagnostics and encoded output bytes. The final diagnostic slot is reserved
for an explicit diagnostic-budget `LIMIT_REACHED` marker. Traversal limits
leave a marked partial result, not empty-success metadata; root budget
failures can prevent any result.

See the [approved budget table](../../specs/layout/layout-inspect.md#d5-proposed-measurable-limits-and-support)
for all names, defaults and accounting. To inspect deliberately larger input,
pass a reviewed larger named budget, for example
`InspectionLimits(max_occurrences=200_000)`. There is no unlimited mode,
automatic retry, repair, or network fallback.

## Serialization

`result.to_dict()` returns an independent JSON-compatible recursive copy.
`result.to_json()` returns two-space, ASCII-safe JSON plus one final line feed.
Both preflight the complete encoded JSON byte budget before allocating the
copy/string and raise `LayoutInspectionError` with `OUTPUT_LIMIT` if it cannot
fit. Inspection itself does not materialize expanded output.

The top-level JSON value is the layout node, with no envelope, format field,
node/edge tables or second diagnostics collection. Optional absent fields are
omitted, not null; present allowed empty maps/arrays/strings remain present.
Unsupported descriptor fields are not dumped. Raw logical annotation values
are preserved with standard JSON escaping, not terminal presentation escaping.
Output implements the expanded branch of the
[version-1 schema](../../specs/layout/layout-inspect.schema.json)
(`urn:regshape:layout-inspect:1`).

The API does not print or log metadata; the example explicitly chooses to print.
Annotations can be sensitive, so choose output retention deliberately.
Summary APIs and the inspect CLI are separate delivery items, not capabilities
introduced by this SDK slice.

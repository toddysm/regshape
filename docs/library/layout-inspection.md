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
Bytes returned before a stream failure still spend the aggregate metadata
budget. Required files disappearing after their read are reported as
`CHANGED_DURING_READ`, distinct from files missing at initial acquisition.
Post-read directory/ancestor renames are likewise detected association changes.
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
An index/manifest descriptor family conflicting with the observed document
shape is diagnosed even when the document omits optional `mediaType`; that
absent field is not filled in.

Explicit subjects stay full source descriptors. They are not containment
children: their target is not followed, statted, or included in stored-object
totals merely because it is a subject. Reverse referrers are never discovered.

`IndexNode.referrer_list` optionally holds occurrence-local
`ReferrerListEvidence` for an actual entry-point tag-schema declaration.
SHA-256 tags identify their full declared subject. SHA-512 prefixes resolve
only from a unique matching full digest in reachable descriptor/subject
metadata; zero or multiple candidates remain unresolved, not corrupted.
Intrinsically malformed documents supply no candidates, so rejected metadata
cannot resolve a prefix or make valid evidence ambiguous.
Invalid list-valued `config`/`subject` slots likewise supply no candidates.
Prefix uniqueness is cached once per source candidate, not rescanned for each
repeated tagged occurrence.
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
No-tree root failures obey the same diagnostic reservation. With a cap of one,
the sole diagnostic is `LIMIT_REACHED`, whose message includes the suppressed
root cause.

See the [approved budget table](../../specs/layout/layout-inspect.md#d5-proposed-measurable-limits-and-support)
for all names, defaults and accounting. To inspect deliberately larger input,
pass a reviewed larger named budget, for example
`InspectionLimits(max_occurrences=200_000)`. There is no unlimited mode,
automatic retry, repair, or network fallback.

## Serialization

`result.to_dict()` returns an independent JSON-compatible recursive copy.
`result.to_json(*, encoding="ascii")` returns two-space, ASCII-safe JSON plus one
final line feed. The optional encoding selects the destination text codec for
byte-budget accounting, including encoding state/BOM; it does not change the
returned string, schema or escaping. Pass the actual output stream encoding
when it is not ASCII/UTF-8. Invalid, non-text and non-stream domain-name codecs
(`idna` and `punycode`) raise `ValueError` before the projection is consumed.
UTF-7 accounting preserves whole-string shift boundaries without buffering
an entire shifted run.
Both methods preflight the complete encoded JSON byte budget before allocating the
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
The inspect CLI is a separate delivery item.

## `summarize_layout`

```python
from regshape.libs.layout import inspect_layout, summarize_layout

inspection = inspect_layout("./layout")
summary = summarize_layout(inspection)
print(summary.to_json(), end="")
```

Signature: `summarize_layout(result: LayoutInspectionResult) ->
LayoutSummaryResult`. Invalid argument types raise `ValueError`.
This is a pure in-memory operation over inspection facts: no filesystem
rescan, config/layer decoding, registry discovery, printing or Click imports.
It does not first materialize expanded output.

`summary.root` is a `SummaryLayoutNode`. Its `SummaryIndexNode` descendants
retain **every index occurrence**, intrinsic metadata, source descriptor
blocks, errors and occurrence-local referrers evidence. At each index,
contiguous complete manifest occurrences of the same evidence-based category
become `SummaryGroup` values. Generic artifacts additionally require exactly
the same own `artifactType`. A different category/type, index, malformed
manifest slot or incomplete manifest ends a run; errors and unknown documents
stay expanded.
The original inspection is unchanged.

Categories are `platform-images`, `attestations`, `signatures`, `lifecycle`,
`artifacts`, and `unclassified`. Exact own artifact types and source descriptor
attestation annotations provide artifact evidence; another own artifact type
cannot be overridden by an attestation annotation. Other nonempty own types
remain literal artifacts, not guessed SBOMs or signatures. Platform images
require a compatible OCI/Docker image-manifest/config pair and source platform
with nonempty, non-`unknown` OS/architecture. Unknown/unknown alone is **not**
attestation evidence; absent platform fields are never invented.
Classification asserts neither authenticity nor payload validity.

Groups expose `positions`, `references`, `unique_manifests`,
`config_references`, `layer_references`, `layer_count_distribution`,
`unique_objects`, `stored_bytes`, and optional `artifact_type`.
`members` borrows complete `ManifestNode` occurrences for full source evidence,
including explicit subjects and each incoming descriptor; it is SDK-only,
not a hidden expanded tree in JSON. Mixed attestation evidence retains its
individual own types in members; a group-level artifact type is emitted only
when uniform. Layer-count distributions count every displayed occurrence:
`{"1": 1, "2": 1}` is not “each has one layer.” Shared content means group
unique totals are **not additive**.

`summary.root.statistics` is a frozen `SummaryStatistics`:

| SDK field | JSON key | Meaning |
| --- | --- | --- |
| `unique_objects` | `uniqueObjects` | Distinct existing contained digest addresses; excludes marker, entry point, missing/unlisted/subject-only targets. |
| `object_references` | `objectReferences` | Config/layer/manifest slots in distinct successfully parsed source documents, including repeated or missing targets. |
| `subject_references` | `subjectReferences` | Separate subject slots from those documents, never containment references. |
| `stored_bytes` | `storedBytes` | Observed stat sizes once per existing contained address, not descriptor-declared sizes. |

`summary.diagnostics`, `.complete` and `.limits` preserve the inspection's
structural failures and budgets. Incomplete results have observed totals and
`statisticsComplete: false`; grouping cannot repair them. Even complete totals
do not prove cryptographic integrity or registry-wide referrer completeness.

`summary.to_dict()` and `.to_json()` follow the same independent-copy,
ASCII/LF and preflight contracts as expanded serialization, using **only the
summary view's** byte budget. They implement the summary branch of the
[version-1 schema](../../specs/layout/layout-inspect.schema.json).
Summary JSON adds root statistics and, only on recognized tagged index
occurrences, an `association` block with source/tag/status and either a full
resolved digest or unresolved SHA-512 algorithm/prefix. This does not invent
an OCI subject, reverse subject direction or imply complete registry discovery.

## Offline regression and resource evidence

The [integrated tests](../../src/regshape/tests/test_layout_inspection_integration.py)
exercise stored simple, nested/multi-root, subject-linked, shared, mixed/unknown
and incomplete layouts through both SDK views and all four CLI forms. The
[fixture builder](../../src/regshape/tests/layout_inspection_fixtures.py) also
retains the digest-pinned BusyBox metadata reference and reproduces the locked
human tree. Its local copy replaces opaque content with two-byte stand-ins and
adjusts descriptor sizes; it is not a digest-valid downloaded image. No image
layers are downloaded or committed, and inspection must not open those stand-ins.

Run the [resource harness](../../src/regshape/tests/benchmark_layout_inspection.py)
from the checkout with installed development dependencies:

```shell
python -m regshape.tests.benchmark_layout_inspection --output layout-baseline.json
```

It verifies the exact wide/reused-index counts, then records one warm-up and
five fresh-process samples per view for BusyBox and both stress shapes. Reports
retain metadata/output bytes, native open/stat/read/parse counts, runtime,
hardware, filesystem, elapsed median/maximum and peak RSS in bytes. Elapsed
time covers the instrumented CLI invocation, excluding interpreter startup and
fixture discovery; RSS covers the entire fresh process, including imports and
output capture. Linux uses post-exec `VmHWM` rather than a potentially inherited
`getrusage` high-water mark; macOS uses `getrusage` in byte units.
Reads use stable, warm-cache files, not a cold-cache claim.
Linux CI evidence and local Mac evidence are separate measurements, not a
cross-machine ranking.

The I/O guard counts actual native metadata opens and observes direct ordinary,
positional and vectored read bytes, including rereads on an existing descriptor.
It permits one Python file wrapper per native metadata open and rejects extra
raw/buffered descriptor wrappers or descriptor duplication that could hide reads.
Negative sampler regressions verify the open/byte-budget or wrapper gates, using
injections that otherwise preserve the SDK output.
Binary metadata streams are observed at factory return, before caller
constructors or the `SafeDirectory` context can read them; the context reuses
that observer rather than double counting. Text metadata factories are rejected
because these counters require exact binary bytes. Standard output and in-memory
streams retain their normal behavior.

A baseline report says `performance_gate: not-approved; measurements only`.
It does **not** pass the release gate. After the owner approves quantitative
limits on #216, rerun with `--thresholds approved-limits.json`. This caller-supplied
JSON must contain the matching `fixture_revision`, `platform` (from the report's
environment), canonical #216 `approval` comment URL, and `limits` covering
every `shape/view` pair. Each pair requires positive finite `elapsed_seconds`
and `peak_rss_bytes` caps. Equality passes; an exceeded maximum exits 1.
The caller must verify the approval's authenticity and scope before using it.
Fixture/platform drift or missing limits is an error, never an implicit pass.

The committed [Linux limits](../../src/regshape/tests/fixtures/layout-inspection/linux-thresholds.json)
and [local Mac limits](../../src/regshape/tests/fixtures/layout-inspection/macos-thresholds.json)
reference the [owner's renewed approval](https://github.com/toddysm/regshape/issues/216#issuecomment-7179703954).
Linux CI enforces its approved limits and retains the full report even on failure.
Run the Mac rerun locally, not on a hosted Mac runner:

```shell
python -m regshape.tests.benchmark_layout_inspection --thresholds src/regshape/tests/fixtures/layout-inspection/macos-thresholds.json --output macos-inspection-approved.json
```

Both files bind a specific fixture/instrumentation revision; fixture-builder,
I/O guard or pinned metadata changes require updated evidence and renewed owner
approval, not silently relaxed caps.

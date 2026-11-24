# Local layout inspection: product requirements and engineering design

- **Status:** Proposed contracts, awaiting owner approval; not implemented.
- **Tracking:** [#130](https://github.com/toddysm/regshape/issues/130).
- **Design item:** [#211](https://github.com/toddysm/regshape/issues/211).
- **Updated:** 2026-11-24.
- **Planning format:** Full. Testing/review mode: GitHub-hosted.
- **Locked prerequisite:** [Approved human-output contract](../cli/layout-inspect.md),
  published through [#218](https://github.com/toddysm/regshape/pull/218).

## Approval boundary

The owner agreed to these additional requirements during #211:

1. JSON has the same information and hierarchy as the corresponding text view.
   There are no separate node/edge collections and no extra undisplayed
   descriptor fields.
2. Recognize local tag-schema referrer lists in 0.2.
3. Explicit summary mode retains every index and its metadata, condenses other
   branches into evidence-based groups and truthful counts, and has matching
   hierarchical JSON.
4. Incomplete layouts expose trustworthy partial results and explicit errors,
   stderr diagnostics and exit 1. An invalid layout root produces no tree.

The concrete types, schema, classification rules, limits, platform policy and
examples below are **proposals**, not approvals inferred from those agreements.
Approve this document and the current delivery breakdown before implementation.
Merging a design requires separate authorization; no release is authorized.
The existing human-output document and its complete real BusyBox baseline are
unchanged. This document supplies its remaining contracts, not a replacement
expanded display.

## Part I: Product requirements

### 1. Executive overview

Registry developers and artifact consumers need to understand a downloaded
Open Container Initiative (OCI) image layout without opening digest-named files
one by one. Add local, read-only SDK inspection and a CLI tree, matching JSON,
and optional summaries. Complete the contracts now so the SDK and CLI delivery
items can implement one agreed interpretation rather than inventing different
graph, counting or failure semantics.

### 2. Problem and evidence

The concrete owner request in #130 concerns a Debian layout containing an
original index, eight platform images and seven attestations. The approved
real BusyBox snapshot independently exercises two platform images, four
referrers, shared content and subjects pointing to a multi-platform index.
The [verified baseline facts](../cli/layout-inspect.md#facts-and-verification-of-this-fixture)
record 15 unique digest-addressed objects and 11,277,619 content bytes.
Neither example is a hard-coded product shape.

> NOTE: External customer names, adoption research and business-volume targets
> have not been supplied. These examples establish the owner's functional need,
> not evidence of three customers or a market-wide demand claim.

### 3. Customer scenarios

**Inspecting a download.** A registry developer receives a layout and wants to
understand which index owns its descriptors. Previously they opened index.json,
followed digests by hand and risked attributing descriptor annotations to the
wrong file. They run `layout inspect -p DIR`, see every index and its own
metadata, and follow the connected image/config/content hierarchy. A second
reference to a digest remains a separate occurrence, not a second stored file.

**Automating the same investigation.** A Python consumer calls `inspect_layout`
and serializes the result, or a script adds `--json`. Both receive the same
ordered hierarchy the developer sees, not a graph requiring a separate join
between node and edge tables. Absent optional text properties remain absent
in JSON. A consumer requesting a summary gets the same condensed information
as `--summary`, including mixed groups and the distinction between references
and distinct stored objects.

**Investigating damage and old referrers.** A registry implementer inspects an
incomplete layout containing a tagged referrers-list index. The tool preserves
valid siblings, marks missing content explicitly, and exits nonzero. It
recognizes the locally declared list without claiming that every listed
manifest actually has that subject or that the registry's referrers set is
complete. The developer repairs the missing file and repeats the read-only
inspection; there is no automated repair or network fallback.

### 4. Outcomes, priorities and scope

All of R1-R10 are required for the 0.2 feature:

| Requirement | Outcome |
| --- | --- |
| R1 | Validate an explicit local directory and inspect all reachable indexes, manifests and opaque config/content locations. |
| R2 | Reproduce the locked expanded hierarchy, metadata ownership, positions, ordering, colors and full values. |
| R3 | Offer SDK-owned typed results and hierarchical JSON with text/JSON information parity. |
| R4 | Retain occurrence-specific descriptors and subjects without inventing reverse links or duplicate storage. |
| R5 | Provide SDK summaries with every index, evidence-based categories, mixed shapes and truthful counts. |
| R6 | Expose invalid roots, partial failures, conflicts, cycles and limits without empty-success fallbacks. |
| R7 | Bound metadata, graph expansion and output; handle hostile terminal text and encoding safely. |
| R8 | Recognize local tag-schema referrers lists without network access or guessed subjects. |
| R9 | Keep inspection read-only, Click-independent and usable without credentials, a daemon or payload reads. |
| R10 | Preserve the 0.2 show-warning / 0.3 show-removal schedule and document migration. |

Non-goals: archives as inspection input; directory scans for unlisted blobs;
registry-wide referrers discovery; fetching descriptor URLs; decoding embedded
descriptor data or config/layer payloads; extraction, execution, repair; digest,
signature, trust or SBOM verification; an exhaustive descriptor dump. Additional
descriptor fields are not added to either expanded text or JSON in this design.
There is no new full-verification flag.

### 5. Existing solutions and reuse

`read_index` reads the entry-point index but does not inspect the closure.
Recursive artifact validation already has strict JSON, safe digest parsing and
safe-storage foundations, but validates and hashes content and follows subjects.
Reuse applicable primitives, **not** its full verifier as the inspector.
`layout status` remains authoring-state inspection and `layout validate
--recursive` remains verification. Do not weaken either operation to add display.

### 6. External alternatives

Manual JSON/file inspection remains a workaround. No competitive superiority
claim is made; this proposal solves the concrete SDK/CLI interpretation problem
in #130 rather than attempting a market comparison.

### 7. Desired experience

```shell
regshape layout inspect --path ./layout
regshape layout inspect -p ./layout --json
regshape layout inspect -p ./layout --summary
regshape layout inspect -p ./layout --summary --json
regshape layout inspect -p ./layout --ascii
```

Default: expanded output, with the locked color policy. `--summary` explicitly
selects the condensed view; `--json` serializes whichever view was selected.
`--ascii` affects text only and is rejected with `--json` as a usage error.
There is no new color flag. SDK callers have the same capabilities without CLI
imports. UI, Bicep, Terraform and Helm are not applicable: no hosted resource
or deployment surface is introduced.

Missing-file diagnostics identify the expected relative path and source
descriptor. Repair or reacquire the local file, then rerun. Permission failures
are distinguished from absence; do not recommend privilege escalation as a
generic remedy. A limit diagnostic identifies the precise budget and boundary.
SDK callers may explicitly supply larger validated budgets; there is no
unlimited mode or automatic retry.

### 7A. Output examples

The complete expanded success text is the unchanged
[144-line real baseline](../cli/layout-inspect.md#baseline-real-image-example).
The following smaller deterministic example specifies the JSON contract without
reproducing that large fixture.

**Tiny fixture.** Store the UTF-8 bytes `{}` with no newline at
`blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`.
Create a manifest from this object using
`json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")`:

<!-- inspect-fixture: manifest -->
```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.example.payload",
  "config": {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2
  },
  "layers": [{
    "mediaType": "application/octet-stream",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2
  }]
}
```

The manifest is 420 bytes with digest
`sha256:f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f`.
The marker is `{"imageLayoutVersion":"1.0.0"}`; index.json is an OCI index
with schemaVersion 2 and one descriptor for that manifest (size 420).

**Complete expanded JSON, stdout, exit 0:**

<!-- inspect-example: expanded -->
```json
{
  "kind": "layout",
  "location": "/example/layout",
  "imageLayoutVersion": "1.0.0",
  "children": [{
    "kind": "index",
    "location": "index.json",
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [{
      "digest": "sha256:f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f"
    }],
    "children": [{
      "kind": "manifest",
      "digest": "sha256:f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f",
      "location": "blobs/sha256/f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f",
      "schemaVersion": 2,
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "artifactType": "application/vnd.example.payload",
      "config": {
        "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      },
      "layers": [{
        "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      }],
      "children": [{
        "kind": "config",
        "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "location": "blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      }, {
        "kind": "content",
        "position": 0,
        "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "location": "blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      }]
    }]
  }]
}
```

`kind`, `position` and `children` encode the text's headings, layer positions and
connected hierarchy. They are not additional graph facts. Descriptor array
positions are native JSON array positions. Source-owned descriptor metadata
remains in `manifests`, `config`, `layers` or `subject`, not copied to children.
There are no `nodes`, `edges`, normalized IDs or references requiring joins.

**Partial JSON, stdout, exit 1.** Delete the shared empty blob from the tiny
fixture. The full result is the expanded example above with each opaque
occurrence replaced by its corresponding object below; everything else stays
identical. This replacement rule defines a complete partial result, not a
second successful tree with missing branches.

<!-- inspect-example: missing-config -->
```json
{
  "kind": "config",
  "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "location": "blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "errors": [{
    "code": "MISSING_OBJECT",
    "message": "Referenced file is missing",
    "source": "blobs/sha256/f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f#/config"
  }]
}
```

<!-- inspect-example: missing-content -->
```json
{
  "kind": "content",
  "position": 0,
  "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "location": "blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "errors": [{
    "code": "MISSING_OBJECT",
    "message": "Referenced file is missing",
    "source": "blobs/sha256/f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f#/layers/0"
  }]
}
```

The corresponding text fragment at the config occurrence is:

```text
Config sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
  Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
  Missing file (MISSING_OBJECT): Referenced file is missing
    Source: blobs/sha256/f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f#/config
```

The error label, not digest or source value, is red when color is enabled.
Stderr contains one line per occurrence, in traversal order:

```text
MISSING_OBJECT: Referenced file is missing; Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a; Source: blobs/sha256/f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f#/config
MISSING_OBJECT: Referenced file is missing; Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a; Source: blobs/sha256/f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f#/layers/0
```

**Invalid root, either output mode:** stdout is empty, exit 1; stderr:

```text
INVALID_LAYOUT: Required layout marker oci-layout is missing
```

### 8. Success measures and guardrails

Before 0.2 delivery: exact baseline reproduction; text/JSON semantic parity for
every deterministic fixture; zero unexpected network/credential/daemon calls;
zero writes and zero opaque-payload reads; exact at-limit/over-limit outcomes;
truthful complete and incomplete counts. Baseline: this capability is not
implemented. These are acceptance gates, not measured improvements in adoption.
Before release, #216 records metadata bytes, elapsed time and peak memory for
the real baseline and bounded stress fixtures on Linux and the local Mac.
No unmeasured latency percentile, availability SLA or market metric is promised.

### 9. Pricing

No pricing/billing impact: this is a local Python package capability, not a
metered service.

### 10. Performance and scale

Expected examples range from one manifest to the pinned 15-object snapshot.
The supported ceiling is the independent limits in D5, not a promise that all
ceilings can be saturated simultaneously. Metadata reads and stat calls scale
with distinct reachable objects; expansion and output scale with occurrences.
No layer-size-dependent read/hashing cost is allowed. Filesystem latency is
caller/environment dependent; unsupported or stalled filesystems do not gain
a fabricated timeout or performance guarantee.

### 11. Observability and troubleshooting

The customer surfaces are exit status, node-local errors, stderr and structured
SDK diagnostics. Use the taxonomy in PM-5: restore missing files, correct
permissions, replace malformed metadata, resolve conflicting descriptors, or
increase a named budget deliberately. Structural success says nothing about
signature trust, digest authenticity or registry completeness. There is no
new remote logging, health service or automatic telemetry upload.

### 12. Security and privacy

The process uses existing local filesystem permissions only. It never prompts
for authentication, reads Docker credentials, changes ACLs or opens a network
path. Treat filenames, JSON, annotations and tags as untrusted. Bound reads;
reject path traversal, symlinks and nonregular inputs; safely escape display
values. Inspection intentionally returns requested metadata, which may contain
sensitive annotations: do not duplicate that content into automatic logs.
The caller controls stdout retention. No new keys, encryption scheme,
cross-region storage or compliance certification is introduced.

### 13. Risks

| Risk | Mitigation / owning slice |
| --- | --- |
| Reusing verification accidentally hashes large layers | Reuse narrow primitives; IO spies and opaque-file read prohibition, #212/#216. |
| Repeated indexes cause exponential output | Separate occurrence/depth/output budgets, #212/#214. |
| Summary mistakes reference counts for stored copies | Unique physical-address facts plus occurrence accounting, #213. |
| Tag conventions are mistaken for authenticated subjects | Source-labeled association evidence; preserve actual subject, #212/#213. |
| Hostile annotations change terminal state | Reversible escaping before color; ANSI span tests, #214. |
| Platform-specific safety primitives are unavailable | Fail closed before reads; explicit Linux/macOS matrix, #212/#216. |

### 14. Rollout and milestones

Design approval is gate 1, authorized implementation is gate 2, reviewed/checked
delivery of all required 0.2 items and documentation is gate 3, and owner release
authorization is gate 4. No dated delivery commitment or preview customer cohort
has been supplied. Deliver SDK inspection, SDK summaries/expanded CLI, summary
CLI and show warning, integrated regressions, then final docs in dependency
order. 0.3 removal follows an actual 0.2 warning release, not merely its PR merge.
Inventory remains Planned during this design gate.

## Part II: Engineering design

### A. Abstract

Add a bounded local inspector that separates physical content identity from
source occurrences while exposing a recursive hierarchy rather than public
node and edge tables. It reads the layout marker, entry-point index and
reachable supported manifest/index documents, and obtains opaque-file existence
and size without reading their content. Source descriptors retain their own
metadata and order. The SDK returns immutable inspection results, occurrence
provenance and diagnostics; it also classifies summaries and recognizes locally
declared referrers-list tags. JSON is a projection of exactly the selected text
view, with a versioned external schema and no hidden graph metadata. The CLI
only selects a view, renders safely, emits diagnostics and maps outcomes to exit
codes. Missing or invalid descendants remain explicit partial results, while an
invalid root or unsupported safe-open platform fails before emitting a tree.
This design preserves the approved expanded display, legacy show stdout and
existing verification APIs. It does not retrieve or verify registry content.

### B. Prerequisites

#210 is complete through merged #218. The existing display specification and
the requirements in Part I are inputs. This proposal reconciles the older
"normalized graph" wording in #211/#212 with the owner's explicit hierarchical
JSON requirement: normalization means consistent field selection and types,
not flat graph tables. The current breakdown requires approval with this design.

### C. Introduction

The current library can create layouts and verify artifact graphs but lacks
display-oriented bounded inspection and partial results. A separate inspection
policy is needed before implementing the already-filed SDK/CLI items. Extending
the full verifier or rebuilding graph facts in Click would violate either the
metadata-only cost boundary or SDK ownership.

### D. Design

#### D1. Components and flow

```text
Existing local layout
        |
        v
SDK safe opens + bounded strict metadata parser
        |
        v
SDK occurrence builder + private per-digest facts/cache
        |
        +--> expanded typed result --> hierarchical JSON serializer
        |
        +--> SDK summary/classification --> summary JSON serializer
        |
        v
CLI text rendering / escaping / color / streams / exit adapter
```

Implement under the existing layout SDK, exporting new functions from
`regshape.libs.layout`; proposed implementation modules are `layout/inspection.py`
and `layout/inspection_models.py`. The layout CLI consumes these APIs.
Reuse safe read-only directory opens and strict JSON/digest parsing from
[artifact storage](../../src/regshape/libs/artifacts/storage.py),
[graph primitives](../../src/regshape/libs/artifacts/graph.py) and
[policy models](../../src/regshape/libs/artifacts/models.py) where their
contracts fit. Do not invoke publication/private-namespace setup, content
verification or the subject-following strict walker.

Each call opens its handles, builds bounded immutable facts/results and closes
all handles before returning. No persistent cache, repair, background process,
new runtime package dependency or durable state is introduced.

#### D2. Public API and typed results

Proposed signatures (contracts, not implementation stubs):

```python
inspect_layout(
    path: str | pathlib.Path,
    *,
    limits: InspectionLimits | None = None,
) -> LayoutInspectionResult

summarize_layout(result: LayoutInspectionResult) -> LayoutSummaryResult
```

| Type / member | Contract |
| --- | --- |
| `InspectionLimits` | Frozen positive, non-boolean integer budgets from D5. `None` selects defaults, not unlimited operation. |
| `LayoutInspectionResult.root` | One immutable `LayoutNode` containing one entry-point `IndexNode` and recursive occurrences. |
| `IndexNode`, `ManifestNode`, `OpaqueNode`, `UnknownNode` | Typed discriminated occurrence variants; intrinsic fields and source-owned descriptor views remain distinct. |
| `DescriptorView` | Full digest plus optional platform/annotations, exactly the expanded descriptor fields; source array position is retained by its containing tuple. An invalid slot retains an errors-only descriptor view at the same position, never a fabricated digest. |
| `SourceLocation` | Relative document path and RFC 6901 JSON pointer; provenance, not a fabricated OCI property. |
| `InspectionDiagnostic` | Stable code, message, source location, and affected occurrence location when available. Diagnostics attach to the affected node/descriptor owner. |
| `ReferrerListEvidence` | Occurrence-local tag, source descriptor, parsed algorithm/prefix, resolution status and full subject digest only when resolvable. Not an intrinsic index subject. |
| `LayoutInspectionResult.diagnostics` | Traversal-ordered projection of node-local errors; no second discovery or duplicated JSON collection. |
| `LayoutInspectionResult.complete` | False when any structural error or traversal limit exists; not a verification/trust/completeness-of-registry claim. |
| `LayoutSummaryResult.root` | Same layout/index ancestry and index metadata, replacing eligible contiguous manifest runs with typed `SummaryGroup` values. |
| `LayoutSummaryResult.diagnostics`, `.complete` | Preserve inspection errors/completeness; grouping never heals a partial result. |
| Both result types: `.to_dict()` | Independent JSON-compatible recursive projection validated by the applicable schema branch; absent fields omitted, no terminal escaping. Preflight the JSON byte budget before allocating the independent copy. |
| Both result types: `.to_json()` | Two-space, ASCII-safe JSON plus a final LF; preserves insertion/source order, refuses nonfinite values, enforces output budget. |

SDK-only typed provenance, structural validation facts and referrers evidence
need not be emitted as hidden JSON properties: the serialized view includes only
what its corresponding text view displays. Private cached facts preserve
descriptor size/type evidence and distinct-file sizes for summary accounting;
the CLI must neither access those facts nor reopen any file.

Bad argument types/invalid budgets raise `ValueError` before IO. Invalid roots,
unsupported platforms and serialization-output limits raise a new
`LayoutInspectionError` derived from `ArtifactError`, carrying typed diagnostics.
Descendant problems return an explicitly incomplete result, not an exception
masquerading as success. The SDK never prints, logs metadata automatically,
imports Click or exits. It performs no automatic retries.

#### D3. Hierarchical JSON and presence

The [Draft 2020-12 schema](layout-inspect.schema.json) is version 1. Its `$id`
identifies the format **in the schema artifact**, not as a hidden field in
stdout. A schema-changing future release must publish a new immutable versioned
schema identifier and document the migration; do not reinterpret version 1.
No source `schemaVersion` is repurposed as the inspection-format version.

The top-level JSON value is the layout node itself, not an envelope.
`children` reflects display branches. Every repeated object occurrence is
expanded again, even if its parsed document came from cache. Descriptor
properties stay in their owner's object; opaque children do not gain decoded
payload fields or incoming annotations.

Required layout properties: kind, absolute location, imageLayoutVersion and one
entry-point child. Every object occurrence has kind and relative location.
Digest-addressed occurrences have their full digest; config/content/layer
locations are not shortened. Layer/content `position` is the actual source
array index. Successful known documents include their actual schemaVersion and
the applicable manifests/config/layers views; own mediaType/artifactType,
subject and annotations appear only when present and valid.

An invalid descriptor slot remains at its actual array position with an
`errors` array and any independently validated display fields; do not remove
the slot and renumber subsequent entries. Without a valid digest there is no
invented child/location. Text prints the same indexed error in its descriptor
block. Result diagnostics include descriptor-local as well as node-local errors.

Omit absent optional keys. Never serialize absent values as `null`; explicit
null in an OCI field that requires a string, map or descriptor is invalid input,
not absence. Preserve present empty annotation maps, strings and arrays where
their OCI field permits them, including empty indexes/layer arrays. Unsupported
descriptor/extension fields are not dumped. Structural validation may inspect
mediaType/size without adding either field to a descriptor's display.

A present platform requires string architecture and os. Known optional
os.version/variant values are strings and os.features/features are string
arrays. Preserve other platform keys in its compact source JSON rather than
mistaking their presence for classification evidence. A malformed platform
produces an INVALID_DESCRIPTOR diagnostic, not a schema-valid arbitrary object.

Known JSON keys use the schema's stable order; original annotation/platform
key order and manifests/layers array order are preserved. A JSON parser may
ignore object-key order, but the serializer must not sort/reorder source arrays.
Raw logical string values remain intact: text escapes them reversibly, JSON
uses ordinary JSON escapes. There is no terminal-control execution in either.

The schema enforces local shape. Digest/path correspondence, descriptor-child
pairing, source positions, classification, accounting and cross-view parity
are additional semantic invariants tested by #212/#213/#214/#216.

#### D4. Traversal, repeated references and subjects

Validate a supported marker and entry-point index before returning any tree.
Walk containment depth-first in source order: manifests, then each manifest's
config and ordered layers. A nonempty own artifactType selects `Content N`
rather than `Layer N`, without asserting a semantic artifact classification.
Resolve digest paths using validated lowercase sha256/sha512 syntax only.
Parse each distinct metadata document once; validate each incoming descriptor
occurrence and stat its target without reading opaque bytes.

Compare descriptor size with observed file size and supported document type
with the document's own mediaType when present. Conflicts produce errors on the
affected occurrence; they never overwrite source metadata or choose a convenient
winner. A malformed document does not supply trustworthy intrinsic metadata.
Retain validated incoming identity/location and its explicit error.

Expand reused indexes at every occurrence. Detect containment cycles using the
active ancestor chain, not a global visited set; terminate only the offending
branch with a CYCLE error. A repeated sibling is not a cycle.

Subjects remain full descriptors in the owning document. They are **reference
facts, not containment children**: do not redraw, follow or validate the subject
target's local availability just for display. A subject may refer outside the
locally contained closure. Subject-only addresses are not silently included in
stored-object totals. Cycles consisting only of subject links do not trigger
recursive expansion. Do not scan blobs to find reverse subjects.

#### D5. Proposed measurable limits and support

All limits are independent and inclusive; reject the first attempted unit above
the cap. Defaults deliberately reuse existing artifact-policy values where
their units match. New aggregate/occurrence/output caps are proposals requiring
approval, not previously measured platform limits.

| Budget | Default | Accounting / outcome |
| --- | --- | --- |
| `max_layout_marker_bytes` | 65,536 | Raw marker bytes, including whitespace; root failure above cap. |
| `max_document_bytes` | 16,777,216 | Each raw index/manifest, including entry point; no partial JSON parsing. |
| `max_total_metadata_bytes` | 67,108,864 | Marker plus distinct metadata documents read; cache hits do not spend bytes twice. |
| `max_json_depth` | 128 | JSON container nesting, including ignored fields; reject before unbounded parsing. |
| `max_objects` | 100,000 | Distinct attempted digest addresses, including missing/invalid targets; entry point excluded. |
| `max_descriptors` | 500,000 | Descriptor slots in distinct parsed documents, including config/layers/subject and malformed slots. |
| `max_depth` | 64 | Display depth: layout = 0, entry point = 1, its children = 2. |
| `max_occurrences` | 100,000 | Display nodes including layout/entry point; repeats consume budget again. |
| `max_diagnostics` | 1,000 | Reserve the final slot for an explicit diagnostic-budget LIMIT_REACHED marker. |
| `max_output_bytes` | 67,108,864 | Final encoded bytes for the selected stdout representation, including LF, indentation, escapes and generated ANSI. |

Read metadata with cap-plus-one bounded reads and no opaque payload opens.
Strictly reject duplicate keys, nonfinite JSON, invalid UTF-8, surrogate code
points and invalid field types; never use permissive coercion.
Do not silently remove long strings, truncate values, shorten digests or limit
output to terminal width. A traversal budget attaches a LIMIT_REACHED error to
the last representable owner, naming budget, cap and source at which traversal
stopped. Do not claim that its unvisited descendants are empty or count them as
inspected. Reserve diagnostic space before expanding children.

Share immutable parsed field views between occurrences rather than copying a
large index's fields once per reference. Preflight serialization/rendering with
a bounded counting sink before allocating the full string or `.to_dict()` copy
and before writing stdout; stop counting immediately above the cap. This
prevents repeated large metadata or escaping amplification from bypassing the
budget during materialization.
If the selected view cannot fit, emit no stdout and OUTPUT_LIMIT on stderr,
exit 1; do not emit a cut-off JSON document or silently substitute summary.
An external broken pipe/write failure can interrupt an already-started write;
report failure, not successful JSON delivery. The cap bounds emitted bytes, not
Python heap size or filesystem latency; memory/time evidence is a #216 gate.

Proposed native support: Linux and macOS with handle-relative, no-follow,
regular-file safe opens. Windows and filesystems lacking these primitives fail
before inspection with UNSUPPORTED_PLATFORM. Do not require write access,
0700/private-publication namespaces, ACL mutation or archive-rename primitives
for read-only inspection. Root and descendant symlinks, parent traversal
components, FIFOs/devices/sockets and unsafe digest paths are rejected. Compare
metadata file identity/size/timestamps around reads and report detected changes;
there is no atomic snapshot guarantee against a concurrently changing layout.
The caller should provide a stable local directory.

#### D6. Summary structure, classification and accounting

Keep the layout and every index, including its intrinsic metadata and source
descriptor blocks. Do not hide a nested index inside a group. At each index,
replace **contiguous runs** of structurally complete manifest occurrences with
the same category and, for artifacts, the same exact artifactType. Group order
is first source occurrence; an intervening index, error or different category
ends a run. Do not reorder later members to manufacture a larger group.
Incomplete/unknown document nodes remain expanded so their errors stay visible.

Classification precedence:

1. Contradictory classification evidence: unclassified, with explicit conflict
   diagnostics where declared descriptor/document fields disagree.
2. Exact own artifactType `application/vnd.cncf.notary.signature`: signatures;
   this says nothing about authenticity.
3. Exact own artifactType `application/vnd.microsoft.artifact.lifecycle`:
   lifecycle artifacts; no lifecycle assertion is verified.
4. Exact own artifactType `application/vnd.in-toto+json`, or source descriptor
   annotation `vnd.docker.reference.type=attestation-manifest`: attestations.
   Other conflicting own artifact types are not overridden by that annotation.
5. Any other nonempty own artifactType: artifacts grouped by its literal value,
   not relabeled SBOM/signature from a guess.
6. With no artifact evidence, a supported image manifest/config media-type pair
   plus a source platform with nonempty, non-`unknown` os/architecture:
   platform images. Missing/unknown platform evidence remains unclassified.

Keep full source values in group evidence where needed to explain a category;
unknown/unknown platform alone is never attestation evidence. Classification
does not decode content or require full verification.

Root summary statistics are displayed in text and emitted only for summary:

| Key | Meaning |
| --- | --- |
| `uniqueObjects` | Distinct existing digest-addressed regular files reached by containment; not entry point/marker, missing targets or unlisted files. |
| `objectReferences` | Config/layers/manifests slots from distinct successfully parsed metadata documents, including missing or repeated targets. |
| `subjectReferences` | Subject descriptor slots from those documents, separately counted; not containment references. |
| `storedBytes` | Sum of stat sizes once per existing contained digest address, not descriptor-declared size, output size or downloaded bytes. |

When inspection is incomplete, these are **observed** statistics, accompanied
by `statisticsComplete: false` and a visible `Statistics complete: false` label.
Even complete structural statistics do not prove complete registry discovery
or cryptographic integrity. Re-expanding a cached document does not create new
source descriptor slots; displayed occurrence counts are separate.

A summary group contains category, source manifest positions, displayed
reference count, distinct manifest count, config/layer occurrence counts,
layer-count distribution, unique contained objects and bytes for that group.
Group unique totals are not additive across groups sharing content. Count a
referenced manifest once per occurrence for shape distributions; only say
"Each" when every member has exactly that shape. Otherwise show the full
histogram, e.g. `{"1": 1, "2": 1}`, never a rounded average.

For the tiny fixture, summary stdout JSON is:

<!-- inspect-example: summary -->
```json
{
  "kind": "layout",
  "location": "/example/layout",
  "imageLayoutVersion": "1.0.0",
  "statistics": {
    "uniqueObjects": 2,
    "objectReferences": 3,
    "subjectReferences": 0,
    "storedBytes": 422
  },
  "statisticsComplete": true,
  "children": [{
    "kind": "index",
    "location": "index.json",
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [{
      "digest": "sha256:f9b333e16b17d3bb223460cb53d9925856fb02b9e142abbcd6b0e232fa39336f"
    }],
    "children": [{
      "kind": "group",
      "category": "artifacts",
      "artifactType": "application/vnd.example.payload",
      "positions": [0],
      "references": 1,
      "uniqueManifests": 1,
      "configReferences": 1,
      "layerReferences": 1,
      "layerCountDistribution": {"1": 1},
      "uniqueObjects": 2,
      "storedBytes": 422
    }]
  }]
}
```

Text emits all these statistics/group fields with labels, not hidden JSON-only
facts. Root labels are `Unique objects`, `Object references`, `Subject references`,
`Stored bytes`, `Statistics complete`; group heading is `Manifest group` and
labels are `Category`, optional `Artifact type (artifactType)`, `Positions`,
`References`, `Unique manifests`, `Config references`, `Layer references`,
`Layer count distribution`, `Unique objects`, `Stored bytes`. Numeric values
remain exact integers, not rounded human-size strings. Descriptor/index display
and label/value styling remain as locked. No hidden full expanded tree is
included in summary JSON.

**Mixed-shape and explicit-subject fixture.** Derive a second manifest from the
tiny fixture by duplicating its sole layer descriptor, then appending a subject
descriptor with mediaType `application/vnd.oci.image.manifest.v1+json`, the first
manifest's digest and size 420, in that property order. Serialize identically.
The second manifest is 716 bytes, with digest
`sha256:a12c8fd7afecaf38491b8dea268b9478ba888c76a4826d908540baf53a30619c`.
Put both manifests in entry-point order, first then second. Root statistics are
3 unique objects, 7 object references, 1 subject reference and 1,138 stored bytes.
The subject is a source fact, not an eighth containment reference or a second
copy of the first manifest. The single contiguous artifact group's shape is:

<!-- inspect-example: mixed-group -->
```json
{
  "kind": "group",
  "category": "artifacts",
  "artifactType": "application/vnd.example.payload",
  "positions": [0, 1],
  "references": 2,
  "uniqueManifests": 2,
  "configReferences": 2,
  "layerReferences": 3,
  "layerCountDistribution": {"1": 1, "2": 1},
  "uniqueObjects": 3,
  "storedBytes": 1138
}
```

Text must show the distribution, not "Each has one layer." Deriving these
fixtures is design validation, not a claim that the inspector is implemented.

#### D7. Local tag-schema referrers lists

Use the OCI Distribution 1.1.1
[referrers tag schema](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#referrers-tag-schema):
`<algorithm>-<first-64-characters-of-subject-encoded-digest>`.
Only consider actual `org.opencontainers.image.ref.name` annotations on
entry-point descriptors targeting supported OCI image-index documents. Do not
guess a tag from a blob filename, an arbitrary nested annotation, an unlisted
file or a repository URL. Recognize each tagged occurrence separately.

For sha256, the tag's 64 lowercase hex characters identify the full declared
subject digest. For sha512, the tag has only a 64-character prefix: resolve a
full digest only from a **unique** matching full digest in reachable descriptor
or explicit-subject metadata. Zero or multiple candidates remain unresolved;
do not manufacture the missing suffix. Other algorithm/tag shapes remain
ordinary index metadata with no recognized association.

Recognition means "this index is locally declared to list referrers for this
tag-derived subject," not "each member's subject was verified." Members lacking
subject remain listed/unconfirmed; retain their actual absence. An explicit
member subject remains authoritative and pointing in its actual direction.
A mismatch with an unambiguously resolved list subject is a conflict diagnostic,
not permission to rewrite either value or move children beneath the subject.
An unresolved prefix alone is not structural corruption; summary must label its
association unresolved instead of claiming a full subject.

The SDK retains typed `ReferrerListEvidence` on the index occurrence.
Expanded text/JSON remain the literal source tree with the tag still in its
descriptor's annotations; recognition does not add a new default-output label.
Summary adds an `association` block on the relevant index, displayed as
`Tag-schema association` with Tag, Source, Status, and either Digest or
Algorithm/Prefix. This is explicitly derived, source-labeled evidence, never a
new OCI `subject` property or a separate edges collection.

Deterministic recognition cases for #212/#213/#216:

| Local input | Expected evidence |
| --- | --- |
| Tagged OCI index, `sha256-` plus 64 `a` hex characters, no member subject | Recognized declaration for `sha256:` plus those same 64 characters; member unconfirmed. |
| Same index with matching explicit member subject | Listed member and explicit subject agree; neither relationship is inverted. |
| Same index with explicit member subject using 64 `b` characters | Conflict error; preserve both source facts, incomplete result. |
| sha512 prefix with exactly one reachable matching 128-hex digest | Resolved full digest from that evidence, not from padding/truncation. |
| sha512 prefix with zero/two matching full digests | Unresolved association; no invented full digest or corruption claim. |
| Same blob referenced twice with different descriptor annotations | Separate occurrence evidence, no globally attached tag. |

#### D8. Terminal, encoding and output parity

Use the approved Unicode connectors when the text stream can encode them.
Otherwise switch automatically to four-character ASCII steps: `|-- `, `` `-- ``
and `|   `; preserve spaces, property indentation and source ordering.
`--ascii` forces that mode and escapes non-ASCII values. In other encodings,
escape only unencodable value characters rather than failing halfway through
output. Never silently replace characters with `?`.

Before adding renderer-generated ANSI, reversibly escape backslashes, newlines,
tabs, carriage returns and all Unicode Cc/Cf/Cs controls in every untrusted
key/value/path/message: use `\\`, `\n`, `\t`, `\r`, and `\uXXXX` / `\UXXXXXXXX`.
This includes ESC, DEL/C1, bidi controls and zero-width formatting controls.
Escaping preserves full information and is not truncation. JSON uses standard
ASCII-safe JSON escapes instead of this text presentation transform.

Strip only renderer-generated SGR in parity tests; stripped colored text must
equal plain text byte-for-byte. Preserve the locked cyan labels, default
foreground values, heading colors, gray connectors, red error labels and
span/line resets. JSON, nonterminals and nonempty NO_COLOR suppress ANSI.

### PM-1. Requirements traceability

The original #130 acceptance criteria map in their source order as follows:

| #130 criterion | Requirement / design | Delivery and validation |
| --- | --- | --- |
| 1: folder validation and entry point | R1, D1/D4/D5 | #212/#214; valid/invalid root fixtures. |
| 2: every index | R2/R5, D3/D6 | #212-#215; nested/multi-root text/JSON parity. |
| 3: metadata provenance | R3/R4, D2/D3 | #212/#214; repeated-target annotations/platforms. |
| 4: expanded and summary CLI | R2/R5, D6 | #214/#215; selected-view shapes. |
| 5: generated versus original index | R1/R2, D3 | #212/#214; index.json versus digest paths. |
| 6: supported graph shapes | R1/R4, D4 | #212/#216; single/nested/multiple roots, layers/content. |
| 7: evidence-based groups | R5, D6/D7 | #213; known/unknown/conflicting types. |
| 8: truthful heterogeneous counts | R5, D6 | #213/#215; uniform and mixed distributions. |
| 9: shared objects and subjects | R4, D4/D6 | #212/#213; reuse, explicit subjects, independent counts. |
| 10: local and read-only | R9, D1/D4 | #212/#216; no-write/no-network/no-credentials spies. |
| 11: reuse and bounded metadata | R7/R9, D1/D5 | #212/#216; byte/read accounting, no hashing. |
| 12: partial/error/limit behavior | R6/R7, D5/PM-5 | #212/#214/#216; exact boundaries/streams/exits. |
| 13: ordering and safe encoding | R2/R7, D3/D8 | #214/#216; controls, ASCII, color resets. |
| 14: deterministic regression cases | R1-R9, PM-7 | Every slice's focused tests plus #216 integration. |
| 15: interpretation/migration docs | R10, F/PM-6 | #208/#217; required final docs and show compatibility. |

Scenario 1 maps to D3/D4 and #212/#214; scenario 2 to D2/D3/D6 and
#212-#215; scenario 3 to D7/PM-5 and #212/#213/#216. Tag recognition extends
R8 through explicit owner direction; no other feature scope is introduced.

### PM-2. APIs, versioning and compatibility

D2 specifies the additive SDK API, D3 the version-1 JSON schema, and section 7
the CLI flags. There is no HTTP, queue, event or mutating API. Read-only calls
can be repeated; there are no retry/idempotency tokens. New result types do not
change existing artifact/layout result types. JSON semantic equality to text
does not mean its layout mirrors Python private cache structures.

### PM-3. Metrics, usage and cost

This PR changes specifications only. The runtime design adds no usage backend,
per-customer identifier, adoption/churn tracking or data upload: that would
contradict local/no-network scope. Business/adoption measurement remains an
explicit product-evidence gap, not invented telemetry. Correctness gates map
to fixture outputs and IO instrumentation; cost maps to metadata bytes/stat
calls, display occurrences, output bytes, elapsed time and peak memory in #216.
No resource depends on downloading or hashing layer bytes.

### PM-4. Authentication, authorization and networking

No identity, permission or network-path change. Existing user read permissions
are the authorization boundary. No credentials, ports, service endpoint, RBAC
role, egress exception or new key lifecycle is required. Unsafe local paths
fail closed rather than broadening permissions or falling back to network.

### PM-5. Failure taxonomy and exact outcomes

| Condition | SDK / tree outcome | CLI outcome |
| --- | --- | --- |
| Invalid arguments or incompatible flags | ValueError / Click usage failure; no inspection | Exit 2 for CLI usage errors, no tree. |
| Unsupported safe-open platform | LayoutInspectionError, UNSUPPORTED_PLATFORM | No stdout; diagnostic stderr, exit 1. |
| Missing/unreadable/invalid marker or entry-point index | LayoutInspectionError, INVALID_LAYOUT with specific cause | No stdout; diagnostic stderr, exit 1. |
| Missing descendant | Identity/location plus MISSING_OBJECT, no fabricated intrinsic fields | Partial selected view, stderr, exit 1. |
| Unreadable/unsafe/nonregular descendant | UNREADABLE_OBJECT, distinct from absence | Partial selected view, stderr, exit 1. |
| Bad JSON/schema/field type | MALFORMED_DOCUMENT or INVALID_DESCRIPTOR on its owner; trustworthy siblings retained | Partial selected view, stderr, exit 1. |
| Unsupported required document media type | Unknown heading/location and UNSUPPORTED_TYPE; no guessed parse | Partial selected view, stderr, exit 1. |
| Descriptor size/type or resolved referrers-list subject conflict | CONFLICTING_DESCRIPTOR, retain source-owned facts | Partial selected view, stderr, exit 1. |
| Containment ancestor cycle | CYCLE, terminate that branch only | Partial selected view, stderr, exit 1. |
| Metadata/object/descriptor/depth/occurrence/diagnostic budget exceeded | LIMIT_REACHED at representable owner; no claim that unvisited children are empty | Partial selected view, stderr, exit 1; root budget failures have no tree. |
| Detected filesystem mutation | CHANGED_DURING_READ; do not reuse unstable parsed facts | Partial or invalid-root outcome as appropriate, exit 1. |
| Output cannot fit its byte cap | LayoutInspectionError, OUTPUT_LIMIT before write | No stdout; stderr, exit 1. |
| Broken pipe/write failure | Result remains a valid SDK object; rendering/delivery failed | Exit 1; OUTPUT_WRITE_FAILED on stderr if writable; no completeness promise for interrupted output. |
| Empty valid entry index / zero-layer manifest | Structurally complete, preserve actual empty array | Exit 0, not an error or null. |

Diagnostics are attached to the nearest source owner when a descriptor has no
valid target digest; never construct a path from invalid digest text. Text
prints `Error (CODE): message` and Source beneath the owner; MISSING_OBJECT uses
`Missing file (MISSING_OBJECT): Referenced file is missing`. The errors array
contains the same code/message/source. SDK diagnostic order follows discovery;
stderr repeats those diagnostics, not raw tracebacks, with full safe values.
No severity-warning channel disguises incomplete structural results as exit 0.
Unrecognized semantic artifact types and unresolved tag prefixes alone are not
structural errors. No retries, background repair or network fallback occurs.

### E. Risk identification and assessment

Breaking-change risk: migration controlled by F. Security/privacy risks:
untrusted local inputs/output, mitigated by D5/D8/G/H. Dependency risk: reuse
must not inherit verifier side effects; assess in J. Infrastructure/auth risks:
no new infrastructure/identity; safe-open support is an explicit gate.
Deployment risk: ship inspection before show warning. Multi-tenant/noisy-neighbor
risk: no service; per-call caps do not promise process-wide isolation. Regression
risk: layout, artifact parsing/storage and CLI dispatch, covered in M/PM-7.
Every identified risk has a named owning delivery slice in section 13/PM-6.

### F. Breaking changes and migration

0.2 adds inspect; legacy show keeps byte-compatible entry-point JSON stdout and
its existing exit behavior. #208 adds only its stderr warning once inspect is
available. `inspect --json` is not a drop-in replacement for show JSON.
0.3 removal remains #209, gated on the warning actually shipping in 0.2.
SDK read_index is not deprecated. Shared layout/artifact/CLI regression tests
protect existing callers; no deprecation warning is introduced by this design PR.

### G. Security risk management

Threat boundaries are untrusted filesystem names/objects, untrusted JSON and
untrusted terminal metadata. Use no-follow handle-relative opens, strict bounded
parsing, path validation, control escaping and renderer-only ANSI. No new
cryptography/trust verification or customer-managed keys apply. Do not print
credentials or include payloads/metadata in automatic debug logs. Caller-owned
stdout can contain the metadata explicitly requested; document that distinction.

### H. Privacy and data sovereignty

All data stays in process and caller-selected local output unless the caller
redirects it elsewhere. No new persistent cache, log sink, backup, user identifier
or cross-region flow. Existing opt-in logging must not acquire raw annotations,
tokens or payloads as a side effect of the inspector.

### I. Observability

Typed diagnostics, visible errors, stream/exit behavior and measured budget
accounting support the troubleshooting in section 11. Per-slice tests act as
deterministic probes; #216 records resource measurements, not backend customer
telemetry. There are no new hosted monitors or percentiles presented as an SLA.

### J. Dependencies

Reuse internal strict JSON/digest/read-only storage helpers only where compatible;
no new runtime dependency is planned. Unavailable filesystem primitives produce
UNSUPPORTED_PLATFORM, not an unsafe fallback. Existing package Python >=3.10
support remains; Linux CI exercises 3.10/3.11/3.12 and local macOS checks cover
native behavior. Inspection must not require native publication APIs merely
because artifact upload/download uses them. No dependency cycles or network SLA.

### K. Deployment and buildout

Deliver through ordinary reviewed package PRs; no service deployment, region
buildout, configuration backfill, registry mutation or data migration. Existing
CI runs on PRs; the benchmark workflow remains explicitly dispatched, not a new
mandatory live-registry dependency. Publishing the package/release is a separate
owner action. Keep Planned inventory until an authorized Build starts.

### L. Multi-tenancy

No hosted multi-tenant component. Per-call budgets constrain hostile local
metadata; they are not quotas across unrelated processes or users.

### M. Regression

Layout: read_index/show/status/validate retain their contracts. Artifacts:
strict parser/storage consumers retain verification and publication safeguards.
CLI: dispatch/help, argument validation, color handling and legacy show stdout
stay compatible. Shared helpers change only with focused existing-caller tests.
There is no mixed-region rollout; partially installed package versions must not
advertise a show migration target before inspect is available.

### N. Rollback

Revert additive inspection/summary changes before release without touching
layouts; there is no persisted inspection format or on-disk migration. If the
inspect command is withdrawn, withdraw its not-yet-released show warning too.
Keep published legacy show behavior through 0.2. A published JSON schema version
must not be silently redefined; breaking revisions need a new documented version.

### PM-6. Phasing and provisional estimates

Engineering judgment only, not owner-approved commitments. Units are engineer
days; test/review and package-integration/rollback effort are separate columns.

| Item / component | Implement | Test/review | Integrate/rollback | Prerequisite |
| --- | --- | --- | --- | --- |
| #212: layout types, safe inspection, serializer and tag recognition | 3-5 | 2-3 | 0.5 | Approved/merged #211 and authorized Build. |
| #213: SDK summaries/classification/accounting | 2-3 | 1-2 | 0.5 | #212. |
| #214: expanded CLI, escape/color/encoding and stream adapter | 2-3 | 1-2 | 0.5 | #212. |
| #215: summary CLI projection | 0.5-1 | 0.5-1 | 0.25 | #213, #214. |
| #208: legacy show migration warning | 0.5 | 0.5 | 0.25 | #214 available. |
| #216: integrated/offline/native/resource evidence | 1-2 | 1-2 | 0.5 | #215, #208. |
| #217: final SDK/CLI/how-to documentation | 1-2 | 0.5-1 | 0.25 | All required 0.2 runtime/test items. |

#209 is separately deferred to 0.3; its estimate belongs to that future work.
Long poles: safe-open reuse without publication requirements, repeated-index
budget semantics, and cross-view summary/error parity. Re-estimate after #212.
No additional implementation issues or alternative final docs item are created.
The owner must approve this interpretation of the existing acceptance criteria;
in particular #212 includes hierarchical JSON and tag recognition, #213 carries
association evidence/counting, and #214/#215 render the matching views.

### PM-7. Test plan and CI

| Level / owner | Required cases and assertions |
| --- | --- |
| SDK unit, #212 | Tiny/real metadata, simple/nested/multi-root indexes, repeats/ancestor cycles, per-occurrence annotations/platforms, source presence, strict parse failures, all D5 thresholds at cap and one over. |
| Recognition unit, #212 | All D7 rows, unchanged subject direction, no reverse scan/network, descriptor-local versus global tagging. |
| Summary unit, #213 | Two manifests with layer counts 1 and 2, repeated targets across/within groups, first-source run ordering, every index retained, unknown/mixed/conflicting evidence, unresolved tags and incomplete counts. |
| CLI unit, #214/#215 | Exact locked baseline, exact selected-view JSON, errors/streams/exits, all text/JSON fields in parity, ANSI spans/resets, NO_COLOR/nonterminal, C0/C1/bidi/backslash injection, Unicode and forced/automatic ASCII. |
| Compatibility, #208 | Legacy show stdout/exit unchanged, warning stderr only, read_index/status/validate unaffected. |
| Offline integration, #216 | Stored-layout preservation; IO spies prove no writes, payload reads, hashing, credentials, daemon/network access; schema validation plus semantic counts; input mutation and unsupported-platform guards. |
| Resource evidence, #216 | Bounded repeated-index expansion, aggregate metadata caps, escaped-output amplification, measured real-fixture metadata bytes/time/memory, Linux and local macOS. |
| Docs, #217 | SDK/CLI/how-to links, complete examples, subject/storage/count interpretation, migration warning/removal timing. |

Tests belong in existing `src/regshape/tests` pytest suites and run via the
normal Tests PR workflow; add selectors to relevant existing compatibility/
platform jobs if needed. No new unit-test framework, required live MCR fetch,
benchmark service or CI runner. Validate native macOS behavior on the local Mac;
existing required hosted checks remain binding and are reported separately.
Deterministic local minimal payloads may model the real metadata graph without
pretending their digests match the real downloaded layers.

For this design-only PR, validate the schema and every inline example, construct
the tiny fixture and recompute its hash/counts, verify mixed/subject semantics,
check Markdown links/whitespace and the untouched locked baseline. No runtime
implementation or new unit-test tooling is part of #211.

### O. Next actions and launch gate

1. Review this proposal and its exact hierarchical/summary/partial examples.
2. Approve the remaining contracts and existing dependency-aware breakdown;
   resolve any product-evidence or resource-target gaps deliberately.
3. Authorize the design merge separately after review/checks.
4. Authorize Build separately, then implement one child per PR and deliver
   the required final #217 documentation.
5. Obtain explicit release authorization; keep #130 open while its deferred
   0.3 child remains open. No external partner/legal launch activity is assumed.

### P. References

- [Locked human-output contract and current work breakdown](../cli/layout-inspect.md).
- [Existing layout operations](oci-layout-create.md).
- [Existing artifact operations](../operations/artifacts.md).
- [Version-1 recursive output schema](layout-inspect.schema.json).
- [OCI Image Layout](https://github.com/opencontainers/image-spec/blob/v1.1.1/image-layout.md).
- [OCI referrers tag schema](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#referrers-tag-schema).

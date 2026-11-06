# Complete artifact download and round-trip: requirements and engineering design

- **Status:** Implemented; revision 4 publication refinement approved, runtime delivery merged through [#126](https://github.com/toddysm/regshape/pull/126), and final documentation [#119](https://github.com/toddysm/regshape/issues/119) merged through [#127](https://github.com/toddysm/regshape/pull/127). All seven required children are closed.
- **Feature:** [#91](https://github.com/toddysm/regshape/issues/91).
- **Design item:** [#113](https://github.com/toddysm/regshape/issues/113).
- **Related, separate capability:** [#112](https://github.com/toddysm/regshape/issues/112), config/layer-position downloads.
- **Target:** [0.2](https://github.com/toddysm/regshape/milestone/1); not a release commitment.
- **Date / inspected baseline:** 2026-11-05 UTC / `d0e2540a24e77b92949e8b3371c1485185e75a6a`.
- **Owner evidence:** [recorded requirements clarification](https://github.com/toddysm/regshape/issues/91#issuecomment-6646891148).
- **Historical approval:** owner approved revision 3 at design head
  `c4e2c922b6f83aa8beb06c6b944a740bb701048d`, recorded design SHA-256
  `87bf351ccb0e0ee8d22887b3f0ad0502c9ca1edaedb2b5c0ee95c1d8ffa70bd4`,
  and merging [design PR #120](https://github.com/toddysm/regshape/pull/120).
  Subsequent approval metadata was administrative, not a new technical revision.
- **Current authorization:** the owner explicitly approved the
  [revision-4 publication refinement](https://github.com/toddysm/regshape/issues/91#issuecomment-6666694366)
  after a reproduced initial-directory acquisition race. This authorizes the
  trusted-private namespace and atomic no-replace protocol below, with explicit
  trust/platform/filesystem boundaries. The exact final revision-4 file hash has
  **not** separately been reviewed/approved by the owner; retain the revision-3
  hash as historical evidence, not as the hash of this revised text.
  Delivery completed through hosted checks, agent-only reviews and protected
  merges. The owner authorized final inventory close-out and feature closure
  after its verified merge; no release, #112 work or branch cleanup is included.

Open Container Initiative (OCI), command-line interface (CLI), application
programming interface (API), product requirements document (PRD), engineering
design document (EDD), and continuous integration (CI) are abbreviated below.
The text below retains the original requirements/design baseline and its
prospective language. Runtime items [#114](https://github.com/toddysm/regshape/issues/114)
through [#118](https://github.com/toddysm/regshape/issues/118), and final
documentation [#119](https://github.com/toddysm/regshape/issues/119), are now merged.
Use the [delivered command reference](artifact.md),
[library reference](../operations/artifacts.md), and
[user workflow](../../docs/guides/artifact-pull.md) for current behavior and
captured output. Section 7A remains explicitly illustrative design output,
not a captured implementation transcript. This delivery-status update does not
change technical contracts or replace the historical approval/hash evidence.

> **Revision 2 approval note:** Review exposed a gap between excluding subjects
> and promising a self-contained round-trip. The approved design follows explicit
> `subject` descriptors, without discovering referrers. A subject-bearing artifact
> can therefore download more content than the initial proposal implied. This
> material refinement was explicitly approved with revision 3.

Revision 3 closes review gaps in output-path identity, compressed-input/gzip
budgets, layout-marker buffering and publication-tag validation. These are
mandatory safety constraints; implementation status is recorded above rather
than inferred from this original design wording.

> **Revision 4 approval note:** `mkdir` followed by `stat/open` in an exposed
> namespace cannot prove creation ownership: replacement before the first
> identity read can be adopted and deleted. The owner approved establishing
> ownership privately **before** exposing staging/output names, with native
> atomic no-replace installation. User-facing command/output scope is unchanged.
> Ordinary permissions do not isolate hostile processes with the same user
> identity; D4 now states that trust precondition explicitly rather than
> misrepresenting permission checks as such isolation.

## Part I: functional requirements

### 1. Overview

Give registry operators and Python callers a complete, verifiable local copy of
an image or supported non-image artifact, and a way to publish that copy again.
A source can name a tag or digest and resolve to a simple manifest or an index.
The default output is an OCI layout directory containing every platform and
required descendant; an explicit option produces the same layout as a compressed
archive. Every index, manifest and blob, including configuration blobs, has
individual progress. No daemon is required and payloads are not executed or unpacked.

### 2. Problem and evidence

The owner requested "manifest index + manifests + blobs", later explicitly
requiring config progress, compressed output and complete nested-index push.
Today this requires manually combining manifest inspection and blob requests;
existing layout push rejects index roots. The manifest-progress prerequisite
[#90](https://github.com/toddysm/regshape/issues/90) is now complete, so its
observations and renderer can be reused rather than building competing displays.

> NOTE: Evidence is one named requesting operator (Todd), the linked issue and
> inspected source. Three customer organizations, adoption telemetry, quantified
> support volume and market-wide demand have not been established. They are not
> invented to satisfy a cloud-service product rubric.

### 3. Scenarios

These are prospective operator workflows derived from the owner's requests,
not claims of measured customer incidents.

- **S1 - Tagged simple artifact.** Before working without registry access, Todd,
  the requesting registry operator, needs a copy of a one-layer artifact.
  Instead of extracting descriptor digests manually, he supplies its
  tag and a new destination. He sees the manifest, config and content receive
  progress, then a completed layout and resolved digest. A tag changing midway
  does not alter the selected graph. The same result and errors let him automate
  the operation through Python without a terminal.
- **S2 - Digest-pinned multi-platform index.** When preparing a portable copy of
  a known multi-platform release, Todd cannot rely on today's shallow validator
  or simple-root-only push. He supplies the release digest. The root is an index
  containing another index and multiple manifests.
  All platforms are retained; shared configs/layers download once. He gets the
  original root digest and recursively verifies the local result, not a
  host-selected manifest or a rewritten index.
- **S3 - Portable archive and restore.** When moving the copy to another registry,
  Todd needs one portable file rather than manually gathering digest files and
  reconstructing child publication order. He requests a gzip-tar file, transfers
  it using his own approved process, and supplies it to `layout push` with a
  destination tag. The tool validates it locally before registry writes, then
  publishes children before parents. The destination resolves to the same root.
- **S4 - Incomplete or unsafe input.** During a pull or restore, a missing layer,
  corrupt config, unsafe archive or exhausted disk prevents Todd's copy from
  completing. Instead of trusting the current shallow validation or manually
  guessing which files are usable, he receives an error identifying the stage
  without exposing credentials. Neither an incomplete local artifact nor a
  successful remote-root publication is claimed. He fixes the cause and retries
  explicitly; this is not a partial-resume feature.

### 4. Outcomes, priorities and scope

Outcome: a successful invocation means the chosen content graph is complete,
digest-preserving and usable for a verified round-trip, not merely that one
manifest was fetched.

| Requirement | Priority | Acceptance boundary |
| --- | --- | --- |
| R1 | Must | All four tag/digest x manifest/index cases work; a tag is fetched once per operation, excluding existing request retries/authentication. |
| R2 | Must | Preserve original content bytes, media types, descriptor metadata and root identity; verify digests and descriptor sizes. |
| R3 | Must | Traverse all index children recursively, config/layer/content and explicit subject descriptors; deduplicate stored/downloaded content by digest without deleting descriptor edges. |
| R4 | Must | Default OCI directory and explicit gzip-tar contain the same complete graph; output is not an extracted root filesystem. |
| R5 | Must | Existing `layout push` accepts both outputs and publishes complete nested indexes with original digests and root-tag semantics. |
| R6 | Must | One row per unique downloaded index/manifest/config/layer blob, including the initially unresolved root; actual bytes and honest totals. |
| R7 | Must | No success-shaped partial output, implicit overwrite/resume, unsafe archive extraction or masked primary failure. |
| R8 | Must | Bounded streaming, graph/metadata limits, credential-safe transport and explicit unsupported-input behavior. |
| R9 | Must | Optional library callbacks, documented result/error/ownership contracts, no daemon, unchanged existing valid flat-layout workflows. |
| R10 | Must | Recursive offline validation, dry-run, JSON output, progress suppression and archive push use the same verified graph rules. |
| R11 | Must | Deterministic unit/integration/frame/security regressions and final documentation cover every delivered surface. |

Scope includes the byte-preserving receive primitive, graph/layout/archive
operations, recursive push and its CLI/dry-run/report paths. Config/layer selection
by position is separately tracked in #112 and is not required to complete #91.
Canonical-reference redesign remains [#88](https://github.com/toddysm/regshape/issues/88).
Credential-setup guidance remains [#86](https://github.com/toddysm/regshape/issues/86).

Non-goals are platform filtering, graph rewriting, registry-to-registry copying,
referrer discovery, arbitrary external-URL fetching, payload
extraction/execution, arbitrary archive formats, parallel transfers and partial
resume. These were not committed requirements cut from this delivery; no
additional speculative backlog features are created by this specification.

### 5. Existing solution and reuse

| Existing surface | Reuse / necessary extension |
| --- | --- |
| [Reference helpers](../../src/regshape/libs/refs.py) | Keep embedded-registry source grammar and default `latest`; validate operation inputs without implementing #88. |
| [Manifest operations](../../src/regshape/libs/manifests/operations.py) | Existing getter returns decoded text; add an independent raw-byte-to-file operation instead of re-encoding that result. |
| [Blob operations](../../src/regshape/libs/blobs/operations.py) | Stream and verify config/content alike, using descriptor sizes and existing redirect/auth behavior. |
| [Layout operations](../../src/regshape/libs/layout/operations.py) | Current validator is shallow and SHA-256-only; push accepts simple roots only. Extend strict graph verification/push, preserving valid flat behavior. |
| [Progress contracts](../../src/regshape/libs/progress.py) and [renderer](../../src/regshape/cli/progress.py) | Adapt existing observations; retain a single rendering engine and existing blob/manifest command behavior. |

Do not use model `to_json()` or `digest()` to store downloaded manifests: those
methods normalize serialization and would change content identities.

### 6. Alternatives

External tools are not benchmarked or certified by this proposal. Within
RegShape, flattening indexes or creating friendly config/layer copies was rejected:
both obscure the original graph and can duplicate content. Digest-addressed OCI
storage plus annotated documentation is the selected portable contract.

### 7. Desired experience

#### Inputs and four root cases

| Source | Root | Required traversal |
| --- | --- | --- |
| `registry/repo:tag` | Manifest | One root GET; config and every layer/content blob by digest. |
| `registry/repo@digest` | Manifest | Verify the requested root digest; same blob traversal. |
| `registry/repo:tag` | Index | One root GET; every child index/manifest recursively, then all required blobs. |
| `registry/repo@digest` | Index | Verify the requested root digest; same complete all-platform traversal. |

A digest can identify an index. No host-platform inference is performed. Tag
resolution uses the fetched root body, not a HEAD followed by a second root GET.
The object returned by that first successful GET is the chosen version.
In all four cases, explicit subject descriptors also add their referenced
manifest/index closure, as specified in D3; referrers discovery is not performed.

#### Command shapes

Replace example registries/repositories with authorized targets and authenticate
separately. These proposed commands use new output names in an existing parent directory:

```bash
regshape artifact pull -a registry.example.com/team/example:v1 \
  --output ./downloaded-artifact

regshape artifact pull -a registry.example.com/team/example:v1 \
  --format oci-tar-gzip --output ./downloaded-artifact.tar.gz

regshape layout validate --path ./downloaded-artifact --recursive

regshape layout push --path ./downloaded-artifact \
  --dest registry.example.com/team/copy:v1

regshape layout push --path ./downloaded-artifact.tar.gz \
  --dest registry.example.com/team/copy:v1 --dry-run --json
```

`--output/-o` is required. `--format` is `oci` (default directory) or
`oci-tar-gzip`; filename extensions do not silently select output format.
`--no-progress`, `--json` and existing command telemetry options are supported.
`--artifact/-a` uses the shared selector; if the shared helper also exposes its
legacy aliases, retain their established deprecated-in-0.2 help wording rather
than invent a different policy. `--raw`, `--part` and `--platform` do not apply.

`layout push --path/-p` retains directory support and additionally accepts a
regular gzip-tar file. File content must match the supported archive profile;
suffix alone is not validation. Destination remains `--dest/-d registry/repo[:tag]`.
An explicit tag applies to the single top-level root descriptor, even when that
root is an index with many descendants. A multi-root layout still rejects a
single tag override. A digest-qualified **destination** is rejected before
network requests; source digest support does not make `@digest` a tag.

#### Directory and archive structure

Angle-bracket names below illustrate actual full hexadecimal digest filenames,
not friendly aliases. This is the normative file organization:

```text
downloaded-artifact/
  oci-layout
  index.json
  blobs/
    sha256/
      <root-index-hex>       original index, when applicable
      <nested-index-hex>     nested index, when applicable
      <manifest-hex>         original manifest bytes
      <subject-hex>          explicitly referenced subject, when present
      <config-hex>           config contents, including non-image config
      <layer-0-hex>          original layer/content bytes
    sha512/
      <hex>                 only when referenced with SHA-512
```

`oci-layout` declares image layout version `1.0.0`. Generated `index.json` is
an OCI index with **one** descriptor for the original root. A simple manifest
still uses this local wrapper. An original index remains a separate blob; it
is neither flattened into nor replaced by the wrapper. Root descriptor `digest`,
`size`, `mediaType` and, when present, `artifactType` describe the verified root.
For a tag source, only the local descriptor adds
`org.opencontainers.image.ref.name` containing that tag. A digest-only source
gets no invented tag. Source provenance/resolved reference is returned in the
result, not injected into content-addressed documents or a new mandatory sidecar.

The archive contains `oci-layout`, `index.json` and `blobs/...` directly at its
root, with no enclosing user-selected directory. It excludes staging files,
logs, credential data and incomplete markers. It compresses the layout container;
layers already compressed remain untouched. Descriptors provide platform, config
and layer-order navigation; friendly duplicate files and symlinks are not generated.

#### Surfaces and verification

The Python API returns typed counts/root metadata only on complete success.
Shell automation checks exit status and can run recursive offline validation.
Graphical UI, agent-host service, Bicep, Terraform and Helm are not applicable:
this is a local library/CLI, not infrastructure provisioning or a deployed service.
Normal CLI status/errors identify the operation stage and safe digest/path context.
Library callers retain exception identity for observer and transport failures.

### 7A. Proposed output samples

These are synthetic contract samples, not output captured from an implementation.
Abbreviated digests are display labels, never runnable source inputs.

Each screen contains one row per object. For the active config blob, this
snapshot replaces its earlier row rather than appending another:

```text
Index 1 sha256:11111111... Downloaded 2.0 KiB/2.0 KiB
Index 2 sha256:22222222... Downloaded 1.0 KiB/1.0 KiB
Manifest 1 sha256:33333333... Downloaded 4.0 KiB/4.0 KiB
Blob 1 (config) sha256:44444444... Downloading 1.0 KiB/2.0 KiB [######------] 50% 512 B/s ~2s remaining
```

Later, that same config row reads:

```text
Blob 1 (config) sha256:44444444... Downloaded 2.0 KiB/2.0 KiB
```

The initial root can show `Manifest 1 Preparing 0 B (total unknown)`; advertised
index kind and verified digest update that same identity. Unknown lengths have
byte/rate-only progress. Small configs retain their final row without a delay.
Embedded content uses `Stored (embedded)`, not fictitious network throughput.

Example successful directory result on stdout, after stderr rows settle:

```text
Downloaded complete artifact.
Source: registry.example.com/team/example:v1
Resolved: registry.example.com/team/example@sha256:11111111...
Output: ./downloaded-artifact
Format: oci
Objects: 2 indexes, 2 manifests, 5 blobs
Shared references reused: 3
```

For archive output, `Output` names the archive and `Format` is `oci-tar-gzip`.
During local packaging stderr can show `Packaging OCI archive`; that is a
separate local phase, not another network transfer or an overall byte percentage.
Object rows remain completed even while artifact publication is pending.

Failure after some objects were stored (stderr, nonzero exit; no success JSON):

```text
Blob 3 (layer) sha256:55555555... Failed 8.0 MiB/8.0 MiB
Error [registry.example.com/team/example:v1]: digest mismatch for required blob sha256:55555555...
Artifact was not completed; incomplete output was removed.
```

If cleanup fails, the last line instead identifies the retained incomplete
path and cleanup error; it never claims removal. A keyboard interruption marks
the current row Cancelled, runs owned cleanup and retains Click's abort behavior.

Unsafe archive push (before credentials or registry operations):

```text
Error [./downloaded-artifact.tar.gz]: unsafe archive member '../outside'
Nothing was uploaded. Use an OCI layout archive with relative regular-file entries.
```

### 8. Outcomes and verification metrics

| Metric / guardrail | Current baseline | Delivery target / gate |
| --- | --- | --- |
| Complete four-case pull | No orchestration | All four fixtures pass before command merge. |
| Nested-index round-trip | Push rejects index root | Destination root and every stored object byte-equal to source for directory/archive fixtures before push merge. |
| Config and other object visibility | Generic blobs and single manifests have progress | Intermediate events/frame for every streamed kind; one final row per unique digest before command merge. |
| Extra root probes | No full pull | One successful root fetch, zero progress-only HEAD/GET; existing auth/retries counted separately. |
| Integrity/publication | No complete-download contract | Every mismatched/missing required object fails; zero success results or committed completion markers in fault fixtures. |
| Resource behavior | Blob helpers read chunks | No unbounded blob/file/archive read; enforce all D6 thresholds at boundary and boundary+1. |
| Compatibility | 2,122 tests per supported Python; 455 minimum-Click cases | Existing suites green plus new cases; counts are not feature acceptance by themselves. |

These are owner-workflow quality goals, not fabricated adoption/revenue targets.
No release date, customer onboarding quota or launch event is promised.

### 9. Pricing

No product pricing/billing change. Users can incur existing registry egress,
storage, network, CPU and local-disk costs; downloading all platforms may cost
more than downloading one manifest's content.

### 10. Performance and scale

D6 defines concrete proposed safety defaults, adjustable only within validated
positive integer bounds. Transfers are sequential and use 64 KiB reads; memory
does not scale with blob/archive payload size. It does scale with bounded
manifest parsing and bounded graph metadata. No "constant memory for the entire
graph" claim is made. Local snapshotting/packing adds disk work, quantified in D5.

Performance tests compare fixed small-graph 16 MiB and 256 MiB streamed blob
fixtures: largest requested read <=64 KiB and additional Python heap peak <=8 MiB.
This tests payload-size independence, not process-resident memory or maximum
graph performance. Report p50/p95 elapsed time over repeated deterministic
fixtures as evidence, without imposing a network throughput/availability service
level that this local client cannot guarantee.

> NOTE: Real-registry p95 throughput/latency and maximum-envelope memory are not
> measured yet. Implementation must record measurements; default limits are
> owner-approved engineering policies, not production certification.

### 11. Observability and troubleshooting

Progress is stderr-only; `--json`, nonterminal stderr and `--no-progress` suppress
animation, not validation. Final JSON is a single success record. Errors use the
existing error channel and nonzero exit without a partial success-shaped record.
Diagnostics never include registry credentials, signed URL queries or payload
previews added for this feature.

| Failure | Detection / diagnosis | Recovery |
| --- | --- | --- |
| 401/403 | Existing auth failure with safe registry/repository context | Check fresh credentials and pull/push scope; expiry is not the only cause. |
| Missing descendant | Failing digest and parent descriptor context | Repair source publication or select a known-complete root; retry. |
| Digest/size mismatch | Expected versus actual identity/count | Do not use partial output; inspect registry/source integrity. |
| Existing output | Preflight failure before registry calls | Choose a new path; no implicit overwrite/resume. |
| Unsafe/oversized archive | Named validation class and safe member name | Supply a supported layout archive or an explicit higher permitted limit. |
| Storage failure/interruption | Stage, retained incomplete path if any | Free space; remove only identified owned leftovers; retry explicitly. |
| Push fails after child uploads | Partial remote state, root publication status | Retry; do not delete remote content or claim transaction rollback. |

### 12. Security and privacy

Untrusted registry bytes, descriptors, local layouts and archives cross into
local storage; archive paths and digest strings never become unchecked filenames.
Use existing credential resolution and transport with source pull or destination
push scope. No new identity, daemon, cloud service, inbound listener or telemetry
backend is introduced. HTTPS remains default; existing explicit insecure behavior
is not broadened. D5/D6 and PM-4 define safe paths, archive limits and redirect rules.
Local outputs can contain sensitive metadata; private staging uses restrictive
permissions, and caller-selected outputs/logs remain caller-owned.

### 13. Risks

Highest risks are byte-changing serialization, incomplete traversal accepted as
valid, unsafe compressed input, false progress completion, and regression in
flat push/report behavior. D2-D7 and the per-item tests assign these to the
implementing library/command owners; the project owner approves residual risk.

### 14. Delivery and release gates

Design PR and native breakdown first; owner approves the actual document before
implementation. Delivery proceeds in PM-6 dependency order, one reviewed PR per
item. Final documentation and a separate Implemented inventory close-out are
required before #91 closes. Existing package/release automation remains the
publication mechanism; no release is authorized by this plan.
Internal validation, owner evaluation and a later public 0.2 release are distinct
gates. Regional rollout, customer-preview cohorts and infrastructure backfill
are not applicable to this local tool; calendar commitments are not available.

## Part II: engineering design

### A. Abstract

The design adds a complete artifact pull operation that preserves the exact
manifest graph selected by a tag or digest and writes a verified OCI layout,
optionally packaged as gzip-compressed tar. The initial root is fetched once;
its original bytes establish identity, and all later requests use descriptor
digests. A bounded graph walker discovers nested indexes, manifests, config
and layer/content blobs, validates every required edge, and stores each digest
once. Existing transfer observations feed the shared renderer through an
artifact context adapter so unknown root identity and later verification do not
create duplicate rows. New completion metadata is committed only after the
whole graph and any packaging succeed. A strict recursive layout path is reused
for offline verification, safe archive import, dry-run and layout push. Push
snapshots verified input before registry writes, uploads blobs and descendant
documents before parent/root publication, and preserves existing valid flat
workflow output shapes. Limits and explicit cleanup/error contracts bound
untrusted metadata, disk use and archive handling. This is an additive local
library/CLI capability, not a deployment service, extractor, concurrent downloader
or resumable transfer engine.

### B. Prerequisites and authority

Part I and #91 capture owner requirements, including the later required archive
and nested-index push scope. #90 is merged and closed. #112 remains independent;
#88 is not a new hard dependency. The owner approved revision 3, its limits and interface details and the native
breakdown. Design #120, all runtime children and final documentation are merged.
The authorized inventory close-out records completion before feature closure;
release remains a separate gate.

### C. Current gaps

`get_manifest` returns decoded text and retains its whole result; re-encoding
cannot guarantee original digests. `add_blob` buffers bytes and only generates
SHA-256 storage; the current layout validator does not recursively verify all
descendants/sizes and silently skips some parse failures. `push_layout` and
its CLI dry-run assume simple root manifests. Its monolithic path and integrity
helper also impose SHA-256-only checks. `push_manifest` does not enforce the
strict commit/digest acknowledgement required by this orchestration.
These are directly coupled changes; they must not be hidden behind a new CLI.

### D. Design

#### D1. Components and flow

Proposed flow, based on the inspected baseline above; arrows indicate caller to
callee. New/extended nodes are labeled. This is a design diagram, not a claim
that these runtime calls already exist.

```text
artifact pull (new CLI) -> artifacts.pull_artifact (new orchestration)
                         -> raw manifest download (new) -> RegistryClient (existing)
                         -> get_blob (existing)         -> RegistryClient
                         -> graph/layout store (new) -> directory/archive publication
                         -> typed adapter (new) -> ProgressDisplay (extended)

layout validate / layout push (extended)
  -> safe source snapshot / archive import (new)
  -> strict graph verification (shared)
  -> postorder push plan (new) -> blob upload / manifest PUT (existing, strengthened)
```

Proposed code locations: `libs/artifacts/operations.py` for orchestration and
results, `libs/artifacts/graph.py` for graph verification/planning, and
`libs/artifacts/archive.py` for the focused archive boundary. Add no more files
unless implementation scale warrants them. Extend existing manifest/layout,
progress/error modules and `cli/layout.py`; add `cli/artifact.py` and register it
in the [root command](../../src/regshape/cli/main.py). The shared graph layer
must not import CLI or `push_layout`; layout operations call it, not vice versa.

#### D2. Reference resolution and byte identity

1. Validate source/output/limits before credentials or network. Require embedded
   registry, nonempty repository/reference, safe reference characters and valid
   requested digest. Reject userinfo, query/fragment injection and traversal
   segments for this new operation; do not rewrite existing parser contracts.
2. GET the tag or digest with supported OCI/Docker schema-2 manifest/index Accept
   types and streaming enabled. No separate resolution HEAD or second root GET.
3. Stream entity bytes after HTTP content-coding removal into a private file,
   count/hash incrementally, and enforce the manifest cap before accumulating
   more data. Never charset-decode/re-encode storage bytes.
4. For a digest request, that algorithm/digest is authoritative. For a tag,
   a valid supported Docker-Content-Digest is verified and used; if absent,
   compute SHA-256. If header and requested algorithms differ, verify both
   against the same bytes rather than comparing unlike strings. Invalid,
   unsupported or mismatching digest headers fail explicitly.
5. Close the response before reporting receipt. Parse strict UTF-8 JSON only
   from the bounded stored file; preserve that file unchanged. Advertised
   media type is provisional until checked against parsed shape.

Only SHA-256 (64 lowercase hex characters) and SHA-512 (128 lowercase hex)
are accepted in this capability's path/integrity contracts. New strict graph
validation and push must support both end-to-end, replacing coupled SHA-256-only
checks; existing model serialization methods remain unchanged.

Manifest/index effective media type is the recognized descriptor/header/body
type, ignoring header parameters. Present recognized declarations must agree.
A missing body `mediaType` can use the recognized descriptor/header; missing
or generic headers can use a recognized body type. No type can be inferred
solely from arbitrary JSON keys. Unsupported types or contradictory shapes fail.
Schema version must be integer 2 (not boolean); required arrays/descriptors are
validated strictly, and duplicate JSON object keys are rejected rather than
silently choosing a conflicting value. Original unknown fields/annotations
remain in stored bytes.

Descriptor sizes are integrity constraints, unlike advisory HTTP lengths.
Each edge's size must equal stored entity bytes; booleans, coercible strings,
negative sizes and unsafe digests are rejected. Header length is used for
display only when comparable; framing errors still fail.

#### D3. Graph completeness, deduplication and validation

Use an explicit stack and visiting/complete sets, not Python call-stack recursion.
An index schedules every `manifests` descriptor in source order. A simple
manifest schedules its config and ordered layers. Preserve all platforms,
annotations, descriptor metadata and duplicate edges in original documents.
Empty layer lists and empty indexes are valid if their schema is valid; a
simple manifest still requires its config. OCI/Docker schema-2 documents are
recognized; config/layer content media types are opaque and need no plugin.

Key storage/network work by `(algorithm, hex)`. Already verified objects reuse
the same file and logical progress identity. Every repeated edge still checks
size and interpretation constraints. Different platform/annotation metadata
on otherwise identical references is allowed; incompatible size/type claims
fail. Distinct digest algorithms are distinct content addresses, not inferred
aliases. A visiting-set repeat is a cycle error; a completed-set repeat is reuse.
Deterministic traversal uses root first; within each document, visit its explicit
subject first when present, then depth-first index order or config before layers.
Observed network object order follows that traversal.

Required closure is **index-child + manifest-config/layer + explicit subject
edges**. A subject descriptor on a manifest or index identifies another supported
manifest/index in the same repository; fetch it by digest and recurse through
its content and any further explicit subject. Apply the same size/type/digest,
deduplication, cycle and resource checks to these edges. Missing or unsupported
subjects fail the full operation instead of producing a dangling local relationship.
No referrers API enumeration or reverse-link search occurs: this copies what the
selected graph explicitly points to, not every signature attached to that graph.
The subject field and original document bytes remain unchanged.

Descriptor `urls` are preserved but never followed directly. Foreign/non-
distributable layers are fetched through the registry blob endpoint; if required
content is only available externally, fail with that reason.

If descriptor `data` is present, accept strict base64 only, validate size/digest
and store its decoded content without a network transfer. Cap encoded metadata
and decoded bytes before allocation. Embedded manifests/indexes are parsed and
traversed normally. Invalid embedded data is an error, not permission to fetch
a different representation. Embedded content has a local stored row; duplicate
references do not generate fake skipped-download rows.

Introduce `validate_layout(path, *, recursive=False, limits=None)` preserving
the existing shallow default for compatibility. Recursive mode shares this
strict walker and verifies all reachable object sizes/digests; it rejects the
incomplete marker and unknown required document structures. The CLI adds
`layout validate --recursive` and accepts a directory or supported archive in
that mode. Without recursive mode, archive input or explicitly supplied limits
are rejected rather than ignored. Push always uses strict recursive validation.
No successful pull relies on the old shallow validation result.

#### D4. Directory storage and publication

Output parent must already exist and be writable. A destination that exists
(including symlink, empty directory or file) is an error before registry access.
Use an invocation-private namespace on the output filesystem, containing the
random staging directory; never use descriptor URLs,
annotations or unvalidated digests for paths. Stream blobs to staged temporary
files and promote to digest paths only after verification/close. Root/child
documents retain their exact bytes; generated wrapper metadata is the only
serialized JSON output.

**Revision-4 trust boundary.** Bootstrap requires retained no-follow directory
handles for a root/effective-user-owned path. Every ancestor must deny
group/other writes, except sticky ancestors owned by root or the effective user
(whose sticky rule protects that user's new entries). Access-control lists
(ACLs) are inspected through handles: the initial implementation rejects Linux
extended access/default ACLs and Darwin allow entries; deny-only Darwin ACLs
are accepted. Unknown/unreadable ACL policy fails closed. The new private root
must be owned by the effective user with mode `0700`. Its identity, permissions,
ACLs and bootstrap ancestry are rechecked throughout its lifetime.

These enforceable checks exclude other unprivileged identities. They **do not**
certify the absence of hostile same-user or privileged actors. The caller must
exclusively control those principals' access to the private namespace, including
its bootstrap parent, from initial acquisition until all children are closed.
If that operational precondition cannot be met, this implementation is
unsupported: use an isolated identity/environment or do not invoke it. There
is no automatic hostile-process detector and no claim that mode `0700`, random
names or another pathname check provides same-user isolation.

Create/open owned directories only within that trusted bootstrap/private
boundary. The caller-selected final name is **never** acquired by
`mkdir/stat/open`. Finish the graph and metadata privately: create the incomplete
marker, write `oci-layout`, and atomically install `index.json` last. Install the
already held staging inode at the final name with a native atomic no-replace
rename: Linux `renameat2(RENAME_NOREPLACE)` or Darwin
`renameatx_np(RENAME_EXCL)`. Every existing target, including an empty directory
or symlink, loses no data and causes an error. File promotions use the same
no-replace rule. Verify target identity against the retained original handle
before updating the ownership ledger; never adopt the object found at that name.

Keep the private namespace alive through publication and cleanup. Unsupported
platforms/native bindings fail before creation; an owned private probe verifies
filesystem collision rejection and successful no-replace movement before
registry access. Unsupported/cross-device operations fail explicitly. There is
no `rename`, replace, copy or check-then-open fallback. Windows currently has no
supported backend; its guard is tested rather than claiming safe writes.

After installation, verify associations and remove the incomplete marker only
after all required writes/diagnostics succeed. Marker removal is an irreversible
publication boundary, including cancellation immediately after the unlink
syscall. Recursive validation/push reject a marker even if other metadata exists.
Retain safely opened parent/directory handles. Every create/move/rename/marker
operation is handle-relative with no-follow semantics; never reopen the output
pathname to obtain ownership. If an entry was renamed,
replaced or became a symlink/reparse point, abort without writing through it or
deleting the replacement. Reclaim an installed, incomplete directory into a
fresh private quarantine name atomically, then check its identity **before**
deleting contents. If a raced move captured a replacement, restore it using
no-replace semantics. If restoration collides, retain it at the explicit private
location without deleting either object. Unknown/moved paths are retained with
diagnostics, not recursively removed. The private root itself is cleaned only
inside the trusted bootstrap boundary. Apply equivalent ownership, no-replace
and lifetime rules to later archive publication.
There is **no claim of atomic whole-directory visibility**: concurrent readers
must wait for completion and validate the layout.
This guards publication-path races within the stated trust boundary, not hostile
access inside private storage or modification of caller-owned output after return.

Track files/directories created by this invocation. On handled failure/cancel,
remove only owned staged/output entries; do not recursively delete an
unrecognized or replaced path. Preserve the primary exception; report cleanup
failure and the incomplete retained path. A process kill can leave clearly
named staging/incomplete output, never a successful returned result. No resume,
automatic deletion of old leftovers, or cross-process takeover is performed.

The final result/exit is authoritative. Failure writing the final stdout summary
or independent telemetry/log cleanup after durable publication cannot roll back
an already complete artifact safely; report failure/nonzero status and leave
that verified artifact intact.
This differs from a download/verification failure and must be documented.

#### D5. Archive contract and safe push source

Output is a single gzip member wrapping POSIX tar with regular OCI files and optional directories.
Use deterministic member ordering, no filesystem-owner identities, normalized
permissions, and gzip timestamp zero. This removes incidental user metadata,
but byte-identical compressed output across Python/zlib versions is not promised.
Support SHA-512 paths and large sizes through bounded POSIX extended headers.
Only `path`, `size`, `mtime` extended attributes needed by the writer are accepted;
no global, GNU long-name, sparse or device extensions in the supported profile.
Other otherwise valid tar variants fail with an explicit unsupported-profile
error, not a generic extraction attempt.

Safe import is a two-pass local operation before credentials/registry access:

1. Require a regular source file under the compressed-input byte cap, then
   enforce that cap again on actual bounded reads (a file can change after stat).
   Parse the gzip header within its separate byte cap, including optional extra,
   filename and comment fields and any header checksum, before handing payload
   bytes to a decoder. Reserved flags or malformed/overlong fields fail.
   Inflate exactly one member into an owned seekable tar spool, enforcing the
   expanded archive cap and validating its CRC/trailer. A second member or any
   bytes after the first member's trailer are rejected, even empty members or
   zero padding. Do not rely on `gzip.GzipFile`'s automatic concatenated-member
   handling. No unbounded `read()` or unbounded header scan is permitted.
2. Preflight raw 512-byte tar headers before invoking any decoder that could
   allocate an extension body. Check checksums, integer sizes and stream bounds;
   reject oversize extension records before reading them. Bound each POSIX
   record length and total metadata; honor an allowed per-file size override
   consistently when calculating data offsets.
3. Resolve effective POSIX paths and reject absolute paths, `..`, `.`, empty
   components, backslashes, drive/UNC names, NULs, links, sparse entries, special
   files, duplicate normalized paths and file/directory conflicts. Only
   `oci-layout`, `index.json`, `blobs/<supported-alg>/<exact-hex>` and their
   directories are admitted. No enclosing user directory is silently stripped.
4. Copy regular member data with bounded reads into private storage; never call
   general `extractall`. Validate exact member lengths, padding/end markers,
   member/metadata counts, the layout graph and every stored object's digest.
   Reject unreferenced archive content and nonzero data after tar end markers.
   Apply the separate small `oci-layout` marker cap and the wrapper-manifest cap
   before buffering/parsing either metadata file; neither may use the large
   archive or content-object budget as permission for a payload-sized JSON read.

Archive output is built completely in sibling staging, closed and verified
before final publication. Verify the expected member list and object hashes while
packing from the verified layout; do not re-import into an additional snapshot.
The compressed size and gzip header must satisfy the import caps before publication.
Publish the completed regular file with an exclusive
hard-link operation, then unlink its staging name; this is a no-replace
publication primitive, not `os.replace`. If the filesystem cannot provide
that primitive, fail explicitly without overwriting or copying a success-shaped
partial file. Document this local-filesystem requirement and test it on supported
platforms. The directory format can be used without hard-link support when its
own handle-safety requirements can be met.
The successful hard link is the archive publication boundary. A later unlink/
diagnostic failure follows D4's post-publication rule: retain the verified output
and report failure, rather than deleting an artifact another caller can now read.

Directory push also creates a private verified snapshot of reachable files
before the first registry operation. Read wrapper metadata once; use safe
regular-file opens, reject symlinks/reparse points in content paths, and verify
while copying. Use descriptor-relative no-follow opens where available; on
other platforms verify opened-handle containment/type before reading, or fail
closed if safe traversal is unavailable. Reopening original mutable paths after
verification or hard-linking mutable source files is not a valid snapshot.
Directory extras such as authoring stage files are ignored, never uploaded;
archive output includes only the verified closure.
Read `oci-layout` with at most its cap plus one byte before JSON decoding;
reject oversize markers even if they contain only valid JSON whitespace. Apply
this to directory snapshots, recursive validation and archive input alike.

Disk budget: directory pull needs about U unique content bytes plus bounded
metadata; archive creation needs U + compressed output C; archive push needs
expanded tar T + verified snapshot U; directory push needs a snapshot U. Moving
the completed directory tree does not duplicate U. Disk-full remains a checked
runtime error even after preflight estimates. No automatic extraction into the
user's current directory and no retained credentials in temporary storage.

#### D6. Safety limits and input validation

Proposed `ArtifactLimits` defaults, shared by pull/strict validation/archive/push:

| Field | Default | Enforcement |
| --- | --- | --- |
| `max_manifest_bytes` | 16 MiB | Each root/index/manifest and local wrapper before JSON allocation. |
| `max_layout_marker_bytes` | 64 KiB | `oci-layout`, including whitespace, before buffering/parsing from directory or archive. |
| `max_objects` | 100,000 | Unique content addresses before scheduling/storing the next. |
| `max_edges` | 500,000 | All descriptors, including repeated edges, before traversal expansion. |
| `max_depth` | 64 | Root depth 0; child depth >64 fails. |
| `max_total_bytes` | 1 TiB | Sum of distinct stored object bytes; reserve declared sizes and enforce actual counts. |
| `max_json_depth` | 128 | String-aware nesting precheck before JSON decoding; parser errors remain explicit. |
| `max_archive_bytes` | 2 TiB | Total expanded tar bytes while spooling, including headers/padding. |
| `max_archive_input_bytes` | 2 TiB | Entire compressed file, including header/trailer, before and during reads. |
| `max_gzip_header_bytes` | 64 KiB | Fixed and optional gzip header fields in total; exactly one member is accepted. |
| `max_archive_entries` | 200,010 | Raw file/directory/extension headers; reject boundary+1. |
| `max_archive_metadata_bytes` | 64 KiB | Per extension body; aggregate extension bodies <=64 MiB. |

All values must be positive non-boolean integers. Initial CLI exposes
`--max-manifest-bytes`, `--max-objects`, `--max-depth`, `--max-total-bytes` and
`--max-archive-bytes` and `--max-archive-input-bytes` on artifact pull, recursive layout validate and layout push
(including dry-run); other caps remain documented library-policy limits.
Raising one cap does not silently raise another. No total is inferred from
archive compression ratio or untrusted HTTP length. Resource failures identify
the cap and stop, rather than silently returning a subset. Defaults are policy
proposals, not OCI limits or guarantees of available disk/memory.

#### D7. Recursive layout push and existing surfaces

Build a verified graph/snapshot once and use it for real push and dry-run.
No network operations, credential-helper calls, blob HEAD or registry writes
occur in dry-run. Accept downloaded directory/archive roots and existing
multi-root flat layouts. A tag override still requires exactly one top-level
entry, not exactly one descendant.

Upload each unique config/layer blob first, retaining existing `--force`,
monolithic/chunked upload, scoped auth and per-blob progress. Existence checks
skip only confirmed existing blobs; only a genuine 404 means missing. Other
errors propagate. Verify SHA-256/SHA-512 through snapshot and both upload modes;
remove the coupled SHA-256-only precheck, not the integrity guarantee.

Publish document dependencies in postorder, including subject-before-referring-
document ordering. Every object referenced by another document must be acknowledged
by digest before that parent is sent. This makes subject-bearing round-trips
self-contained without assuming the destination already has the subject. A top-level
root with no incoming parent reference can be sent once using its final tag
(the registry stores the same digest), retaining flat-layout request counts.
A root also used as another root's descendant is first published by digest,
then its requested tag alias is published during the final root-reference phase.
Deduplicate identical `(digest, publication-reference)` operations.
The generated local wrapper is never pushed as the artifact root.

Without explicit destination tag, use each top-level descriptor's ref-name tag,
or its digest when untagged. Never invent `latest` for an untagged root. Preserve
child bytes and annotations; source ref-name belongs to the local wrapper, not
rewritten root content. A destination tag deliberately replaces that remote tag;
this is not a conditional compare-and-swap.
All publication references must be resolved and validated in the local plan
before constructing a credentialed client or uploading anything. An explicit
destination tag or present top-level `org.opencontainers.image.ref.name` must be
a string fully matching ASCII `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`. Validate present
annotations even when a tag override is supplied, so dry-run and real push have
one interpretation. Reject empty, nonstring, overlong, whitespace/control,
slash, colon, percent, query and fragment forms; never trim, URL-decode or silently
sanitize an invalid value into a different tag. This same tag rule applies to
the new pull source. Digest publication uses the strict digest grammar instead.
Reports name the exact validated publication reference. Error contexts escape
control characters and bound displayed untrusted strings.

Strengthen the manifest PUT path for graph publication with an additive
keyword-only `expected_digest=None`: when supplied, require `201 Created` and
verify any reported digest against the exact submitted bytes/expected identity.
A missing digest header is allowed with the acknowledged exact payload; report
the locally verified digest. Existing callers omitting the new argument retain
their established return behavior. Close owned responses on success/failure;
do not mask primary transport/observer errors with cleanup failures.

Preserve `PushResult`, legacy callback names and the existing CLI JSON keys.
`manifests` remains a list of publication reports, now including index documents
identified by `media_type`; index reports have `blobs: []`. `manifests_pushed`
counts acknowledged document publications (including required tag aliases).
Valid ordinary flat layouts retain their previous report shape and counts.
Nested/multi-root reports follow actual postorder publication; dry-run uses the
same plan and indicates intended operations, never successful remote writes.
Blob logical reuse does not create duplicate progress rows; existing per-manifest
reference reports and skipped accounting remain compatible.

Partial push is not transactional: already uploaded blobs/documents remain if
a later operation fails. Never delete them as rollback. A final root PUT whose
response is lost may have succeeded remotely; report outcome uncertainty and
require explicit verification/retry, not a claim that the tag was unchanged.
Normal failure before that phase leaves final root references unpublished.
Manifest-upload byte progress is not newly promised; existing push blob progress
and document publication diagnostics remain truthful.

#### D8. Observation and rendering contract

Add a frozen artifact-level context event that adapts existing `ManifestProgress`
and `TransferProgress`; do not change their public fields or required blob digest.
Fields: `transfer_id: str`, `kind: unknown|index|manifest|blob`,
`phase: preparing|transferring|received|verifying|stored|skipped|failed|cancelled`,
`digest: str | None`, `role: config|layer|None`, `bytes_completed: int`,
`total_bytes: int | None`, `attempt: int` (>=1), and
`source: network|embedded|local`. Callback type is
`Callable[[ArtifactProgress], None]`; it is optional and synchronous.
Counters are nonnegative absolute bytes for the current attempt; count cannot
exceed a present total. Embedded/local counts describe local work, not network
traffic. Invalid values raise `ValueError`; non-callable observers fail preflight.

Reserve root invocation identity before GET. Bind its verified digest to that
same identity after receipt; the renderer never allocates a new row on kind or
digest discovery. The graph owns digest-to-identity reuse. Config/layer role is
descriptive (first encountered role), not identity; content shared across roles
still occupies one row. Blob callbacks retain their current retry semantics.
Transport retries before headers keep the same Preparing row; do not fabricate
byte retry events for work not observed. No body resume/retry is added.

Receipt is not stored success: verify, close and safely store before `stored`.
Observer errors abort and propagate unchanged, disable recursive notification
and trigger owned cleanup. Preserve the original exception if a failure observer
or cleanup also fails, using the existing secondary-error mechanism.
After final required object storage, packaging and publication are distinct
artifact phases. Emit a final result only after those and required diagnostics
succeed. Stdout summary failure after publication follows D4's explicit rule.

Extend the existing normalized renderer input, rather than implementing a second
renderer. Preserve throttling (<=10 routine redraws/second, forced lifecycle/
geometry changes), width/reflow, diagnostic coordination and stdout handoff.
One final row per unique content identity is retained; duplicates have no new
row. Unknown totals show bytes/rate only, no overall percentage. All suppression
conditions apply to all object kinds. No controls enter stdout, files or logs.

### PM-1. Traceability

| Requirement / scenario | Design | Delivery/test responsibility |
| --- | --- | --- |
| R1, R2; S1/S2 | D2, D3 | Raw receive and graph items; four-root/tag-race/byte-identity fixtures. |
| R3; S1/S2 | D3, D6 | Graph item; nested/shared/inline/unknown/cycle/limit cases. |
| R4; S3 | D4, D5 | Graph and archive items; directory/archive closure equality. |
| R5; S3/S4 | D5, D7 | Recursive push item; destination-store round-trip and partial outcomes. |
| R6; S1-S4 | D8 | Raw receive, graph, command items; every-kind frame tests including config. |
| R7, R8; S4 | D2-D7, PM-5 | All source items; corruption, filesystem, archive and auth failure injection. |
| R9, R10; S3/S4 | PM-2, D3, D7 | Graph/archive/push/command; old-contract and dry-run parity tests. |
| R11; all | PM-7 | Every delivery item plus required final documentation. |

No implementation feature lacks a requirement: archive snapshotting and limits
are safeguards for R7/R8, not extra product surfaces. #112 is linked, not folded
into the breakdown.

### PM-2. Public API and output contracts

Proposed additive signatures (types are defined by this design):

```python
def download_manifest(
    client: RegistryClient, repo: str, reference: str, accept: str,
    output_path: str | Path, *,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    max_bytes: int = 16777216,
    progress_callback: ManifestProgressCallback | None = None,
) -> ManifestDownload: ...

def pull_artifact(
    client: RegistryClient, repo: str, reference: str,
    output_path: str | Path, *,
    output_format: ArtifactFormat = ArtifactFormat.OCI,
    limits: ArtifactLimits | None = None,
    progress_callback: ArtifactProgressCallback | None = None,
) -> ArtifactPullResult: ...

def validate_layout(
    layout_path: str | Path, *,
    recursive: bool = False, limits: ArtifactLimits | None = None,
) -> None: ...
```

The manifest primitive writes to an exclusively created caller-selected file,
owns that file/response, but borrows the client. It reports `RECEIVED` only after
the primitive's required size/digest verification and close; existing
`get_manifest` semantics remain unchanged and do not acquire a digest guarantee.
`ManifestDownload` is frozen: `path: str`, `digest: str`, `size: int`,
`media_type: str`, `reported_digest: str` (empty if absent).
The artifact adapter determines stored success after parsing/storage.

Extend existing `push_layout` only with keyword-only `limits=None`; broaden
`layout_path` to directory or supported archive, retaining all other positional
parameters/callbacks. Keep clients caller-owned. Export new artifact types from
the new artifacts package; manifest file receive from manifests. New errors use
`ArtifactError(RegShapeError)` for graph/storage/archive/limit context; preserve
existing auth, manifest, blob, request and caller-observer exceptions with chaining
where translation is necessary. CLI catches expected domain failures explicitly.

`ArtifactPullResult` is frozen and has: `source: str`, `resolved_reference: str`,
`root: Descriptor`, `output_path: str`, `output_format: ArtifactFormat`,
`indexes: int`, `manifests: int`, `blobs: int`, `downloaded_objects: int`,
`embedded_objects: int`, `reused_references: int`, `bytes_downloaded: int`,
`bytes_stored: int`. Counts are unique content addresses except reused references.
Bytes exclude generated wrapper/tar/gzip metadata; downloaded bytes exclude
embedded data and retry attempts not part of successful bodies.
The CLI `--json` record uses those names, `output_format` as its string value,
and `root` in OCI descriptor wire form. There is no success record on failure.

Formats are string enums `oci`, `oci-tar-gzip`. No daemon/REST service/versioned
queue is added. Package release 0.2 introduces these additive Python/CLI surfaces;
layout marker stays 1.0.0, wrapper schemaVersion stays 2, and archive contains no
new proprietary required schema. Transport stream cache bypass remains intact.
New pull exits: 0 complete; 2 usage/input-choice error; 1 runtime/domain failure
or Click abort. Unexpected caller errors retain normal process failure behavior.
No credentials are accepted in source URLs or serialized results.

### PM-3. Metrics, usage and cost

Existing opt-in diagnostics supply request timing, method/scenario timing and
request metrics. Request timing still ends at response headers; no captured
stream body preview is interpreted as an empty artifact. Final result supplies
unique counts/actual bytes; local phases expose stage and elapsed operation
timing without extending network-byte metrics to disk compression work.
R1/R3 metrics use mocked request counts; R2/R5 compare destination object stores;
R6 uses callback/frame recordings; R7/R8 use failure and bounded-read fixtures.

No usage analytics backend or per-customer tracking is introduced. Users may
retain opt-in logs/results under their own policy; this does not establish
product adoption/churn/frequency. Costs can be estimated from unique downloaded
bytes, successful upload bytes, configured retries and D5 disk use plus measured
CPU time. No registry-independent price or bandwidth guarantee is available.

> NOTE: Longitudinal per-customer adoption/churn measurement required by a
> cloud-service PM rubric is unavailable and intentionally not collected by this
> local tool. This is a review coverage gap, not claimed telemetry coverage.

### PM-4. Authentication, authorization and networking

Source requests use repository pull scope; destination publication requires
destination push and any existing existence-check pull permission. Login remains
separate through Docker-compatible credential resolution; no new identities,
permission grants, secrets files or automatic token-refresh policy. Use existing
client timeouts/retries/authentication. Close discarded responses before retries
or auth exchange; cached live streams remain prohibited.

Only the selected registry and its established registry-mediated redirects are
network destinations. Preserve transport's cross-origin credential stripping,
signed-query handling and redirect validation; do not fetch descriptor URLs or
forward registry authorization to unrelated hosts. New context/errors/logging
must redact signed queries, authorization and credential-helper output.
Tests assert redirect safety, not a blanket claim that all existing telemetry
paths have been security-certified. Private endpoints/firewalls are configured
by the caller's environment; no inbound ports, cloud networking resources or
cross-tenant hosted state are added.

### PM-5. Errors, edge cases and retry

| Class | Defined behavior |
| --- | --- |
| Bad reference/format/limits | Fail preflight; no credentials/network/output mutation. |
| Destination exists/race | Fail without changing competitor-owned content. |
| Missing/corrupt/unsupported required content | Fail full operation; never omit silently or return partial completion. |
| Header/body disagreement | Fail with safe type/identity context; no byte reserialization workaround. |
| Cycles, depth/edge/object/byte caps | Fail at boundary before scheduling/allocating more work. |
| Invalid inline data or conflicting repeated descriptor | Fail; valid differing platform/annotations alone are not conflicts. |
| Archive traversal/link/duplicate/extension bomb | Fail local import before any registry operation. |
| Transient registry status/timeout | Existing configured transport behavior only; no new nested retry loop or partial-body resume. |
| Observer/output/cancel/cleanup | Preserve primary error; cleanup owned storage; report leftovers; published-output boundary follows D4. |
| Partial/uncertain push | Keep acknowledged remote content, report stage/outcome uncertainty; no automatic deletion rollback. |

All operations are sequential per invocation. Different destinations may be used
concurrently; same destination requires exclusive reservation. Digest PUTs are
content-addressed and safely repeatable under normal registry semantics; tags
are mutable and not transactionally guarded. A retry of pull is a new invocation
and requires a new or explicitly cleaned destination.

### E. Risk checklist

| Risk category | Applies | Mitigation / owner |
| --- | --- | --- |
| Compatibility | Yes | D7/F flat API/JSON/dry-run regressions; push owner. |
| Security / untrusted archives | Yes | D5/D6 fail-closed paths, bounded headers, no general extraction; archive owner. |
| Data privacy / logs | Yes | D4/PM-4/H private staging and redaction; all item owners. |
| Dependencies / cycles | Yes | D1/J shared graph below layout/CLI; design owner. |
| Infrastructure / auth | Existing only | PM-4 unchanged scopes and explicit caller configuration. |
| Build/release | Yes | K staged source changes and later authorized release; project owner. |
| Resource contention | Local disk/CPU | D5/D6 limits and exclusive output ownership, not tenant fairness claims. |
| Regression | All touched components | M/PM-7 exact behavior and negative fixtures per component. |

### F. Compatibility and intentional behavior changes

Existing `get_manifest`, blob models/events, optional callbacks, standalone
manifest output and valid flat-layout push formats remain compatible. The
new strict recursive validation is opt-in standalone but mandatory for push;
previously accepted incomplete/malformed/size-inconsistent layouts can now fail
before uploads. Document that safety tightening in release/migration guidance.
Digest destinations accidentally treated as tags must now fail clearly.
Invalid ref-name annotations previously interpolated into URLs also fail before
credentials/uploads, even with an explicit override; valid flat tag names retain
their established behavior.
Broader canonical grammar redesign is not included.

New archive input and index support must cover `--path`, `--dry-run`, `--json`,
tag override, no-tag destination and both upload modes consistently. No UI or
infrastructure-as-code contracts change. Do not silently drop old callbacks or
change valid flat result keys while implementing the graph.

### G. Security assessment

Threat boundaries: registry -> parser/storage; local untrusted layout/archive ->
verified snapshot; snapshot -> credentialed registry publisher; diagnostics ->
caller terminal/log. Attack classes include decompression/metadata bombs, deep
JSON/graphs, path traversal, symlink/reparse races, signed-URL leaks and mutable
source files. D2-D7 specify enforcement before side effects. Hashes provide
content integrity, not trust in publisher identity or signature verification.
No custom cryptography, encryption/key store or customer-managed-key service
is introduced. Owner review plus deterministic adversarial fixtures is required;
no external security-board approval or compliance certification is claimed.

### H. Privacy and data location

Bytes move only among caller-selected registry endpoints, established redirected
storage and caller-selected local destinations/temporary storage. Those endpoints
may cross regions; the tool cannot promise residency beyond the user's setup.
No background uploads, third-party analytics, backups or retention service.
Temporary copies are removed after operations where possible; crash leftovers
and final outputs/logs are caller-owned. Restrictive staging permissions do not
encrypt data at rest; use an encrypted filesystem when required.

### I. Observability

Use existing scenario/method/request instruments and safe stage context rather
than a new backend. Local completion is final metadata + verified return;
remote completion is acknowledged postorder publications. Failure detection is
nonzero status/domain exception, object row and stage diagnostics. PM-3 maps
metrics, and section 11 gives self-service recovery. Request success does not
substitute for complete graph, archive or output success. No managed alerting
service or network-availability service level is implied.

### J. Dependencies

Reuse Requests, Click and Python standard-library hashing, filesystem, tar and
gzip facilities on supported Python 3.10-3.12. No package is added in the design.
Standard tar handling alone is insufficient without D5's bounded preflight and
manual regular-file copy; availability of a new-version extraction filter is
not assumed. Filesystem no-replace publication capability is checked explicitly.
Registry availability/rate limits/prices and disk capacity remain external
constraints with explicit failures. Completed #90 is a foundation, not a
circular dependency on #91. #112/#88 do not block this delivery.

### K. Deployment and buildout

Land raw receive, graph/store, archive, recursive push, command and final docs
through reviewed changes in PM-6 order. Modules are packaged through existing
project tooling; no service deployment, region buildout, registry backfill or
manual production setup. Enable new pull only when complete underlying paths
are present. Existing valid directory push must remain functional at every step.
Final inventory close-out and eventual automated package release are separate
owner-authorized operations.

### L. Multi-tenancy and contention

No multi-tenant backend: tenant quotas/fairness are not applicable. Multiple local
processes still compete for disk/CPU; limits and exclusive output reservation
prevent interference, not operating-system resource starvation. Never share
temporary credentials or reuse another invocation's staging directory.

### M. Regression

Cover manifest buffered/observed decoding and response ownership; blob download
integrity/redirects and both uploads; flat layout generation/validation/push and
legacy callbacks; Docker conversion/push using layout internals; artifact-option
aliases; telemetry/progress stdout isolation and CLI minimum-Click behavior.
Strict validation and SHA-512 additions need positive and negative baselines,
not broad rewrites of unrelated model parsing or cleanup.

### N. Rollback

Before implementation the design/inventory proposal can be amended or withdrawn
without runtime effects. After implementation users can avoid new commands or
use a prior package, but prior versions cannot be promised to push nested roots.
An authorized code rollback must preserve readable standard OCI outputs and
avoid deleting user data. There is no remote registry rollback transaction;
operator-directed tag repair is outside automatic cleanup. No migration of
existing artifacts or schema backfill is required.

### PM-6. Work breakdown and estimates

Seven native children: one design item and six delivery items. Proposed order
and dependencies below are technical prerequisites, not authorization to build.
Each estimate includes implementation, automated tests/source documentation,
review/CI and integration/rollback consideration. Relative S/M/L is a planning
estimate, not a promised number of days.

| Item | Deliverable | Depends on | Relative effort |
| --- | --- | --- | --- |
| [Design #113](https://github.com/toddysm/regshape/issues/113) | Combined spec, complete breakdown, Planned inventory | Completed foundations | S |
| [Raw receive #114](https://github.com/toddysm/regshape/issues/114) | Verified raw manifest-to-file API without changing decoded getter | #113 | M |
| [Graph/directory #115](https://github.com/toddysm/regshape/issues/115) | Complete traversal, strict validation, safe directory publication, artifact callbacks | #114 | L |
| [Archive #116](https://github.com/toddysm/regshape/issues/116) | Bounded gzip-tar packaging/import, archive pull output and safe snapshot | #115 | L |
| [Recursive push #117](https://github.com/toddysm/regshape/issues/117) | Index-aware validation/publication, archive source, dry-run/report/algorithm compatibility | #116 | L |
| [Command/progress #118](https://github.com/toddysm/regshape/issues/118) | Artifact command, per-object renderer adapter, result/errors/suppression, integration fixtures | #117 | M |
| [Final docs #119](https://github.com/toddysm/regshape/issues/119) | User workflow, library/command references, folder/archive/push samples, navigation | #114-#118 | S |

Archive safety and publication across filesystems are long poles. Graph and push
span shared modules but each is one coherent capability; if implementation reveals
that a safe slice cannot fit one reviewed change, stop and seek approval for a
revised native breakdown rather than silently expanding scope.
Detailed children must include Python file/type/callable documentation for new
and modified declarations, including internal helpers/tests, plus direct API docs.

### PM-7. Tests and continuous integration

Existing hosted Python 3.10/3.11/3.12 tests, minimum Click 8.1.0 and CodeQL remain
the gate. Add new command/recursive-push selectors to minimum-Click coverage.
Add a targeted Windows/macOS matrix for archive, safe-path/snapshot and
publication-boundary tests before archive/push delivery; the current Linux
matrix alone does not certify these platform-specific paths.
Use deterministic synthetic registries/transports and temporary directories;
no private credentials or paid registry infrastructure. Existing counts are
2,122 tests per Python job and 455 minimum-Click cases, not predicted future counts.

Required suites/measurements:

- Four root cases; tag changes after initial response; no progress-only probes;
  root digest absent/present/different supported algorithm/wrong value.
- Original JSON whitespace/key ordering/unknown metadata preserved; strict
  UTF-8, omitted/conflicting types, malformed schema and non-image content.
- Root + nested index + multiple manifests + shared config/layer fixture;
  SHA-256/SHA-512; inline data; empty indexes/layers; absent and explicit subjects;
  subject-bearing manifest/index round-trips into an empty repository, shared/
  nested/missing subjects and cycles; zero referrers API discovery requests;
  unsupported required type, external-only blob and repeated-edge contradictions.
- Bounds at each limit and limit+1, deep JSON/graphs, duplicate-heavy graphs,
  synthetic cycles, bounded reads/heap growth and stream closure on every exit.
- Exact final layout closure and gzip round-trip; no layer extraction; safe
  member names/types; POSIX extension limits, large size fields, duplicates,
  sparse/global extensions, gzip/tar truncation/CRC/padding/trailing-data errors.
- Compressed-input cap and gzip header cap at boundary/+1; many empty gzip
  members before/after a valid archive, overlong filename/comment/extra fields;
  oversized `oci-layout` padded with whitespace from both directory and archive.
- Existing output and destination race; symlink/reparse input; mutable source
  after snapshot; unsupported filesystem publication; disk/write/close/cleanup
  faults; cancellation and final stdout failure after durable publication.
- Rename-and-symlink replacement of a reserved destination/staging parent during
  publication: no writes outside owned handles, no deletion of replacement paths,
  failure on identity mismatch and explicit retained-output reporting.
- Revision-4 acquisition/install/cleanup races: public mkdir ownership acquisition
  rejected; preexisting substitution immediately before installation never adopted;
  held-inode mismatch before/after native movement fails; quarantine replacement
  is restored without replacement or explicitly retained on collision. Verify
  namespace lifetime, owner/permission/ACL rejection, native flag values,
  unsupported backends/filesystems, and zero registry calls on failed preflight.
- Push into a fake destination content store, then pull/verify that store:
  object bytes and root identity must match. Assert every parent is published
  after required children; tag overrides only roots; default ref-name/digest;
  flat/multi-root JSON and dry-run plan parity; no dry-run credentials/network.
- Invalid ref-name annotations and explicit tags, including `prod#suffix`,
  `prod?query`, encoded separators, controls, nonstrings and length boundaries:
  reject before credentials/uploads; valid plan/report/HTTP references agree.
- 401/auth exchange, retries/429/timeouts, redirected signed downloads,
  authorization stripping, unsupported digest and incomplete/uncertain commits.
- Visible progress frames for every index, manifest, config and layer; initial
  unknown root upgraded in-place; repeated shared objects no extra row;
  embedded/local states distinct from network bytes; failure/cancel/retry and
  final artifact packaging boundaries; no controls in stdout/files/logs.
- Full legacy suite including Docker/layout push; scoped type/lint baseline
  comparisons. No new framework unless a demonstrated test requirement needs it.

The original design-only review validated links/anchors, source/signature
evidence, sample consistency and whitespace, not unimplemented runtime behavior.
Revision-4 delivery must validate its actual implementation and native safety
matrix in addition to those document checks. No configured docs builder or
Markdown linter exists; none is claimed.

### O. Next actions and approval gate

Revision 3 and its seven linked children were owner-approved; design #120 and
all runtime/documentation deliveries #122-#127 are merged. The owner approved full
delivery and the [revision-4 publication refinement](https://github.com/toddysm/regshape/issues/91#issuecomment-6666694366).
Merge the authorized inventory close-out after exact-head checks/reviews, verify
Implemented on main, then close #91. Historical revision-3 approval is not an assertion
that the exact final revision-4 text/hash received separate owner review.
Further material changes require renewed approval. No launch, release, package bump, #112 work or branch
cleanup is authorized here.

### P. References

- [Full artifact requirements](https://github.com/toddysm/regshape/issues/91).
- [Owner clarification](https://github.com/toddysm/regshape/issues/91#issuecomment-6646891148).
- [Separate component selection](https://github.com/toddysm/regshape/issues/112).
- [Manifest progress design](manifest-progress.md).
- [Manifest operation reference](../operations/manifests.md).
- [Blob operation reference](../operations/blobs.md).
- [Layout push design/reference](layout-push.md).
- [Current progress guide](../../docs/guides/live-transfer-progress.md).
- [Current capability inventory](../../docs/project-state.md).

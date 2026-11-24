# CLI design: `layout inspect` human-readable output

**Tracking:** [#130](https://github.com/toddysm/regshape/issues/130)

**Decision date:** 2026-11-23

**Status:** Approved human-output design; not implemented. The complete feature
design and delivery breakdown are not yet approved.

**Approval record (2026-11-24):** The owner confirmed this design and authorized
its commit/PR plus creation of a dependency-aware work breakdown. The locked
scope is the human-output contract below. The remaining contracts are tracked
in [#211](https://github.com/toddysm/regshape/issues/211); their answers, the new
breakdown, implementation, merge, and release still require their own gates.
Publication of this document is tracked by
[#210](https://github.com/toddysm/regshape/issues/210).

## Overview and approval boundary

Inspect a local OCI Image Layout as a logical reference tree, with metadata for
every index individually, rather than as an ordinary filesystem directory
listing. Preserve the relationships and metadata actually stored in the layout.

This document locks the human-readable hierarchy, field presentation, metadata
ownership, complete values, and color scheme agreed during the #130 design
discussion. It supersedes earlier previews using shortened digests, tabs,
separate descriptor nodes, interpretive reference labels, or elided thumbprints.
The complete real-image example below is the baseline for this presentation.

| Area | Decision |
| --- | --- |
| CLI / SDK terminology | `regshape layout inspect` / `inspect_layout` |
| Input | Explicit local layout directory, `--path` / `-p` |
| Default output | Human-readable tree, colored when appropriate |
| Machine-readable output | `--json`; exact schema still open |
| Human display | Approved in this document |
| Summary, failures, limits, exact SDK types | Require separate design completion |
| Implementation, releases, merge | Not authorized by this approval |

This is a focused display specification, not a claim that the full PRD/EDD or
all acceptance criteria in #130 have been completed.

## Usage and compatibility

Planned command forms, not commands currently delivered by this document:

```shell
regshape layout inspect --path ./my-layout
regshape layout inspect -p ./my-layout --json
```

In 0.2, introduce `inspect` and preserve legacy `layout show`'s entry-point-only
JSON stdout. Add a migration/deprecation warning on stderr after `inspect` is
available; do not make `show` an alias for the new graph output. In 0.3, remove
the CLI `show` command. Keep the SDK's distinct `read_index` operation.

- [#208](https://github.com/toddysm/regshape/issues/208): 0.2 deprecation warning,
  dependent on delivery of `inspect`.
- [#209](https://github.com/toddysm/regshape/issues/209): 0.3 removal, dependent on
  the warning actually shipping in 0.2; not a blocker for #130's 0.2 delivery.

`inspect --json` is not a drop-in replacement for `show`'s old JSON shape.

## Local inspection and the SDK boundary

All inspection capabilities belong in the Python SDK and must be usable without
the CLI. The SDK owns bounded filesystem reads, graph discovery, metadata and
descriptor provenance, source ordering, relationships, unique-object accounting,
and structured diagnostics. Exact API signatures and result types remain open.

The CLI calls the SDK and owns text/JSON rendering, connectors, safe terminal
escaping, color, output streams, and adaptation to the eventual exit policy.
It must not reopen layout files to reconstruct metadata or associations.
The SDK must not import Click/the CLI, print, or exit.

Inspection is read-only and local. It does not modify the layout, extract or
execute content, contact a registry, resolve credentials, or require a container
daemon. Config and layer payloads remain opaque. Structural inspection is not
full blob-digest verification, signature verification, or SBOM validation.
Existing graph/storage mechanisms should be reused where appropriate without
silently turning display into payload hashing.

## Tree and spacing

- Start with one `OCI layout` node and connect the complete hierarchy from
  that root. Its child is the actual entry-point index, followed by its
  referenced objects.
- Display each index separately, including the entry point, original indexes,
  and nested indexes. Do not collapse the normal view into grouped counts.
- Preserve source `manifests` and `layers` array order. Do not reorder entries
  by platform, artifact type, digest, or label.
- Use the connected-tree style in the baseline example: four-character branch
  steps, properties on an internal node's vertical stem, and two-space
  indentation for nested property sections.
- Use spaces, never literal tabs or fixed-width label/value column alignment.
  Separate a populated field's colon from its value with one space.
- Keep the original image/config/layer subtree when adding referrers; do not
  silently switch to a condensed overview.
- Use `[0]`, `[1]`, etc. for actual descriptor-array positions. These are source
  positions, not internal node IDs. Do not label entries "First entry".
- Use `Layer 0`, `Layer 1`, etc. for image layer branches and `Content 0`, etc.
  for artifact payload branches, preserving the corresponding layer positions.
- Show repeated config/content occurrences with their complete digest and
  location, without "shared", "already shown", or similar suffixes. Repeated
  occurrences do not mean duplicate stored files; unique-object accounting
  remains distinct from edge occurrences in the SDK.

The Unicode connectors in the baseline are the human-tree presentation.
The exact ASCII fallback and traversal-limit rendering still need specification.

## Locations and metadata ownership

The layout root has one absolute `Location`, identifying the selected directory.
Every object occurrence has one `Location`, identifying its file relative to
that root. The entry-point index is located at `index.json`; digest-addressed
objects use their complete `blobs/<algorithm>/<encoded-digest>` paths.

Do not print separate Folder, File, or Version source file properties. The
layout version comes from `oci-layout`; document that source here rather than
adding another normal-output location field.

| Node | Displayed information |
| --- | --- |
| Layout | Location; `Layout format version (imageLayoutVersion)` |
| Entry-point index | Location; schema version; own media type, artifact type, annotations and subject when present; `Referenced objects (manifests)` |
| Digest-addressed index | Complete digest in heading; location; schema version; own media type, artifact type, annotations and subject when present; `Referenced objects (manifests)` |
| Manifest | Complete digest in heading; location; schema version; own media type, artifact type, annotations and subject when present; `Configuration (config)` and ordered `Layers (layers)` |
| Config | Complete digest in heading; location; no decoded payload properties |
| Layer / content | Source position and complete digest in heading; location; no extracted or decoded payload properties |

Label actual OCI properties with their exact wire names in parentheses:
`Document schema version (schemaVersion)`, `Media type (mediaType)`,
`Artifact type (artifactType)`, `Annotations (annotations)`, and so on.
Annotation keys and nested JSON keys are printed literally. `Location` is a
filesystem property, not an OCI field; do not invent an OCI field name for it.

`schemaVersion: 2` is the index/manifest schema discriminator, not an OCI release
number. `imageLayoutVersion: 1.0.0` is the layout format version; neither it nor
a media-type suffix identifies whether the documents use Image Spec 1.0 or 1.1.
Retain Docker media types verbatim when Docker documents are stored in an OCI
layout; do not relabel them as OCI documents.

A heading's digest identifies the content-addressed file. It need not occur as
a property in that object's JSON and does not imply that inspection verified
the file's digest.

### Embedded descriptors

Descriptors are embedded JSON objects, not separate files or tree nodes.
Their metadata stays inside the document containing them:

- Index: `Referenced objects (manifests)` with `[n] Digest (digest)`.
- Manifest: `Configuration (config)` with `Digest (digest)`, and
  `Layers (layers)` with `[n] Digest (digest)`.
- Referrer: optional `Subject (subject)` with `Digest (digest)`.

Always identify the referenced object using its complete digest. Show a
descriptor's platform and annotations beneath that descriptor when present.
The compact platform JSON preserves exact source field names, such as
`architecture`, `os`, `os.version`, and `variant`.

Do not copy incoming descriptor annotations or platforms onto the referenced
object's metadata. A layer descriptor's title annotation stays under that
manifest's `Layers (layers)` entry, not on the opaque content node.
Preserve separate occurrences even when two descriptors target the same digest.

The baseline selects digest, platform, and annotations from descriptors. It is
not an all-fields descriptor dump. Additional display policy for descriptor
`mediaType`, `size`, `artifactType`, `urls`, `data`, other optional fields, and
unknown extension fields must be settled separately, not silently added to the
locked default. In particular, do not fetch descriptor URLs or decode embedded
payloads to render this view.

### Optional fields and failures

Omit absent optional properties from text. Do not print `absent` or empty
placeholders for missing `artifactType`, `subject`, platform, or annotations.
An ordinary container image need not identify another object as its subject.

Do not print availability when a file exists. Missing files must retain their
expected locations and an explicit red missing-file label. Unreadable files
and malformed documents must remain explicit errors, not be silently presented
as missing or successful empty metadata. Exact diagnostic wording, partial-view
structure, and exit behavior remain open.

Missing optional values are to be represented by `null` in the eventual
normalized JSON result, subject to defining that result's exact schema. This
does not authorize inserting nulls into stored OCI documents.

## Referrers and subject direction

An OCI 1.1 referrer manifest/index contains a `subject` descriptor pointing to
its associated object. That subject does not acquire a reverse list or the
referrer's annotations.

Keep actual containment edges in the tree and show the full subject digest in
the owning referrer's property block. Do not append "Index above", "already
shown", trust claims, or positional interpretations. The baseline does not
redraw the subject's complete subtree under each referrer.

Only reachable local metadata is inspected. Do not scan unlisted blobs, query
the registry Referrers API, or imply that all possible referrers were found.
Do not assume that a referrer targets a platform manifest: the real example
below has four referrers targeting the multi-platform index.

Recognition of older tag-schema referrer-list conventions is not approved by
this display decision. The earlier synthetic OCI 1.0/1.1 signature/SBOM examples
require revision and separate approval; do not substitute them for this fixture.

## Color contract

Color provides the label/value distinction; spacing must not attempt a second
column-alignment system. The plain output has the same text and hierarchy.

| Element | Appearance | ANSI SGR |
| --- | --- | --- |
| Field labels, parentheses, array positions, annotation keys and their colons | Cyan | `36` |
| Values, including complete digests, paths, platform JSON and thumbprints | White on the dark-terminal preview | `37` |
| Tree connectors | Gray | `90` |
| `Index` heading word | Bold magenta | `1;35` |
| `Manifest` heading word | Bold cyan | `1;36` |
| `Config` heading word | Bold yellow | `1;33` |
| `Layer N` / `Content N` heading name | Bold blue | `1;34` |
| Missing-file/error labels | Bold red | `1;31` |

- Only a node heading's type/name is colored with its node-type color. Its
  complete digest is a value, not part of that colored label.
- In an indexed descriptor line, the bracketed position, label and colon are
  cyan; the actual, unabbreviated digest is white.
- Reset styling between independently styled spans and at line end. Labels
  must not bleed into values, or values into subsequent connectors.
- White is the intended dark-terminal appearance. Production value text uses
  the terminal-default foreground (`39`) to remain readable on light themes;
  the dark-terminal preview may use explicit white (`37`). ANSI colors are
  terminal-palette entries, not fixed RGB guarantees.
- Suppress ANSI in JSON, redirected/nonterminal output, and when `NO_COLOR`
  is nonempty. Do not introduce a new color-control flag as part of this
  document.
- Missing/error text remains meaningful without color; color is not the only
  error indication.

### Complete and safe values

Never shorten digests, thumbprints, annotation strings, or filesystem filenames.
Do not replace long values with ellipses, counts, or "see stored manifest".
Natural terminal wrapping is preferable to truncation.

Escape untrusted terminal control characters rather than executing them.
For example, an embedded tab or newline in a metadata value is displayed
using an escaped representation, not as output alignment or a new tree line.
Preserve ordinary visible characters and the full underlying value. Do not
interpret strings that happen to contain JSON, such as the thumbprint annotation,
as new OCI array fields.

## Baseline real-image example

Source image: `mcr.microsoft.com/azurelinux/busybox:1.36`, pinned for this capture
to the full multi-platform digest shown below. The tag is mutable; this is a
recorded snapshot, not a promise about future tag contents.

Four owner-supplied digest references were downloaded and their exact root
descriptors appended to the local entry-point index. The original image
descriptor and all digest-addressed image bytes were preserved. Inspection
itself does not perform that fixture-assembly operation.

The displayed layout root is normalized to `/examples/mcr-busybox-1.36` for
publication. No other displayed value is shortened or replaced.

```text
OCI layout
│  Location: /examples/mcr-busybox-1.36
│  Layout format version (imageLayoutVersion): 1.0.0
│
└── Index
    │  Location: index.json
    │  Document schema version (schemaVersion): 2
    │  Media type (mediaType): application/vnd.oci.image.index.v1+json
    │  Referenced objects (manifests):
    │    [0] Digest (digest): sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
    │      Annotations (annotations):
    │        org.opencontainers.image.ref.name: 1.36
    │    [1] Digest (digest): sha256:1199dc981377947d51590b7e79361ab64c17b6748a8e4a2a37475e4185e1a780
    │    [2] Digest (digest): sha256:3d0cfc6b813be4b277a2b0b46134fa134de5a3dbf427af91243e0ec230f408fc
    │    [3] Digest (digest): sha256:3f7ff85876b7f720a6a3ecaa317d48d97b0a2e1c07ff5858b689a577eb5e2aa0
    │    [4] Digest (digest): sha256:00175e7d3c6de0bd77f12b6e77d33ea815562a087253b3649a19dc6bf5bbf127
    │
    ├── Index sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
    │   │  Location: blobs/sha256/c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
    │   │  Document schema version (schemaVersion): 2
    │   │  Media type (mediaType): application/vnd.docker.distribution.manifest.list.v2+json
    │   │  Referenced objects (manifests):
    │   │    [0] Digest (digest): sha256:3e909ef8b7486601ba8c396a5dbcd6354655619219b9bc2edfa4bc9058799c72
    │   │      Platform (platform): {"architecture": "amd64", "os": "linux", "os.version": "azurelinux-1.0"}
    │   │    [1] Digest (digest): sha256:c557ab563d495bc615161bd0632bd154411f80967512be3151315a1eeadb1651
    │   │      Platform (platform): {"architecture": "arm64", "os": "linux", "os.version": "azurelinux-1.0", "variant": "v8"}
    │   │
    │   ├── Manifest sha256:3e909ef8b7486601ba8c396a5dbcd6354655619219b9bc2edfa4bc9058799c72
    │   │   │  Location: blobs/sha256/3e909ef8b7486601ba8c396a5dbcd6354655619219b9bc2edfa4bc9058799c72
    │   │   │  Document schema version (schemaVersion): 2
    │   │   │  Media type (mediaType): application/vnd.docker.distribution.manifest.v2+json
    │   │   │  Configuration (config):
    │   │   │    Digest (digest): sha256:b5e573137f3eb48cacc73be36b706aa05c853426fa8ee787c3fc66f9dd0c2131
    │   │   │  Layers (layers):
    │   │   │    [0] Digest (digest): sha256:58be4cd54090814ad90fff8481b9b4efa14ec48da55308ecb7dd8ba1ac1c15bf
    │   │   │    [1] Digest (digest): sha256:2863e8f27df388569919188f5c5f53d1c7ed444d95290c6ade1a33c295176ec3
    │   │   │
    │   │   ├── Config sha256:b5e573137f3eb48cacc73be36b706aa05c853426fa8ee787c3fc66f9dd0c2131
    │   │   │     Location: blobs/sha256/b5e573137f3eb48cacc73be36b706aa05c853426fa8ee787c3fc66f9dd0c2131
    │   │   ├── Layer 0 sha256:58be4cd54090814ad90fff8481b9b4efa14ec48da55308ecb7dd8ba1ac1c15bf
    │   │   │     Location: blobs/sha256/58be4cd54090814ad90fff8481b9b4efa14ec48da55308ecb7dd8ba1ac1c15bf
    │   │   └── Layer 1 sha256:2863e8f27df388569919188f5c5f53d1c7ed444d95290c6ade1a33c295176ec3
    │   │         Location: blobs/sha256/2863e8f27df388569919188f5c5f53d1c7ed444d95290c6ade1a33c295176ec3
    │   │
    │   └── Manifest sha256:c557ab563d495bc615161bd0632bd154411f80967512be3151315a1eeadb1651
    │       │  Location: blobs/sha256/c557ab563d495bc615161bd0632bd154411f80967512be3151315a1eeadb1651
    │       │  Document schema version (schemaVersion): 2
    │       │  Media type (mediaType): application/vnd.docker.distribution.manifest.v2+json
    │       │  Configuration (config):
    │       │    Digest (digest): sha256:cb0cce4eda8f423407494ef007c9cb3f43fbdeb085239f37663f0b3a58a84845
    │       │  Layers (layers):
    │       │    [0] Digest (digest): sha256:13bffcecfa3d5486c10d0377da452400bf7af439c1a299e49a6fc77eef66ddca
    │       │    [1] Digest (digest): sha256:ca95e05adb1550f58c9f35d515121aff44bb85cf5dfadd2b94600480f37b00ba
    │       │
    │       ├── Config sha256:cb0cce4eda8f423407494ef007c9cb3f43fbdeb085239f37663f0b3a58a84845
    │       │     Location: blobs/sha256/cb0cce4eda8f423407494ef007c9cb3f43fbdeb085239f37663f0b3a58a84845
    │       ├── Layer 0 sha256:13bffcecfa3d5486c10d0377da452400bf7af439c1a299e49a6fc77eef66ddca
    │       │     Location: blobs/sha256/13bffcecfa3d5486c10d0377da452400bf7af439c1a299e49a6fc77eef66ddca
    │       └── Layer 1 sha256:ca95e05adb1550f58c9f35d515121aff44bb85cf5dfadd2b94600480f37b00ba
    │             Location: blobs/sha256/ca95e05adb1550f58c9f35d515121aff44bb85cf5dfadd2b94600480f37b00ba
    │
    ├── Manifest sha256:1199dc981377947d51590b7e79361ab64c17b6748a8e4a2a37475e4185e1a780
    │   │  Location: blobs/sha256/1199dc981377947d51590b7e79361ab64c17b6748a8e4a2a37475e4185e1a780
    │   │  Document schema version (schemaVersion): 2
    │   │  Media type (mediaType): application/vnd.oci.image.manifest.v1+json
    │   │  Artifact type (artifactType): application/vnd.cncf.notary.signature
    │   │  Subject (subject):
    │   │    Digest (digest): sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
    │   │  Annotations (annotations):
    │   │    io.cncf.notary.x509chain.thumbprint#S256: ["279fe22c1705ff7d5ab8a00f15b4321bc1120a891be5b01b6acde032fa1c8c5e", "9b1894f223d934cbd6575af3c6e1f6096b9221a7da132185f5a5cdc92235b5dc", "23ffe2b8bdb9a1711515d4cffda04bc7f793d513c76c243f1020507d8669b7db"]
    │   │    org.opencontainers.image.created: 2026-11-22T18:40:19Z
    │   │  Configuration (config):
    │   │    Digest (digest): sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │  Layers (layers):
    │   │    [0] Digest (digest): sha256:77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a
    │   │      Annotations (annotations):
    │   │        org.opencontainers.image.title: /mnt/vss/_work/1/a/payloads/cHVibGljL2F6dXJlbGludXgvYnVzeWJveDoxLjM2LjEtYXpsMy4wLjIwMjYxMTIw.json
    │   │
    │   ├── Config sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │     Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   └── Content 0 sha256:77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a
    │         Location: blobs/sha256/77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a
    │
    ├── Manifest sha256:3d0cfc6b813be4b277a2b0b46134fa134de5a3dbf427af91243e0ec230f408fc
    │   │  Location: blobs/sha256/3d0cfc6b813be4b277a2b0b46134fa134de5a3dbf427af91243e0ec230f408fc
    │   │  Document schema version (schemaVersion): 2
    │   │  Media type (mediaType): application/vnd.oci.image.manifest.v1+json
    │   │  Artifact type (artifactType): application/vnd.cncf.notary.signature
    │   │  Subject (subject):
    │   │    Digest (digest): sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
    │   │  Annotations (annotations):
    │   │    io.cncf.notary.x509chain.thumbprint#S256: ["279fe22c1705ff7d5ab8a00f15b4321bc1120a891be5b01b6acde032fa1c8c5e", "9b1894f223d934cbd6575af3c6e1f6096b9221a7da132185f5a5cdc92235b5dc", "23ffe2b8bdb9a1711515d4cffda04bc7f793d513c76c243f1020507d8669b7db"]
    │   │    org.opencontainers.image.created: 2026-11-22T18:40:18Z
    │   │  Configuration (config):
    │   │    Digest (digest): sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │  Layers (layers):
    │   │    [0] Digest (digest): sha256:77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a
    │   │      Annotations (annotations):
    │   │        org.opencontainers.image.title: /mnt/vss/_work/1/a/payloads/cHVibGljL2F6dXJlbGludXgvYnVzeWJveDoxLjM2.json
    │   │
    │   ├── Config sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │     Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   └── Content 0 sha256:77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a
    │         Location: blobs/sha256/77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a
    │
    ├── Manifest sha256:3f7ff85876b7f720a6a3ecaa317d48d97b0a2e1c07ff5858b689a577eb5e2aa0
    │   │  Location: blobs/sha256/3f7ff85876b7f720a6a3ecaa317d48d97b0a2e1c07ff5858b689a577eb5e2aa0
    │   │  Document schema version (schemaVersion): 2
    │   │  Media type (mediaType): application/vnd.oci.image.manifest.v1+json
    │   │  Artifact type (artifactType): application/vnd.microsoft.artifact.lifecycle
    │   │  Subject (subject):
    │   │    Digest (digest): sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
    │   │  Annotations (annotations):
    │   │    org.opencontainers.image.created: 2026-11-22T18:12:55Z
    │   │    vnd.microsoft.artifact.lifecycle.end-of-life.date: 2027-11-22T17:42:17Z
    │   │  Configuration (config):
    │   │    Digest (digest): sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │  Layers (layers):
    │   │    [0] Digest (digest): sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │
    │   ├── Config sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   │     Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │   └── Content 0 sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │         Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    │
    └── Manifest sha256:00175e7d3c6de0bd77f12b6e77d33ea815562a087253b3649a19dc6bf5bbf127
        │  Location: blobs/sha256/00175e7d3c6de0bd77f12b6e77d33ea815562a087253b3649a19dc6bf5bbf127
        │  Document schema version (schemaVersion): 2
        │  Media type (mediaType): application/vnd.oci.image.manifest.v1+json
        │  Artifact type (artifactType): application/vnd.microsoft.artifact.lifecycle
        │  Subject (subject):
        │    Digest (digest): sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
        │  Annotations (annotations):
        │    org.opencontainers.image.created: 2026-11-22T18:12:51Z
        │    vnd.microsoft.artifact.lifecycle.end-of-life.date: 2027-11-22T17:42:17Z
        │  Configuration (config):
        │    Digest (digest): sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
        │  Layers (layers):
        │    [0] Digest (digest): sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
        │
        ├── Config sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
        │     Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
        └── Content 0 sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
              Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
```

### Facts and verification of this fixture

- Two platform images: Linux AMD64 and ARM64/v8, each with a config and two
  layers. Their platform metadata comes from the manifest-list descriptors.
- Two `application/vnd.cncf.notary.signature` manifests and two
  `application/vnd.microsoft.artifact.lifecycle` manifests. None is an SBOM.
- All four `subject` descriptors target the original multi-platform Docker
  manifest list, not the platform manifests or generated entry-point index.
- The signatures share one 10,903-byte COSE payload. All four referrers share a
  2-byte empty config, also used as content by the lifecycle artifacts.
- Five entry-point descriptors; 15 unique digest-addressed objects: one stored
  index, six manifests, and eight opaque blobs, totaling 11,277,619 content
  bytes. The entry-point index and layout marker are additional files.
- The assembled fixture passed recursive descriptor-size and content-digest
  validation. No signature authenticity, certificate trust, or lifecycle
  assertion was verified.

These are fixture facts, not hard-coded labels/counts, a summary-output schema,
or a promise that ordinary inspection performs that verification.

## Implementation acceptance checks for this display

1. Reproduce the baseline tree and ordering with only the layout-root location
   normalized. Cover all individual indexes, descriptor positions and file
   locations; preserve all source annotation strings and full digests.
2. With color disabled, output has no ANSI or literal tab bytes. With color
   enabled, stripping only renderer-generated SGR sequences yields exactly
   the same text. Verify label/value colors and resets, not merely the presence
   of some ANSI bytes.
3. Keep descriptor annotations/platforms with their actual containing document,
   including distinct annotations on references to the same content.
4. Preserve repeated config/content occurrences without interpretive suffixes;
   maintain unique-object counts independently of display occurrences.
5. Omit absent optional fields and present-file availability lines. Show missing
   locations and error labels explicitly with and without color, using the
   separately approved diagnostic/partial-result contract.
6. Escape terminal-control injection while keeping full readable values.
   Never shorten a digest or thumbprint to fit a terminal width.
7. Respect `NO_COLOR`, nonterminal output, and JSON ANSI suppression.

These are future implementation checks, not tests claiming an implemented
`layout inspect` command exists.

## Remaining design decisions

- Exact SDK signatures, result/diagnostic types, and normalized JSON graph
  schema, including optional-field/null semantics and serialization versioning.
- Summary grouping, counts, flags, and any summary/JSON combinations.
- Partial results, unreadable/malformed/missing-object diagnostics, size/type
  conflicts, cycles, traversal limits, and exit statuses.
- Numeric resource limits, operating-system/filesystem support, exact ASCII
  fallback, and handling of unusually large metadata without silent truncation.
- Additional descriptor fields and optional/unknown extension-field display.
- Eligibility and evidence rules for local tag-schema referrer associations.
- Completion and approval of the full feature design, current work breakdown,
  and review/release gates. The filed breakdown is listed below; it does not
  resolve the unanswered contracts above.

Changes to the locked human-output contract require an explicit design revision;
do not treat later metadata additions or implementation shortcuts as implicit
approval to change this presentation.

## Dependency-aware work breakdown

All rows are native sub-issues of [#130](https://github.com/toddysm/regshape/issues/130),
with native blocked-by relationships as well as dependencies in each issue body.
Every implementation slice includes focused tests and current source contracts;
the regression item supplements, rather than replaces, those per-slice checks.
There is exactly one final user-documentation item for the 0.2 feature.

| Work item | Deliverable | Prerequisites | Initial state | Target |
| --- | --- | --- | --- | --- |
| [#210](https://github.com/toddysm/regshape/issues/210) | Publish this approved human-output design | None | In review when this design PR opens | 0.2 |
| [#211](https://github.com/toddysm/regshape/issues/211) | Complete SDK/JSON, summary, failures, limits, encoding and scope contracts; obtain approval | #210 merged | Blocked | 0.2 |
| [#212](https://github.com/toddysm/regshape/issues/212) | Bounded local SDK inspection, provenance, diagnostics and typed JSON results | #211 approved and merged | Blocked | 0.2 |
| [#213](https://github.com/toddysm/regshape/issues/213) | Evidence-based SDK summaries and truthful counts | #212 | Blocked | 0.2 |
| [#214](https://github.com/toddysm/regshape/issues/214) | Expanded CLI tree, label/value colors and SDK-backed JSON | #212 | Blocked | 0.2 |
| [#215](https://github.com/toddysm/regshape/issues/215) | CLI summary presentation using SDK summary results | #213, #214 | Blocked | 0.2 |
| [#208](https://github.com/toddysm/regshape/issues/208) | Preserve legacy `show` JSON; add 0.2 stderr migration warning | #214; `inspect` must be available | Blocked | 0.2 |
| [#216](https://github.com/toddysm/regshape/issues/216) | Offline end-to-end/compatibility regression coverage and fixture evidence | #215, #208 | Blocked | 0.2 |
| [#217](https://github.com/toddysm/regshape/issues/217) | Final SDK/CLI/how-to documentation and migration guidance | #212, #213, #214, #215, #208, #216 | Blocked | 0.2 |
| [#209](https://github.com/toddysm/regshape/issues/209) | Remove CLI `show` only after the warning ships | #208 plus actual 0.2 release delivery | Deferred / Blocked | 0.3 |

After #212, the SDK-summary and expanded-CLI slices can proceed independently.
The 0.2 warning can follow #214 while summary presentation is completed.
The 0.3 removal is not a prerequisite for the 0.2 delivery or documentation.
Retain #130 as an open tracking issue while its deferred native child remains
open; distinguish completing 0.2 scope from closing the entire cross-release
tracking issue.

This breakdown is filed for review, not permission to start Build. #211 must
resolve its questions without silently changing this locked display baseline.
Every implementation branch still requires explicit authorization and the
normal testing/review gate.

## References

- [Layout CLI reference](layout.md)
- [Layout operations design](../layout/oci-layout-create.md)
- [Existing graph validation implementation](../../src/regshape/libs/artifacts/graph.py)
- [Existing artifact operations specification](../operations/artifacts.md)
- [Naming and compatibility decision](https://github.com/toddysm/regshape/issues/130#issuecomment-7117253471)
- [OCI Image Layout](https://github.com/opencontainers/image-spec/blob/v1.1.1/image-layout.md)
- [OCI descriptors](https://github.com/opencontainers/image-spec/blob/v1.1.1/descriptor.md)
- [OCI image manifests and subjects](https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md)

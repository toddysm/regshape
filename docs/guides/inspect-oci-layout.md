# Inspect a local OCI layout and interpret its content tree

<!-- doc-type: how-to -->

Use `layout inspect` to follow the logical references in an existing Open
Container Initiative (OCI) image-layout directory. This guide describes
unreleased **0.2 development builds**, not the published 0.1.1 package.
Check `regshape layout inspect --help` in your installation.

To obtain these development commands, follow the README's
[From source installation steps](../../README.md#installation), rather than
installing the older PyPI/Homebrew release. In an existing checkout, fetch the
approved development version, activate your intended virtual environment and
run `python -m pip install -e .` from the checkout root. Run the help check again
with that environment's `regshape`; `No such command 'inspect'` means the command
is still using an older installation or a different environment.

## Before you start

- Use Linux or macOS with the required handle-relative, no-follow filesystem
  operations. Windows fails closed. Inspection needs read access, not a private
  writable namespace or publication primitives.
- Select an existing directory containing `oci-layout`, `index.json` and its
  referenced `blobs/<algorithm>/<digest>` files. Archives are not inspection
  inputs. Paths must not contain symlink components or `..`; on macOS, use the
  actual `/private/...` path instead of `/tmp` or `/var` symlink aliases.
- Keep the directory stable during inspection. Detected changes are errors;
  the result is not an atomic filesystem snapshot.
- No registry login, credentials, network access or container daemon is needed.
  Inspection never repairs or rewrites the layout, scans unlisted blobs,
  follows descriptor URLs, decodes embedded data, reads config/layer payloads,
  extracts files or verifies signatures.
- Metadata and paths can themselves contain sensitive information. Choose
  where you save reports deliberately; terminal escaping is not redaction.

For verification rather than structural inspection, see
[`layout validate --recursive`](../../specs/cli/layout.md#layout-validate).
Its hashing, subject traversal and private-snapshot prerequisites differ.

## 1. Choose expanded text, summary or JSON

Replace `./my-layout` with your local directory:

```shell
regshape layout inspect --path ./my-layout
regshape layout inspect -p ./my-layout --summary
regshape layout inspect -p ./my-layout --json
regshape layout inspect -p ./my-layout --summary --json
```

The default expanded view shows every occurrence. The summary retains every
index and its source descriptor blocks, while condensing eligible contiguous
manifest runs into evidence-based groups. The CLI selects the
[SDK result](../library/layout-inspection.md), without rescanning or recounting.
Use the expanded view when you need individual grouped manifests' metadata.

JSON describes the **selected view**, using the
[version-1 recursive schema](../../specs/layout/layout-inspect.schema.json).
The root has `kind: "layout"`, an absolute `location`, `imageLayoutVersion`
and ordered `children`. There is no envelope or node/edge join table.
Summary JSON additionally has `statistics` and `statisticsComplete`, and
eligible child runs have `kind: "group"` rather than hidden expanded members.
Absent optional fields are omitted; present allowed empty values remain.
Logical annotation strings round-trip through JSON escaping.

In a script retaining a report, check the exit status before accepting it as complete:

```shell
if regshape layout inspect -p ./my-layout --summary --json > inspection.json; then
    python -m json.tool inspection.json
else
    printf '%s\n' 'Inspection failed or is incomplete; inspect stderr and any partial report.' >&2
    exit 1
fi
```

Exit **0** means structurally complete, **1** means a root/partial/limit/delivery
failure, and **2** means a command usage error. Invalid roots and output-budget
failures emit no stdout. Descendant failures retain a partial view and ordered
stderr diagnostics with exit 1. A valid empty index is complete; an unreadable
index is not an empty index.

## 2. Read locations and source-owned metadata

| Display | Interpretation |
| --- | --- |
| `OCI layout` | Selected local directory; its format version comes from `oci-layout`. |
| First `Index`, at `index.json` | Actual layout entry point. It is not automatically a RegShape-generated wrapper. |
| `Index <digest>`, at `blobs/<algorithm>/<digest>` | Stored original or nested index; each occurrence retains its own metadata. |
| `Manifest <digest>` | Its own document fields, configuration descriptor and ordered layer/content descriptors. |
| `Config`, `Layer N`, `Content N` | Opaque referenced file locations; no payload decoding or extraction. |
| `Subject (subject)` | A descriptor owned by the referring manifest/index, not another containment child. |

An entry point can reference simple manifests, indexes or multiple roots.
`Referenced objects (manifests)` and `Layers (layers)` preserve zero-based
source positions and ordering. Platforms and annotations beneath a descriptor
belong to that descriptor in its containing document, not to the referenced
object's own metadata. Reusing a digest does not transfer one incoming edge's
annotations onto another occurrence.

`schemaVersion: 2` is a document schema discriminator; `imageLayoutVersion:
1.0.0` is the layout format version. Neither declares the OCI specification
release used by every stored object. Docker manifest/list/config/layer media
types remain literal Docker values even inside an OCI layout.

Digests, filenames, annotation strings and thumbprints are never shortened.
Repeated digests still show their full locations: repeated references do not
mean duplicate stored files. A heading's digest identifies an address; it is
not evidence that the inspector hashed the file.

### Colors and encoding

TTY text uses cyan labels and default-foreground values, gray connectors,
bold magenta indexes, bold cyan manifests/groups, bold yellow configs,
bold blue layers/content and bold red error labels. JSON and redirected output
have no ANSI; a **nonempty** `NO_COLOR` disables color on a terminal too:

```shell
NO_COLOR=1 regshape layout inspect -p ./my-layout
regshape layout inspect -p ./my-layout --ascii
regshape layout inspect -p ./my-layout --summary --ascii
```

Unicode connectors are used when the destination encoding supports them;
otherwise connectors fall back to ASCII. `--ascii` also escapes every
non-ASCII value. Backslashes, controls, format characters and unsafe line
separators are escaped rather than interpreted; unencodable values are
escaped, never replaced with `?`. JSON uses standard ASCII-safe JSON escaping.
`--ascii --json` is a usage error, not another JSON format.

## 3. Interpret the pinned BusyBox/referrer example

The [complete real-image baseline](../../specs/cli/layout-inspect.md#baseline-real-image-example)
captures `mcr.microsoft.com/azurelinux/busybox:1.36`, pinned to:

```text
sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb
```

The tag is mutable. This assembled example preserves the original image and
adds four separately obtained referrer roots to the local entry-point index.
Inspection does not download or assemble them. Ordinary
[artifact pull](artifact-pull.md) does not discover reverse referrers either.

- Entry-point position 0 references the stored Docker multi-platform index.
  That index references two platform images, amd64 and arm64, each with a
  config and **two** layers.
- Positions 1 and 2 are manifests whose own artifact type is
  `application/vnd.cncf.notary.signature`; their displayed thumbprints are
  metadata, **not verified certificate or signature trust**.
- Positions 3 and 4 have own type
  `application/vnd.microsoft.artifact.lifecycle`; they are not invented SBOMs.
- All four explicit subjects point to the multi-platform index, not to an
  individual platform image. Their shared configs/content remain separate
  occurrences but count once per stored address.

Summary produces one platform-image group under the stored index and separate
signature and lifecycle groups under the entry point:

| Category | Source positions in its owning index | Manifest references | Unique manifests | Layer-count distribution |
| --- | --- | --- | --- | --- |
| `platform-images` | 0, 1 | 2 | 2 | `{"2": 2}` |
| `signatures` | 1, 2 | 2 | 2 | `{"1": 2}` |
| `lifecycle` | 3, 4 | 2 | 2 | `{"1": 2}` |

An index, different category/type, malformed slot or incomplete manifest splits
a group. Unknown/unknown platform values alone do not establish an attestation.
The [SDK classification rules](../library/layout-inspection.md#summarize_layout)
also cover attestations, literal generic artifact types and unclassified
manifests. A mixed group shows its layer-count histogram, not a false "Each has
one layer" statement.

Global totals are 15 unique contained objects, 21 object references and four
separate subject references. Reference totals count slots in distinct parsed
source documents, not every expanded reuse of those documents. Stored bytes
come from observed file sizes, not declared descriptor sizes. Group unique
totals are not additive when groups share content. Subject-only and unlisted
files are not added to containment storage totals.

### Reproduce the tree offline

In the [source checkout installed above](../../README.md#installation), use the existing
[fixture builder](../../src/regshape/tests/layout_inspection_fixtures.py).
Choose a nonexistent output directory:

```python
from pathlib import Path

from regshape.tests.layout_inspection_fixtures import build_layout

build_layout(Path.cwd() / "inspection-demo", "busybox")
```

```shell
regshape layout inspect -p ./inspection-demo
regshape layout inspect -p ./inspection-demo --summary
regshape layout inspect -p ./inspection-demo --summary --json
```

This fixture uses the [pinned public metadata](../../src/regshape/tests/fixtures/layout-inspection/busybox-metadata.json)
but replaces opaque payloads with two-byte stand-ins and adjusts descriptor
sizes while retaining the original names. It reproduces the full displayed
tree, not a digest-valid downloaded image. Its observed stored total is
**5,522 bytes**; the real assembled baseline has **11,277,619 bytes**.
Both have the same 15/21/4 object/reference/subject counts. Structural success
for the stand-in fixture must not be described as successful verification.
When finished, remove only the demo directories you created
(`inspection-demo` and, if used below, `inspection-incomplete`) and the report
files you created (`inspection.json` and `partial-inspection.json`). Reports can
contain sensitive metadata; retain them only in an appropriate protected
location if needed. Do not delete the original inspected layout or unrelated
files.

### Local tag-schema associations are not discovery

A reachable index referenced from the entry point with an actual
`org.opencontainers.image.ref.name` tag-schema declaration can carry
occurrence-local referrer-list evidence. Summary displays its
`Tag-schema association`; summary JSON uses `association`.
SHA-256 declarations carry a full subject digest; SHA-512 prefixes resolve only
with one matching full digest in reachable descriptor/subject metadata.
No match or multiple candidates means unresolved, not a guessed subject.
An explicit conflicting member subject is an error. Recognition never fills
in absent OCI subjects or proves that a registry's referrer set is complete.
The BusyBox example above has explicit referrer roots and subjects; it does
not need or invent a tagged referrer-list index.

## 4. Diagnose incomplete layouts without hiding failures

To reproduce a missing object separately from the BusyBox fixture, create a
second nonexistent demo directory:

```python
from pathlib import Path

from regshape.tests.layout_inspection_fixtures import build_layout

build_layout(Path.cwd() / "inspection-incomplete", "incomplete")
```

```shell
regshape layout inspect -p ./inspection-incomplete --json > partial-inspection.json
# This command exits 1; the JSON contains source-local errors.
regshape layout inspect -p ./inspection-incomplete --summary
# This also exits 1; the text shows Statistics complete: false.
```

For this fixture, the first stderr diagnostic is:

```text
MISSING_OBJECT: Referenced file is missing; Location: blobs/sha256/44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a; Source: blobs/sha256/7a0ec367b394281cc7b4922da1a5c29af73bebf68fe0385c347494af5175a1a4#/config
```

`Source` identifies the owning document and its RFC 6901 JSON pointer; `Location`
identifies the affected file. Valid siblings remain. Missing, unreadable,
malformed, unsupported and conflicting objects have distinct
[typed diagnostics](../library/layout-inspection.md#diagnostics-and-limits).
An invalid marker/entry point instead raises a no-tree failure and emits no
stdout. Summary preserves failures; it does not repair or omit damaged branches.

Recover explicitly using trusted original content or a separately prepared
valid layout, then rerun. Do not synthesize missing payloads, rewrite digests,
follow URLs or retry automatically inside inspection.
For a detected mutation, stop the concurrent writer and inspect stable input.
For a symlink or unsupported filesystem/platform, choose a supported real path;
do not bypass the safe-open checks.

For `LIMIT_REACHED`, use a separately prepared smaller layout or deliberately
reviewed [SDK limits](../library/layout-inspection.md#inspectionlimits).
There are no CLI budget overrides or unlimited mode. For `OUTPUT_LIMIT`,
explicit `--summary` may fit because only the selected view is preflighted;
it does not bypass traversal limits. A reviewed SDK `max_output_bytes` increase
is another explicit option. `OUTPUT_WRITE_FAILED` means delivery failed,
not that a report was completed; fix the destination/encoding and rerun.

## 5. Migrate from `layout show`

In 0.2, `layout show` retains its entry-point-only JSON stdout and emits one
plain stderr warning:

```text
Warning: 'layout show' is deprecated in 0.2 and will be removed in 0.3; use 'layout inspect'.
```

Use `layout inspect` for human graph inspection, optionally `--summary` or
`--json`. **Inspect JSON is not the old OCI Image Index JSON**: do not change
a script from `show` to `inspect --json` without adapting its parser.
For the original entry-point file, use `python -m json.tool ./my-layout/index.json`
or the unchanged SDK [`read_index`](../library/layout-inspection.md#read_index).
Only the CLI `show` command is scheduled for removal in **0.3**; SDK
`read_index` is not deprecated. This work does not publish either release.

## Related references

- [Layout command reference](../../specs/cli/layout.md#layout-inspect)
- [Inspection and summary SDK](../library/layout-inspection.md)
- [Approved inspection requirements and contracts](../../specs/layout/layout-inspect.md)
- [Create a layout](create-oci-layout.md)
- [Download, verify and republish an artifact](artifact-pull.md)

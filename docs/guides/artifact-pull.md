# Download, verify, and republish a complete artifact

<!-- doc-type: how-to -->

Use this workflow to keep a byte-preserving, all-platform copy of an Open
Container Initiative (OCI) artifact and publish it to another repository.
It describes the development implementation targeting **0.2**, not a released
package promise. Check `regshape artifact pull --help` in your installation.
See the [command reference](../../specs/cli/artifact.md) and
[Python application programming interface (API)](../../specs/operations/artifacts.md)
for exact options, types, budgets, and return values.

## Before you start: supported storage and trust

**Linux and macOS only, on filesystems supporting the required native operations.
Windows is unsupported and fails closed before credentials or registry access.**
Passing platform tests is not certification of every operating system,
filesystem, mount, or access-control configuration.

- Use Python 3.10–3.12 for the tested development configurations. No Docker
  daemon is needed. Use a registry/repository you may read, and a test destination
  you may write. Authenticate through [registry login](../../specs/cli/auth.md),
  not credentials embedded in a reference or command.
- Use an existing writable parent and a **new** destination. Files, empty
  directories, and symlinks already at the destination are errors. There is no
  overwrite, merge, or resume mode.
- Paths must have no symlink components, parent (`..`) components, or special
  files. On macOS, use the actual `/private/...` path rather than a `/var` or
  `/tmp` symlink alias where applicable. Do not normalize an untrusted path to
  bypass a rejected component; select and inspect a safe parent instead.
- The output parent and the system temporary parent used for verification/push
  must meet the [private-namespace policy](../../specs/operations/artifacts.md#safe-storage-and-snapshot-consumers).
  Retained ancestors must be root/effective-user owned and deny group/other
  writes, except protected sticky ancestors. Linux extended access/default
  access-control lists (ACLs) and macOS allow ACL entries are rejected;
  deny-only macOS ACLs are permitted. Unknown ACL policy fails closed.
- **Trust boundary:** exclude hostile processes with the same user identifier
  (UID) and privileged/root access to the private namespace **and its bootstrap
  parent**, from initial acquisition until all children close. Mode `0700`,
  random names, and ACL checks do not isolate those actors or detect their
  presence. Use an isolated identity/environment, or do not run the operation,
  if you cannot satisfy this precondition.
- Native no-replace rename support is required: Linux
  `renameat2(RENAME_NOREPLACE)` or macOS `renameatx_np(RENAME_EXCL)`, plus
  handle-relative/no-follow operations. A private probe checks the filesystem
  before registry access. Archive publication additionally requires an exclusive
  hard link on the output filesystem. Unsupported/cross-device operations fail;
  there is no overwrite, pathname-copy, or ordinary-rename fallback.
- Budget disk space: let **U** be unique uncompressed stored object bytes,
  **C** the compressed layout archive, and **T** its expanded tar stream.
  Directory pull needs approximately U; archive creation peaks at U+C;
  directory validation/push needs a separate U snapshot; archive import peaks
  at T+U, in addition to the existing input archive. Allow bounded metadata
  and filesystem overhead too. Default limits are policies, not reserved space.

All platforms and explicit subjects can increase transfer/storage cost.
Hashes establish byte integrity, not publisher identity or signature trust.
Payloads may contain sensitive data; use appropriate filesystem encryption.
Do not put secrets or signed download URLs in examples, logs, or issue reports.
This feature does not add payload previews or signed-URL diagnostics.

## 1. Select the root and a new output

Replace the example registry/repository/tag with an accessible artifact.
The following shell commands are templates for an authorized registry; they
are **not** a live public demo. For an executable network-free example, jump to
[Try the workflow without a registry](#try-the-workflow-without-a-registry).

```bash
ARTIFACT='registry.example/team/package:stable'
regshape artifact pull --artifact "$ARTIFACT" --output ./package-layout
```

The existing current directory must satisfy the prerequisites. Do not run
`layout init` first: pull creates the complete output itself.

| Source | Root received | What is copied |
| --- | --- | --- |
| `registry/repo:tag` | Simple manifest | That manifest, config, layers/content, and explicit subject closure |
| `registry/repo@digest` | Simple manifest | The pinned manifest and the same complete closure |
| `registry/repo:tag` | Index | The selected index, all nested indexes/platforms, manifests, blobs, and explicit subjects |
| `registry/repo@digest` | Index | The pinned index and the same all-platform closure |

Use a full real digest (`sha256:` plus 64 lowercase hexadecimal characters,
or `sha512:` plus 128). To pin a later copy, use `resolved_reference` from a
successful result as `--artifact`, with a different new output path.

The root is fetched **once**, without a resolution HEAD request or progress-only
refetch (existing authentication/request retries are separate). A tag is a
snapshot of that received root: if it changes remotely mid-operation, descendants
are still fetched by the selected digests. There is no platform selection.

Original manifest/index bytes, unknown fields, annotations, and descriptor
edges remain unchanged. Every edge is validated, but each unique content address
is downloaded/stored once. Config shared as a layer still has one object row.
Explicit `subject` descriptors are followed recursively in the same repository,
subject first; **reverse referrers discovery is not performed**. Attached
signatures not explicitly reachable are not automatically copied. Descriptor
URLs are retained but never fetched directly: externally available-only required
content fails. Valid embedded descriptor data is verified and stored locally.

## 2. Choose directory or archive

Directory (`--format oci`, the default) produces this organization. Angle-bracket
names illustrate full hexadecimal digest filenames, not literal file names:

```text
package-layout/
  oci-layout                  layout version 1.0.0
  index.json                  generated wrapper: one original-root descriptor
  blobs/
    sha256/
      <root-index-hex>         original index bytes, if the root is an index
      <nested-index-hex>       original nested index, when present
      <manifest-hex>           original manifest bytes
      <subject-hex>            explicitly referenced document, when present
      <config-hex>             config blob, even for non-image content
      <layer-hex>              original layer/content; never unpacked
    sha512/
      <hex>                   when a SHA-512 address is referenced
```

The local `index.json` is **not the original index**. It wraps even a simple
manifest. For tag sources its root descriptor gains
`org.opencontainers.image.ref.name`; digest sources get no invented tag.
The wrapper is never pushed as the artifact root.

For a single portable file:

```bash
regshape artifact pull -a "$ARTIFACT" -o ./package.tar.gz --format oci-tar-gzip
```

The archive has `oci-layout`, `index.json`, and `blobs/...` **at its root**, not
inside `package-layout/`. It holds exactly the same verified closure. Existing
compressed layers remain untouched; neither format extracts payloads or produces
a root filesystem. Filename suffixes do not choose a format.

The supported archive profile is one gzip member with a Portable Operating
System Interface (POSIX) tar stream: regular layout files and optional allowed
directories; bounded per-file `path`, `size`, and finite decimal `mtime`
extensions only. Header/trailer cyclic redundancy checks (CRC), lengths,
checksums, padding/end markers, and all independent input/metadata budgets are
checked. Concatenated gzip members or trailing bytes, links, traversal,
duplicates, sparse/special files, global/GNU extensions, unknown attributes,
and unreferenced archive content are rejected. Arbitrary tar archives are not
supported. See [archive validation details](../../specs/operations/artifacts.md#archive-reader-and-reusable-source-selection).

## 3. Verify offline and use bounded automation

```bash
regshape layout validate --path ./package-layout --recursive
regshape layout validate --path ./package.tar.gz --recursive
```

Exit zero plus `Layout at <path> is valid.` means strict recursive verification succeeded
for that snapshot. It validates every reachable digest/size, including configs
and explicit subjects. Default validation without `--recursive` remains shallow;
archives and explicitly supplied limits require recursive mode. Validation
creates private temporary storage but does not modify the source.

Set independent positive budgets to fit your environment. For example, this
allows 1 GiB of unique content (one gibibyte is 1,073,741,824 bytes):

```bash
regshape artifact pull -a "$ARTIFACT" -o ./bounded-layout \
  --max-manifest-bytes 16777216 --max-objects 100000 --max-depth 64 \
  --max-total-bytes 1073741824 --max-archive-bytes 2147483648 \
  --max-archive-input-bytes 2147483648 --json
```

Those same six limit flags work on recursive validation and push/dry-run.
Raising one does not raise another. Further library-only limits protect graph
edges, JSON nesting, the layout marker, gzip headers, archive entries, and
extension metadata; see [all budgets](../../specs/operations/artifacts.md#limits).

`--json` emits one final JavaScript Object Notation (JSON) success record on
standard output (stdout), without animation. `--no-progress` suppresses animation
but keeps the human summary. Nonterminal standard error (stderr) and JSON
telemetry also suppress all object/local-phase rows. No terminal controls enter
stdout. Check exit status before accepting any result: stdout is written only
after publication, display/scenario diagnostics, and owned log close.

## 4. Plan, then publish to a registry

Choose a destination you are authorized to modify. An explicit tag **replaces
that remote tag**, without compare-and-swap protection.

```bash
DEST='registry.example/dest:copy'
regshape layout push --path ./package-layout --dest "$DEST" --dry-run --json
regshape layout push --path ./package-layout --dest "$DEST" --json
```

The archive is accepted directly; do not extract it first:

```bash
regshape layout push --path ./package.tar.gz --dest "$DEST" --dry-run
regshape layout push --path ./package.tar.gz --dest "$DEST" --chunked --no-progress
```

Dry-run performs the same strict local snapshot and tag planning, but constructs
no registry client, calls no credential helper, and makes no network requests.
It describes intended operations, not successful writes or known remote blobs.

An override requires **one top-level root**; an index with many descendants
qualifies. Without a tag (`--dest registry.example/dest`), each root uses its
wrapper ref-name annotation, or its digest when untagged. It never invents
`latest`. A digest-qualified **destination** is invalid; to publish only by
digest, pull a digest source and use an untagged destination. Tags must match
ASCII `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`. Even overridden source annotations
are validated before credentials/uploads.

Push uploads unique blobs first, then acknowledges children and explicit
subjects by digest before their parents. Final root references come last;
a root also used as a child gets its digest publication before its final tag
alias. Confirmed existing blobs may be skipped; only a genuine 404 permits a
missing-blob upload. `--force` bypasses existence checks.

An acknowledged push preserves the root digest and original document bytes.
Verify the destination explicitly by pulling its tag to a new output and
comparing `root.digest` with the original result, then recursively validating it.
See [push results and failure boundaries](../../specs/cli/layout-push.md).

## Progress: captured screens, not an overall percentage

These are actual captures using the repository's
[nested/shared fixture](../../src/regshape/tests/artifact_fixtures.py) and
[command terminal harness](../../src/regshape/tests/test_artifact_pull_cli.py).
Digests are shortened **display labels**, not runnable inputs. The fixture
contains opaque payloads, not an executable image.

The initial root row is upgraded in place when its kind/digest becomes known:

```text
Manifest 1 Preparing 0 B (total unknown)
```

Successive snapshots of the same tiny config row include:

```text
Blob 1 (config) sha256:99c7... Downloading 0 B/25 B [------------] 0%
Blob 1 (config) sha256:99c7... Received 25 B/25 B
Blob 1 (config) sha256:99c7... Verifying 25 B/25 B
Blob 1 (config) sha256:99c7... Downloaded 25 B/25 B
```

These are overwritten states, not four retained rows. Blob transport can also
verify before its Received event; graph/storage verification is still required.
Receipt or 100% transferred is not stored success. No artificial delay is added
for the 25-byte config. Routine redraws are throttled, so very short states need
not be visibly distinct on every terminal.

Final archive screen, with one row per unique object:

```text
Index 1 sha256:43ba... Downloaded 974 B/974 B
Index 2 sha256:b572... Downloaded 183 B/183 B
Index 3 sha256:e169... Downloaded 586 B/586 B
Manifest 1 sha256:6ffc... Downloaded 736 B/736 B
Blob 1 (config) sha256:99c7... Downloaded 25 B/25 B
Blob 2 (layer) sha256:59e5... Downloaded 32 B/32 B
Manifest 2 sha256:4f82... Downloaded 724 B/724 B
Packaging OCI archive ... done
Publishing artifact ... done
```

Index 2 is the explicit subject; Index 3 is nested. Shared references reuse
five edges, not five more transfers. The directory variant has only Publishing,
not Packaging. These local phases are separate from network throughput; stored
objects alone do not mean the artifact is published.

The [renderer fixtures](../../src/regshape/tests/test_artifact_pull_cli.py#L289)
exercise unknown totals, local sources, cancellation, and failure. Representative
captured rows from those normalized observations:

```text
Index 1 Downloading 50 B (total unknown) 25 B/s
Index 1 Received 50 B (total unknown)
Index 1 Verifying 50 B (total unknown)
Index 1 Failed 50 B
Index 1 Cancelled 50 B
Blob 1 (config) sha256:4413... Stored (embedded) 2 B/2 B
Blob 1 (config) sha256:4413... Stored (local) 2 B/2 B
```

Unknown totals have no percentage or remaining-time estimate. Embedded/local
rows have no network rate; duplicates do not create fake skipped rows.
Final stdout field order and the full result schema are in the
[command reference](../../specs/cli/artifact.md#completed-output).

Actual missing-config capture from the simple-root failure fixture (Hypertext
Transfer Protocol, HTTP, status 404):

```text
Manifest 1 sha256:8787... Downloaded 543 B/543 B
Blob 1 (config) sha256:0232... Failed 0 B/19 B
Error [registry.example/source:moving]: Blob not found: registry.example/source@sha256:023288e5045e93ce2bf1d4159183249b2fe416d09d1ae79e0d48f6d2809e1b64 : HTTP 404
```

Cancellation at the same mocked boundary instead retained:

```text
Manifest 1 sha256:8787... Downloaded 543 B/543 B
Blob 1 (config) sha256:0232... Cancelled 0 B/19 B

Aborted!
```

Both invocations exited 1, wrote no stdout, and left no output in the owned
temporary parent. Those are fixture observations, not a promise that cleanup
can always succeed.

## Failure, recovery, and migration

| Situation | What to do |
| --- | --- |
| Unsupported native operation, unsafe parent/ACL, Windows | Use a supported Linux/macOS environment meeting the trust contract; do not disable safety checks. |
| Existing output | Choose another new destination; inspect existing content before any manual removal. No resume is attempted. |
| Missing/corrupt object, invalid source/tag, exhausted budget | Fix the source or deliberately adjust the named budget; retry to a new destination. A partial graph is not success. |
| Cancellation or handled failure before publication | Active row fails/cancels; no success JSON. Owned incomplete storage is cleaned when safe. Keyboard interruption retains `Aborted!` behavior. |
| Cleanup failure, replacement race, or process kill | Inspect the reported retained path. Do not blindly remove `.regshape-*` paths or another process's data; automatic takeover is not supported. |
| Summary/diagnostic/log failure **after publication** | Exit is nonzero, but complete output is retained. Verify that output before retrying; failure is not proof that the destination is absent. |
| Partial push | Previously uploaded blobs/documents remain; no rollback deletes them. Correct the failure, inspect remote state, and explicitly retry. |
| Lost response to final root PUT | Outcome is uncertain: the tag may have changed remotely. Verify destination identity before retrying; do not assume unchanged state. |

Readers must wait for successful completion and validate the output. Directory
installation is no-replace, but there is **no atomic whole-directory visibility
guarantee**. Recursive validation/push reject `.regshape-incomplete`. After
publication, the output is caller-owned; later modification is outside the
operation's safety guarantee.

For development builds containing this change, push now always performs strict
recursive validation. Previously accepted incomplete, malformed, or
size-inconsistent layouts fail before upload. Invalid ref-name annotations and
digest destinations also fail before credentials, even when a valid tag override
is supplied. Valid flat layouts retain their result keys and callback semantics;
directory authoring extras are ignored, while archive extras are rejected.

Standalone `manifest get` still retrieves one parsed/raw/selected document,
and `blob get` still retrieves a selected digest; their overwrite/output/progress
semantics have **not** become complete-artifact semantics. Separate config or
layer-position downloads [#112](https://github.com/toddysm/regshape/issues/112)
remain **Planned/unimplemented**. Full pull includes those blobs but adds no
`--part`, `--raw`, `--platform`, or component selector.
No release or package-version change is implied by these docs.

## Try the workflow without a registry

Run this Python block from a repository checkout with development dependencies,
using `PYTHONPATH=src python3` (paste it into that interpreter, or pass it on
standard input). It uses the existing **test-only** fake store, not a public
library fixture. No sockets or credential lookup occur. Native filesystem
operations are real and still require the prerequisites above.

The code exercises actual command-line interface (CLI) pull/validate/push and
library pull for both directory/archive formats. Assertions verify every stored
byte and destination root; temporary output is removed when the block exits.
Only the known temporary parent is canonicalized for macOS aliases.

```python
"""Network-free command/library round trips through the delivered operations."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner
from regshape.cli.main import regshape
from regshape.libs.artifacts import (
    ArtifactFormat, ArtifactLimits, preflight_artifact_pull, pull_artifact,
)
from regshape.libs.layout.operations import push_layout, validate_layout
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.tests.artifact_fixtures import address, nested_registry
from regshape.tests.test_artifact_push import ObjectRegistry

_, root, objects = nested_registry()
runner = CliRunner()
with TemporaryDirectory() as temporary:
    parent = Path(temporary).resolve()
    for output_format in ArtifactFormat:
        store = ObjectRegistry(root, objects)
        output = parent / output_format.value
        with (
            patch("regshape.libs.transport.client.resolve_credentials",
                  return_value=(None, None)),
            patch("requests.request", side_effect=store.respond),
        ):
            pulled = runner.invoke(regshape, [
                "artifact", "pull", "-a", "registry.example/source:moving",
                "-o", str(output), "--format", output_format.value, "--json",
            ])
            assert pulled.exit_code == 0, pulled.output
            data = json.loads(pulled.stdout)
            assert data["root"]["digest"] == address(root)
            assert data["bytes_stored"] == sum(map(len, objects.values()))
            assert "\x1b" not in pulled.stdout
            checked = runner.invoke(regshape, [
                "layout", "validate", "--path", str(output), "--recursive",
            ])
            assert checked.exit_code == 0, checked.output
            planned = runner.invoke(regshape, [
                "layout", "push", "--path", str(output),
                "--dest", "registry.example/dest:copy", "--dry-run", "--json",
            ])
            assert planned.exit_code == 0, planned.output
            assert not store.objects
            pushed = runner.invoke(regshape, [
                "layout", "push", "--path", str(output),
                "--dest", "registry.example/dest:copy", "--json",
            ])
            assert pushed.exit_code == 0, pushed.output
            assert store.references["copy"] == address(root)
            assert store.objects == objects

            events, phases = [], []
            library_output = parent / (output_format.value + "-library")
            limits = ArtifactLimits(max_total_bytes=1024**3)
            preflight_artifact_pull(
                "registry.example", "source", address(root), library_output,
                output_format=output_format, limits=limits,
            )
            client = RegistryClient(TransportConfig(registry="registry.example"))
            result = pull_artifact(
                client, "source", address(root), library_output,
                output_format=output_format, limits=limits,
                progress_callback=events.append, phase_callback=phases.append,
            )
            assert result.root.digest == address(root)
            assert not result.root.annotations  # Digest source: no invented tag.
            validate_layout(str(library_output), recursive=True, limits=limits)
            published = push_layout(
                str(library_output), client, "dest", limits=limits,
            )
            assert published.manifests_pushed > 0
            assert store.references[address(root)] == address(root)
            assert store.objects == objects
            assert events and phases
```

This proves the synthetic closures under the local test environment, not
interoperability or performance with every live registry. The existing
[four-case command tests](../../src/regshape/tests/test_artifact_pull_cli.py#L157)
add simple/index × tag/digest × directory/archive coverage and a pull-back check.

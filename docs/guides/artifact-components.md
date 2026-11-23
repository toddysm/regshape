# Download a config or zero-based layer

<!-- doc-type: how-to -->

Save one config or content layer without downloading a complete artifact.
This guide describes **unreleased development builds targeting 0.2**, not a
published-package guarantee. Check `regshape config get --help` and
`regshape layer get --help` in your installation. No Docker/Podman daemon is
required.

| Need | Operation |
| --- | --- |
| Exact config payload | `config get` |
| Exact layer/content payload at position N | `layer get --index N` |
| Config/layer descriptor metadata, not content | `manifest get --part config` or `--part layers` |
| Blob with a digest you already know | `blob get` |
| Complete manifest/index graph, configs, layers and explicit subjects | [artifact pull](artifact-pull.md) |

`config` and `layer` are resources. `artifact` is reserved for whole-artifact
and meta operations. Component download does not produce an OCI layout.

## Before you start: output and trust

- Use an authorized registry/repository and a **simple OCI image manifest or
  Docker schema-2 image manifest**, including non-image artifacts represented
  by such a manifest. Config/layer media types do not have to describe images.
  An index or manifest list needs an explicit child-manifest target.
- For a private registry, first use
  [`regshape auth login`](../../specs/cli/auth.md). Component commands resolve
  Docker credential-store credentials automatically; there are no per-command
  username/password options.
- **Linux/macOS only**, on a compatible native filesystem. Windows fails
  closed before credentials or registry access. Handle-relative/no-follow
  operations, native no-replace rename support for private staging, and
  exclusive hard-link publication are required. Unsupported filesystems have
  no copy/overwrite fallback. Platform tests do not certify every mount.
- Choose a **new file under an existing writable, trusted parent**.
  `--output/-o` is required; there is no implicit filename, stdout payload,
  overwrite, append, force, resume, or verify-only mode. `-o -` is rejected.
  Existing files, directories, symlinks and dangling links are all refused.
- Parent paths must have no symlink or `..` components. On macOS, use the
  actual `/private/...` path rather than `/tmp` or `/var` aliases where needed.
  Inspect a safe parent; do not normalize an untrusted path to bypass checks.
- Retained ancestors must be root/effective-user owned and deny group/other
  writes except protected sticky ancestors. Linux extended access/default
  ACLs and macOS allow ACL entries are rejected; macOS deny-only ACLs are
  allowed. Unknown ACL policy fails closed. See the shared
  [native storage contract](../../specs/operations/artifacts.md#safe-storage-and-snapshot-consumers).
- Exclude hostile same-user and privileged processes from the private namespace
  **and its bootstrap parent** throughout acquisition and cleanup. Random names
  and mode `0700` do not isolate those actors. Use an isolated identity/environment
  or do not run the operation if that trust precondition cannot be met.

Reserve disk space for the manifest, selected blob, metadata and any range
scratch. The published file is a hard link to verified staging bytes, not a
second full copy. Hash verification establishes integrity, not publisher trust.
Payloads may be sensitive; use appropriate local storage protection.

## 1. Download config or layer content

These are templates for an authorized registry, not live public examples.
Replace the registry, repository and tag, and use distinct absent output paths
under the trusted parent prepared above:

```bash
ARTIFACT='registry.example/team/package:stable'
regshape config get --artifact "$ARTIFACT" --output ./config.bin
regshape layer get -a "$ARTIFACT" --index 0 -o ./first-layer.bin
```

`--artifact` and `-a` are equivalent. These new commands **do not** accept
`--image-ref/-i`; deprecated aliases remain only on the pre-existing commands
described in the [migration guide](artifact-options.md).
The registry must be embedded, without a URL scheme or credentials. An omitted
tag defaults to `latest`; explicit tags are preferable for repeatable scripts.

Layers are ordered and **zero-based**: a single-layer artifact has layer **0**.
For a manifest with three layers, the first is 0 and the last is 2:

```bash
regshape layer get -a "$ARTIFACT" --index 2 -o ./last-layer.bin
```

There is no automatic "last" selector. Negative/noninteger indices fail as
usage errors; zero-layer manifests and out-of-range indices report that no
valid index exists or give the valid range. Subjects do not count as layers.

Config whitespace, encoding and a missing final newline are preserved. A
non-JSON config and arbitrary binary/non-image layers work identically.
Compressed payloads remain compressed: no JSON reserialization, decompression,
archive extraction, execution or implicit format conversion occurs.

## 2. Pin a manifest or select an index child

Use the successful result's `resolved_reference` for later selections from
the same manifest. A digest target uses `@`, not `:`, before the digest.
This complete digest is from the deterministic fixture used to check the
commands, **not an object hosted at this example registry**; replace it and
the repository with your actual simple-manifest target:

```bash
PINNED='registry.example/team/package@sha256:289d5c9e579d51de30e30b67bcd729297b83d15fefbf4fa19a49d49bb7d885e6'
regshape config get -a "$PINNED" -o ./pinned-config.bin
regshape layer get -a "$PINNED" --index 0 -o ./pinned-layer.bin
```

Each invocation receives one manifest and selects a descriptor from that
verified snapshot. If a tag moves afterward, the blob still comes from the
selected digest; it does not resolve the tag again. Separate tag invocations
can select different manifests.

If the target is an index, inspect it with `manifest get`, choose the desired
child descriptor explicitly, then use that child's manifest digest as
`PINNED`. If it is another index, repeat until you reach a simple manifest.
There is no host-platform or arbitrary-first-child selection and no
`--platform` flag. For all platforms, use [complete-artifact pull](artifact-pull.md).

## 3. Bound receipt and inspect metadata

```bash
regshape config get -a "$ARTIFACT" -o ./bounded-config.bin \
  --max-manifest-bytes 16777216 --max-total-bytes 1073741824 --json
regshape layer get -a "$ARTIFACT" --index 0 -o ./quiet-layer.bin --no-progress
```

`--max-total-bytes` counts **unique verified manifest plus selected blob bytes**,
not just the blob. Equality with the cap is allowed; overflow fails, with the
blob budget reserved before its receipt. A content address shared by manifest
and blob is counted once. A verified empty blob consumes zero blob bytes but
still requires receipt and digest/declared-size verification; even one extra
byte fails. There is no `--max-blob-bytes` option and no integrity-disable flag.
The [command reference](../../specs/cli/components.md) lists exact defaults.

Both operations verify the selected digest **and** declared size; manifest
digest-addressed requests are verified too. An unavailable selected registry
blob fails even when its descriptor embeds valid `data` or lists `urls`.
Unlike component receipt, `artifact pull` retains its existing embedded-data
fallback. For embedded-only content use that complete-artifact workflow and
its content-addressed layout, or
[publish the component blob](../../specs/cli/blob.md) to the same repository
before retrying component receipt. External descriptor URLs are not followed.

Shared `--concurrency`, `--ranges-per-blob`, `--range-size`,
`--download-retries`, `--scratch-dir`, `--sequential` and `--legacy-transport`
controls retain the [download-policy contracts](../library/downloads.md#policy).
One component is selected, not multiple layers in parallel; ranges can overlap
within that blob. Default pooled transport uses four slots and one full stream.
Eligible ranges need a blob at least four times the range size. Scratch defaults
to private component staging; an explicit trusted scratch root overrides it.
See the [command option/conflict table](../../specs/cli/components.md#shared-download-controls).

### Progress and output channels

An interactive stderr terminal shows actual manifest receipt, processing and
selected **Config** or **Layer** bytes, not a fabricated whole-operation percent.
These are projections of captured deterministic fixture frames (bars, spacing
and rates omitted), with a 728-byte manifest and a 77-byte config:

```text
Manifest 1 Received 728 B (total unknown)
Manifest 1 Processing 728 B (total unknown)
Manifest 1 Downloaded 728 B
Config 1/1 sha256:554dcd9fc9e4... 0 B / 77 B Downloading
Config 1/1 sha256:554dcd9fc9e4... 77 B / 77 B Verifying
Config 1/1 sha256:554dcd9fc9e4... 77 B / 77 B Committing
Config 1/1 sha256:554dcd9fc9e4... 77 B / 77 B Downloaded
```

The same fixture's layer 0 shows `Layer 1/1`, digest prefix
`sha256:7795220b3997...`, and `22 B / 22 B`. `1/1` is a transfer count,
**not** the zero-based layer position. Manifest completion is not component
completion, and even a terminal row is not a substitute for final process status.

`--no-progress`, non-TTY stderr, `--json`, or JSON telemetry suppress animation,
not receipt, verification, errors or deferred warnings. Progress, diagnostics,
warnings and opt-in telemetry use stderr; stdout contains only result metadata,
never payload bytes. Redirecting stdout alone does not suppress terminal stderr.
Keep `--log-file` separate from output, including symlink/hard-link aliases.
Linux conservatively rejects absent same-parent names equivalent after Unicode
normalization/case folding; choose names differing beyond case.

## 4. Handle publication and failure

Receipt uses private staging under the output parent. After verification and
transport teardown, publication uses an exclusive **hard link without
replacement**. A concurrent destination collision never authorizes overwrite.
The public hard-link syscall is the retained-output boundary, even if an
exception interrupts the syscall's return handling.

Before that boundary, handled receipt, callback, cancellation and teardown
failures remove owned private bytes when safe, **including already verified
private bytes**. Receipt, `COMMITTING`, or receiver-to-staging handoff does not
transfer public ownership. After publication, complete output is caller-owned
and is never deleted by the operation, including on late callback, cleanup,
renderer, telemetry or result failures.

Cleanup checks identity and reports secondary cleanup failures without replacing
the primary error. It cannot safely delete arbitrary replacement entries,
guarantee cleanup after a process kill, or prevent hostile same-user mutation.
Inspect reported retained locations; do not blindly delete `.regshape-*` paths.
Publication is not an `fsync`-backed crash-durability guarantee.

**A nonzero exit does not imply no file. File existence, a Downloaded row,
or parseable JSON does not prove command success.** Result writing can leave
partial metadata; final stdout flush or deferred-warning failure can leave
complete metadata with nonzero status. Check final process status before using
results; independently inspect/verify any retained output before reuse/retry.

| Failure | Exit | Recovery |
| --- | --- | --- |
| Missing flags, `-o -`, malformed reference, negative/noninteger index, option conflict, log/output alias | 2 | Correct usage; use embedded registry, modern selector and distinct new paths. |
| Index target, missing config, zero layers or out-of-range position | 1 | Inspect manifest metadata; target a simple child digest or a valid position. |
| Auth/HTTP 401, missing manifest/blob | 1 | Check repository permissions/login and digest availability; embedded-only blobs need the workflow above. |
| Corrupt digest, short/extra content or declared-size mismatch | 1 | Repair source content/descriptor; retry to a new path. Never bypass verification. |
| Manifest/total/structural cap | 1 | Confirm intended size/structure; deliberately adjust the named limit, not all limits blindly. |
| Existing file/directory/dangling link, unsafe parent/ACL, unsupported platform/filesystem, disk failure | 1 | Choose a new supported trusted destination; fix space/permissions without disabling safeguards. |
| HTTP 429/retry exhaustion or scratch pressure | 1 | Wait for quota, reduce concurrency/ranges, select trusted disk scratch; see [retry recovery](parallel-downloads.md#4-handle-capability-fallback-and-retries). |
| Keyboard interruption before publication | 1 | Wait for active readers/cleanup, then inspect state before retrying. |
| Late callback, cleanup, diagnostic or result failure | Nonzero | Complete file and partial/complete metadata may remain; inspect status and independently verify retained bytes. |

Socket inactivity timeouts do not bound elapsed time against a continuously
trickling peer; cancellation can wait for readers to stop. Do not remove active
staging/scratch. Abrupt termination may bypass normal diagnostics.

## Use the SDK directly

The [component SDK reference](../library/components.md) gives complete
signatures, result fields, errors and callback lifetimes, plus a runnable
network-free example. Production Python usage needs no CLI import:

```python
from regshape.libs.components import download_config, download_layer
from regshape.libs.transport import RegistryClient, TransportConfig

client = RegistryClient(TransportConfig(registry="registry.example"))
config = download_config(client, "team/package", "stable", "./sdk-config.bin")
manifest_digest = config.resolved_reference.rsplit("@", 1)[1]
layer = download_layer(
    client, "team/package", manifest_digest, "./sdk-layer.bin", layer_index=0,
)
print(layer.digest, layer.size, layer.output_path)
```

The output parent must already meet the same trust/storage policy. The client
is borrowed, not closed or reconfigured by component operations.

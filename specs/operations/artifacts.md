# Complete artifact directory/archive downloads and verification

<!-- doc-type: reference -->

This reference covers the delivered complete-artifact library in development
builds targeting 0.2 (tested Python 3.10–3.12), under the
[approved artifact design](../cli/artifact-pull.md). It does not assert release
availability. **Native storage supports Linux/macOS on compatible filesystems;
Windows is unsupported and fails closed.** The
[private-namespace trust contract](#safe-storage-and-snapshot-consumers) is a
prerequisite, not a same-user sandbox.
Recursive publication is covered by the [layout push reference](../cli/layout-push.md).
See the [complete workflow and executable mock-safe example](../../docs/guides/artifact-pull.md)
and [command reference](../cli/artifact.md).

## Python application programming interface

Import `ArtifactError`, `ArtifactFormat`, `ArtifactLimits`, `ArtifactProgress`,
`ArtifactProgressCallback`, `ArtifactPhase`, `ArtifactPhaseCallback`,
`ArtifactPullResult`, `preflight_artifact_pull`, `pull_artifact` and
`DirectorySnapshot`, `ArchiveSnapshot`, `DownloadOptions`, `DownloadStats`,
`DownloadStatus`, and `snapshot_layout`
from `regshape.libs.artifacts`.

### Signatures and caller ownership

```text
pull_artifact(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    output_format: ArtifactFormat = ArtifactFormat.OCI,
    limits: ArtifactLimits | None = None,
    progress_callback: ArtifactProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ArtifactPullResult

preflight_artifact_pull(
    registry: str, repo: str, reference: str, output_path: str | Path, *,
    output_format: ArtifactFormat = ArtifactFormat.OCI,
    limits: ArtifactLimits | None = None,
) -> None

snapshot_layout(path: str | Path, *, limits: ArtifactLimits | None = None)
    -> DirectorySnapshot | ArchiveSnapshot
```

The client is borrowed and already configured. Repository/reference and registry
authority validation happens before network or filesystem writes. The output
parent must exist. Existing destinations, including empty directories and
symlinks, are never adopted or overwritten.
`repo` is the repository within the client's registry; `reference` is a tag or
full digest, not a complete registry URL. `limits=None` selects `ArtifactLimits()`.
Callbacks must be callable or `None`; invalid observers raise `TypeError` before
effects. The library never renders progress.

`download_options=None` establishes one pooled operation context before root
discovery for supported clients; roots, child manifests and blobs share scoped
authentication and reusable connections. The original client is borrowed, not
closed or mutated by workers. Unsupported custom clients retain the original
serial path with one logged notice when options are omitted. Explicit pooled
requests require a safe fork override. `DownloadOptions.legacy()` selects the
original serial path without the new provider/pool; `sequential()` still uses
modern pooled transport. This transport foundation does not yet schedule graph
objects in parallel. See [download policy and transport](../../docs/library/downloads.md)
for policy fields, retry precedence and downstream feature boundaries.

`download_status_callback` adds immutable caller-thread per-blob observations
and one aggregate summary after publication/cleanup, without changing
`ArtifactPullResult` or `ArtifactProgress`. Blob status IDs match artifact
progress rows. Summary request/retry counts include root and child-manifest
traffic. The original error remains authoritative, and observer exceptions
retain their exact identity. Legacy and unsupported custom clients reject this
new observer before effects; their old omitted-option calls remain compatible.
See [status and statistics](../../docs/library/downloads.md#status-and-statistics).

Command callers use `preflight_artifact_pull(registry, repo, reference, output_path,
output_format=ArtifactFormat.OCI, limits=None)` **before constructing the client**
to reject invalid source/policy/destination inputs before credential lookup.
It also creates/removes a private native-publication probe under the checked
parent. It makes no registry calls and does not reserve the destination.
`pull_artifact` repeats checks and holds its own handles to reject later races.

`ArtifactFormat.OCI` writes a directory. `ArtifactFormat.OCI_TAR_GZIP` writes
a single gzip member containing a POSIX (Portable Operating System Interface)
tar archive of exactly the same verified closure at its root. Use the enum,
not a filename extension, to select it. The
[runnable example](../../docs/guides/artifact-pull.md#try-the-workflow-without-a-registry)
constructs the client, callbacks, budgets and safe temporary outputs for both
formats without credentials or network access.

The archive contains `oci-layout`, `index.json`, then digest paths in lexical
order; no enclosing directory, payload extraction, credentials or staging files.
Owner/group names and identifiers, modification times, and gzip timestamp are
zero/empty; regular-file mode is `0644` in tar. The published file itself remains
private (`0600`). SHA-512 paths and sizes above 8 GiB use bounded per-file POSIX
`path`/`size` records. Repeated output is deterministic on the same runtime;
byte-identical compression across Python/zlib versions is not promised.

### Identity and graph

The original tag/digest is fetched once, without a resolution HEAD request.
All platforms, nested indexes, explicit subjects, configs and layers are
traversed. Subjects precede index/config/layer children. No reverse referrers
discovery or direct fetching of descriptor URLs occurs. Content must be
available from the registry or valid embedded descriptor data.

Exact received bytes are stored under their verified Secure Hash Algorithm
(SHA-256 or SHA-512) address.
Unknown fields and descriptor metadata stay in those bytes; documents are never
reserialized. One generated wrapper descriptor identifies the original root.
Only a tag source adds `org.opencontainers.image.ref.name` to that wrapper.
When the root declares `artifactType`, its validated classification is also
retained on the wrapper descriptor and `ArtifactPullResult.root`.

Each descriptor occurrence is checked, even when its object was already
downloaded. Different size, media type or document/blob interpretation fails.
Strict UTF-8 JSON rejects duplicate keys, excessive nesting, noninteger schema
versions and unsupported/contradictory document types. Embedded base64 must be
canonical and match both digest and size; it never falls back to a network fetch.
An iterative traversal detects cycles and checks reused-subtree depth.

### Limits

`ArtifactLimits` is frozen. Every field is an independent positive non-boolean
integer. Defaults:

| Field | Default |
| --- | --- |
| `max_manifest_bytes` | 16 MiB |
| `max_layout_marker_bytes` | 64 KiB |
| `max_objects` | 100,000 |
| `max_edges` | 500,000 |
| `max_depth` | 64 |
| `max_total_bytes` | 1 TiB |
| `max_json_depth` | 128 |
| `max_archive_bytes` / `max_archive_input_bytes` | 2 TiB each |
| `max_gzip_header_bytes` | 64 KiB |
| `max_archive_entries` | 200,010 |
| `max_archive_metadata_bytes` | 64 KiB |

KiB, MiB and TiB mean kibibytes (1024 bytes), mebibytes (1024² bytes), and
tebibytes (1024⁴ bytes). These are independent policy caps, not available-space
guarantees. Boolean, zero, negative and noninteger values raise `ValueError`.
Archive limits apply independently: input bytes include gzip header/trailer;
expanded bytes include every tar header/padding/end byte; entries include raw
directory and extension headers. Gzip headers include all optional fields and
their delimiters/checksum. POSIX extension bodies are capped individually and
at 64 MiB in aggregate. Raising one cap never raises another.
Roots have depth zero. Root descriptors
count as edges, but the generated wrapper is not a content object. Total bytes
count distinct addresses, excluding generated metadata. Descriptor sizes reserve
the remaining total before downloads; actual receives check budgets before writes.
Payloads stream in at most 64 KiB pieces. Metadata is separately capped before
buffering/parsing, including whitespace in `oci-layout`.

### Observations and result

Observers receive frozen `ArtifactProgress` records synchronously. An unresolved
root has one stable `transfer_id` before its digest/type is known. Every unique
address has one identity; shared config/layer roles retain the first encountered
role. `source` distinguishes network, embedded and local work.

| `ArtifactProgress` field | Type / default | Contract |
| --- | --- | --- |
| `transfer_id` | `str`, required | Nonempty stable invocation/object identity; root keeps it across kind/digest discovery |
| `kind` | `str`, required | `unknown`, `index`, `manifest`, `blob` |
| `phase` | `str`, required | `preparing`, `transferring`, `received`, `verifying`, `stored`, `skipped`, `failed`, `cancelled` |
| `digest` | `str \| None`, required | Unknown initially, otherwise strict lowercase SHA-256/SHA-512 address |
| `role` | `str \| None`, required | `config`, `layer`, or `None`; first encountered role, not identity |
| `bytes_completed` | `int`, required | Nonnegative absolute bytes in the current attempt |
| `total_bytes` | `int \| None`, required | Nonnegative total or unknown; count cannot exceed a present total |
| `attempt` | `int = 1` | At least one; boolean counters are invalid |
| `source` | `str = "network"` | `network`, `embedded`, or `local` |

`ArtifactProgressCallback = Callable[[ArtifactProgress], None]`.
Invalid event values raise `ValueError`. Counters are not deltas or aggregate
artifact totals; embedded/local counts are not network bytes.

`received` is not `stored`: parsing, integrity checks and safe storage still must
succeed. No library code renders a terminal. Observer exceptions propagate
unchanged, including immutable exceptions and cancellation. Further notifications
are disabled after an observer raises.

The optional `phase_callback: ArtifactPhaseCallback | None = None` parameter on
`pull_artifact` announces local work separately: `ArtifactPhase.PACKAGING` before
archive packing and `ArtifactPhase.PUBLISHING` before directory installation or
the completed archive's exclusive publication. Directory output has only the
publication phase. Callbacks run synchronously before that work and may abort it
with their original exception; neither notification asserts success or carries
invented network bytes. Completed object observations remain complete during
these local phases.
`ArtifactPhase` is a string enum (`PACKAGING = "packaging"`,
`PUBLISHING = "publishing"`), and
`ArtifactPhaseCallback = Callable[[ArtifactPhase], None]`. These additive hooks
do not change `ManifestProgress`, `TransferProgress`, or standalone blob/manifest
semantics. See the [callback reference](../../docs/library/transfer-progress.md).

### Command integration

```text
regshape artifact pull --artifact registry.example/team/package:stable --output ./new-layout
regshape artifact pull -a registry.example/team/package:stable -o ./copy.tar.gz --format oci-tar-gzip --json
```

`--artifact/-a` and `--output/-o` are required. The shared legacy
`--image-ref/-i` aliases retain their deprecated-in-0.2 wording. Format defaults
to `oci`, regardless of suffix. `--max-manifest-bytes`, `--max-objects`,
`--max-depth`, `--max-total-bytes`, `--max-archive-bytes` and
`--max-archive-input-bytes` expose the corresponding positive integer budgets
above. Existing telemetry flags and global `--log-file` remain supported.
Native publication supports Linux/macOS, not Windows, under the trust
preconditions below.
The [canonical command reference](../cli/artifact.md) includes exact source
validation, option defaults, human output order, and JSON field types.

Interactive stderr uses the shared renderer for one retained row per unique
index, manifest or blob, including tiny configs; unknown-root kind/digest discovery
updates the original row. Embedded/local work says `Stored (embedded/local)`
rather than claiming a network transfer. Packaging/publication are separate
local rows. `--no-progress`, `--json`, nonterminal stderr and JSON telemetry
disable every progress row. Width adaptation and routine-redraw throttling are
shared with existing commands.

The final text or `--json` result is written only after publication, progress
cleanup, scenario diagnostics and owned telemetry-log close succeed. JSON uses
the `ArtifactPullResult` field names, the format string and root descriptor wire
form. A later stdout/diagnostic error exits nonzero and retains complete published
output; it never rolls publication back. Usage errors exit 2; runtime failures
and cancellation exit nonzero. No `--part`, `--raw` or `--platform` selector is added.
Operation-owned filesystem failures use the normal `Error [source]: ...`
diagnostic after incomplete-output cleanup. Transport failures report only
`Transport failed (<exception type>)` alongside the source, not raw exception
messages that may contain signed redirect queries. Observer and post-publication
diagnostic/result sink exceptions retain their original identity.

`ArtifactPullResult` is returned only after complete publication. Its frozen
fields are `source`, `resolved_reference`, `root`, `output_path`, `output_format`,
`indexes`, `manifests`, `blobs`, `downloaded_objects`, `embedded_objects`,
`reused_references`, `bytes_downloaded`, and `bytes_stored`. `root` retains the
existing mutable `Descriptor` value type. Counts are unique addresses except
reused references. Byte counts exclude generated metadata and unsuccessful
attempts; downloaded bytes also exclude embedded data.
`source`, `resolved_reference`, and absolute `output_path` are strings;
`output_format` is `ArtifactFormat`; all counters are integers. JSON uses
`root.to_dict()` (camel-case descriptor wire keys) and `output_format.value`.
The [exact field table](../cli/artifact.md#completed-output) includes all
thirteen fields; there is no partial result on failure.

## Safe storage and snapshot consumers

The [storage primitives](../../src/regshape/libs/artifacts/storage.py) require
no-follow, handle-relative filesystem operations. Unsupported platforms fail
closed before writes; there is no pathname-write fallback. Source path components
must not be symlinks. This currently excludes ordinary Windows Python filesystem
operations; callers must handle the explicit unsupported-platform error.
Parent (`..`) components are rejected before normalization rather than letting
a preceding symlink disappear from the no-follow walk.

Revision 4 uses a `.regshape-private-*` namespace on the output filesystem, with
staging **inside** that private root. Bootstrap checks retained ancestor handles:
owners must be root or the effective user; group/other writes are forbidden
except root/effective-user-owned sticky ancestors. Linux extended access/default
access-control lists (ACLs) and Darwin allow ACL entries are rejected; Darwin
deny-only ACLs are permitted. Unknown ACL policy fails closed. The private root
must remain owned by the effective user with mode `0700`. Its identity, access
policy and ancestor policy are rechecked throughout its lifetime.

**Caller trust precondition:** the caller must exclusively control access by
same-user and privileged principals to the private namespace and its bootstrap
parent for the full lifetime. Permission/ACL checks do **not** isolate hostile
same-user processes and cannot detect their presence. If that precondition is
unavailable, use an isolated identity/environment or do not invoke these writing
operations. Random names and mode `0700` are not advertised as a same-user sandbox.

The [native backend](../../src/regshape/libs/artifacts/native.py) uses Linux
`renameat2(RENAME_NOREPLACE)` or Darwin `renameatx_np(RENAME_EXCL)`. Missing
bindings/platform support fail before creation; an owned private collision/
success probe checks filesystem support before registry access. There is no
ordinary-rename, replace, copy or check-then-open fallback. Cross-device or
unsupported filesystem operations fail explicitly.

Prepare exact graph bytes and metadata privately with `.regshape-incomplete`
present and `index.json` written last. Install the already held directory inode
atomically at the final name without replacing any existing target, and check
the installed identity against that held handle. The public name is **never**
opened to acquire ownership. Remove the marker only after commit checks; its
removal is irreversible even if cancellation arrives immediately afterward.
There is no atomic whole-directory visibility guarantee: concurrent readers
must validate and reject the marker.

Within the stated trust boundary, replacements cannot redirect writes or be
adopted for deletion. Failed installed trees are atomically reclaimed into a
private quarantine name and identity-checked before any content is deleted.
If cleanup captured a replacement, it is restored without overwriting a new
winner. If restoration collides, its exact private location is retained and
reported. Unknown/moved paths are retained rather than recursively deleted.
Cleanup cannot replace the primary failure. After complete publication, a close
or caller-output failure leaves the artifact intact and still reports failure.

Archive publication instead uses a handle-relative, no-follow **exclusive hard
link** of an already closed/verified private regular file, followed by unlinking
its staging name. This requires local filesystem hard-link support; unsupported,
cross-device, permission or disk-full errors have no copy/overwrite fallback.
The successful link is irreversible even if cancellation arrives immediately
after it. Neither later unlink/diagnostic failure nor a replacement at the
public path authorizes deleting that public entry. Archive creation peaks at
unique content U plus compressed output C and bounded metadata, not U+C plus
another tar/snapshot. Objects are rehashed while packing.

### Archive reader and reusable source selection

```python
with snapshot_layout("/safe/source/layout-or-archive", limits=ArtifactLimits()) as snapshot:
    graph = snapshot.graph
    wrapper_bytes = snapshot.wrapper_bytes
    for item in graph.objects:
        # Consume only the private store inside this lifetime.
        snapshot.store.verify(item.digest, item.size)
```

`snapshot_layout(path, *, limits=None)` selects a directory or regular archive
without relying on its extension. `ArchiveSnapshot(path, *, limits=None)` is a
single-use context; `DirectorySnapshot` remains available for known directory
inputs. Source symlinks, special files and unsafe ancestors are rejected through
no-follow handles; archive reads check opened-file identity/size/mutation metadata.
Consumers **must not** reopen original source paths or hard-link mutable source
files. `store`, `graph`, and `wrapper_bytes` belong to the entered context.
After exit the private files/handles are unavailable.

`graph.objects` is dependency-first and deduplicated by content address.
`graph.roots` contains normalized graph edges, **not** complete original root
descriptor metadata. Consumers needing platform/annotation/tag/unknown fields
must interpret the bounded original `wrapper_bytes`, not reconstruct them from
normalized edges. This is the source contract used by recursive push.

Import first spools one gzip member with bounded compressed reads and bounded
deflate output. It verifies the optional header checksum, trailer cyclic
redundancy check (CRC) and modulo-32-bit size. It rejects every extra member or
trailing byte, including empty gzip members and zero padding. Raw 512-byte tar
headers are then preflighted before extension allocation: checksum, numeric
profile, effective offsets/sizes, metadata/entry budgets, padding and end markers.
Only regular `oci-layout`, `index.json`, strict SHA-256/SHA-512 blob paths and
optional `blobs`/algorithm directories are accepted. Duplicate paths, unsafe
components, links, sparse/special files, global/GNU extensions, and unknown POSIX
attributes fail explicitly. Only local `path`, `size`, and finite decimal `mtime`
attributes are supported. Data is manually copied with bounded reads, never
general extraction. Strict recursive graph verification rejects missing/corrupt
objects and **unreferenced archive content** before a consumer can perform
registry effects.

Peak import disk is expanded tar T plus copied content U and bounded metadata.
The tar spool is removed before context entry returns; only the private verified
snapshot remains. Directory snapshots need U, ignore unrelated authoring files,
and still verify required files. Metadata caps include whitespace before JSON
decoding. Disk exhaustion, cancellation and cleanup errors propagate; cleanup
preserves the primary failure and reports retained private storage when needed.

For later storage consumers, compose `SafeDirectory.open(parent)` with
`SafeDirectory.private_namespace(parent_handle)`, then
`namespace.temporary()`. Keep the namespace context alive until every child is
closed, including after installation or failed cleanup. `parent.install(child,
name)` accepts only a pre-opened owned private child. Public `mkdir` acquisition
and transfer between distinct private namespaces are rejected. File promotion
likewise requires private handles and native no-replace movement. Do not retain
these handles beyond their owning contexts.

`with DirectorySnapshot(path, limits=...) as snapshot:` reads wrapper metadata
once, verifies/copies reachable regular files into private storage, and closes
the source before returning. `snapshot.store`, `snapshot.graph` and
`snapshot.wrapper_bytes` are available only within that context. Graph objects
are in dependency-first order and retain ordered child digest occurrences.
Directory extras, including authoring staging files, are ignored. Consumers
must use the snapshot, never reopen original mutable paths or hard-link them.
Snapshot exit removes owned content. These primitives are reusable boundaries,
not implementations of registry publication.
Snapshots apply the same namespace trust/native requirements to their system
temporary parent; the mutable source itself need not satisfy the writing trust
policy. Recursive validation also uses a temporary private verified snapshot;
it never writes to the original source.

## Layout validation

`validate_layout(path, *, recursive=False, limits=None)` retains the existing
shallow default. Explicit limits without recursive mode raise `ValueError`.
Recursive directory/archive validation uses the same graph rules, safe opens and
marker cap, and supports both digest algorithms. Missing inline-content files still
fail local validation: a successful local layout must contain its whole closure.

```console
regshape layout validate --path ./layout --recursive --max-depth 64
regshape layout validate --path ./layout.tar.gz --recursive --max-archive-input-bytes 1073741824
```

Available limits are `--max-manifest-bytes`, `--max-objects`, `--max-depth`,
`--max-total-bytes`, `--max-archive-bytes` and `--max-archive-input-bytes`.
Without `--recursive`, supplying any limit is a usage error and archive input is
rejected. Pull and push use the strict recursive rules; the shallow default
does not establish complete-artifact integrity.

### Error contract

Invalid source/policy syntax raises `ValueError`; invalid callback types
raise `TypeError`. Graph, archive-profile and storage-policy failures raise
`ArtifactError`, as does an unsupported output format. Invalid destination
syntax may raise `ValueError`; unsafe parent components raise `ArtifactError`.
Transport/authentication/manifest/blob errors retain their
existing exception classes; filesystem operations may raise `OSError`.
Synchronous observer exceptions and cancellation propagate unchanged.
Cleanup preserves the primary failure, reporting secondary failures/retained
storage rather than claiming successful removal. After publication, a close,
diagnostic, or caller-output failure leaves the complete artifact intact.
Consumers must distinguish that boundary rather than retrying into an existing
destination or assuming a nonzero command exit means no output exists.

## Additive receive hooks

`download_manifest(..., output_dir_fd=None)` accepts a borrowed safe directory
handle; its output path must then be a single filename. All exclusive creation,
identity checks and cleanup use that handle. The caller verifies parent
association and platform support.

`get_blob(..., max_bytes=None, output_dir_fd=None)` keeps legacy behavior when
the new keywords are omitted. Supplying `max_bytes` enables exclusive output,
bounded entity decoding/error bodies and pre-write size checks.
`output_dir_fd` requires `max_bytes`. This opt-in path requires an output filename
and returns the actual verified entity size. Blob redirects retain Requests'
credential-stripping behavior; their discarded intermediate bodies are bounded
before Requests can consume them eagerly. Response/file closure precedes
successful receipt. Transport, authentication, manifest, blob and caller
exceptions keep their existing taxonomy; graph/storage-policy failures use
`ArtifactError`, while filesystem failures may propagate as `OSError`.

Both transport modes require a 401 response's **actual issuer origin** (scheme,
host and effective port) to match the configured registry before interpreting
its challenge or attempting a no-challenge `/v2/` fallback. Redirected probes
and authentication retries have the same check; missing/invalid origin metadata
fails closed, and rejected responses are closed without masking the auth error.
Successful signed content-delivery-network downloads remain supported.
This does not restrict token-realm origin: a challenge actually issued by the
trusted registry may legitimately delegate token issuance to an external realm.

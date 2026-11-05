# Complete artifact directory downloads and verification

This reference covers the graph/directory implementation of
[#115](https://github.com/toddysm/regshape/issues/115), under the
[approved artifact design](../cli/artifact-pull.md). It does not claim delivery
of archive output, recursive push, or the artifact command-line interface.

## Python application programming interface

Import `ArtifactError`, `ArtifactFormat`, `ArtifactLimits`, `ArtifactProgress`,
`ArtifactProgressCallback`, `ArtifactPullResult`, `pull_artifact` and
`DirectorySnapshot` from `regshape.libs.artifacts`.

```python
result = pull_artifact(
    client, "team/package", "stable", "/existing/parent/new-layout",
    limits=ArtifactLimits(max_total_bytes=20 * 1024**3),
    progress_callback=events.append,
)
```

The client is borrowed and already configured. Repository/reference and registry
authority validation happens before network or filesystem writes. The output
parent must exist. Existing destinations, including empty directories and
symlinks, are never adopted or overwritten.

`ArtifactFormat.OCI` is currently the only implemented format.
`ArtifactFormat.OCI_TAR_GZIP` is reserved and fails before side effects.

### Identity and graph

The original tag/digest is fetched once, without a resolution HEAD request.
All platforms, nested indexes, explicit subjects, configs and layers are
traversed. Subjects precede index/config/layer children. No reverse referrers
discovery or direct fetching of descriptor URLs occurs. Content must be
available from the registry or valid embedded descriptor data.

Exact received bytes are stored under their verified SHA-256 or SHA-512 address.
Unknown fields and descriptor metadata stay in those bytes; documents are never
reserialized. One generated wrapper descriptor identifies the original root.
Only a tag source adds `org.opencontainers.image.ref.name` to that wrapper.

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

Archive fields are policy reserved for the subsequent archive layer, not a claim
that archive handling is implemented. Roots have depth zero. Root descriptors
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

`received` is not `stored`: parsing, integrity checks and safe storage still must
succeed. No library code renders a terminal. Observer exceptions propagate
unchanged, including immutable exceptions and cancellation. Further notifications
are disabled after an observer raises.

`ArtifactPullResult` is returned only after complete publication. Its frozen
fields are `source`, `resolved_reference`, `root`, `output_path`, `output_format`,
`indexes`, `manifests`, `blobs`, `downloaded_objects`, `embedded_objects`,
`reused_references`, `bytes_downloaded`, and `bytes_stored`. `root` retains the
existing mutable `Descriptor` value type. Counts are unique addresses except
reused references. Byte counts exclude generated metadata and unsuccessful
attempts; downloaded bytes also exclude embedded data.

## Safe storage and later consumers

The [storage primitives](../../src/regshape/libs/artifacts/storage.py) require
no-follow, handle-relative filesystem operations. Unsupported platforms fail
closed before writes; there is no pathname-write fallback. Source path components
must not be symlinks. This currently excludes ordinary Windows Python filesystem
operations; callers must handle the explicit unsupported-platform error.

Private sibling staging owns an explicit file/directory identity ledger.
Publication exclusively reserves the destination, creates `.regshape-incomplete`,
moves verified content through retained handles, writes `index.json` last, and
removes the marker only after commit checks. There is no atomic whole-directory
visibility guarantee: concurrent readers must validate and reject the marker.

Replaced or renamed parents/reservations cannot redirect writes. Cleanup removes
only recognized owned objects; unknown/replaced paths are retained with a
diagnostic rather than recursively deleted. Cleanup cannot replace the primary
failure. After durable publication, a close or caller-output failure leaves the
complete artifact intact and still reports failure.

`with DirectorySnapshot(path, limits=...) as snapshot:` reads wrapper metadata
once, verifies/copies reachable regular files into private storage, and closes
the source before returning. `snapshot.store`, `snapshot.graph` and
`snapshot.wrapper_bytes` are available only within that context. Graph objects
are in dependency-first order and retain ordered child digest occurrences.
Directory extras, including authoring staging files, are ignored. Consumers
must use the snapshot, never reopen original mutable paths or hard-link them.
Snapshot exit removes owned content. These primitives are reusable boundaries,
not implementations of archive packing/import or registry publication.

## Layout validation

`validate_layout(path, *, recursive=False, limits=None)` retains the existing
shallow default. Explicit limits without recursive mode raise `ValueError`.
Recursive directory validation uses the same graph rules, safe opens and marker
cap, and supports both digest algorithms. Missing inline-content files still
fail local validation: a successful local layout must contain its whole closure.

```console
regshape layout validate --path ./layout --recursive --max-depth 64
```

Available limits are `--max-manifest-bytes`, `--max-objects`, `--max-depth`,
`--max-total-bytes`, `--max-archive-bytes` and `--max-archive-input-bytes`.
Without `--recursive`, supplying any limit is a usage error. Archive input is
not yet supported, even in recursive mode.

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

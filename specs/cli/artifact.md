# CLI: `artifact pull`

<!-- doc-type: reference -->

The command-line interface (CLI) downloads a complete Open Container Initiative
(OCI) graph. This is the development implementation targeting 0.2, not a promise
that published 0.1.1 contains it. For the procedure, storage prerequisites,
captured progress, and recovery, use the
[complete-artifact guide](../../docs/guides/artifact-pull.md).

**Native publication supports Linux/macOS on compatible filesystems only.
Windows is unsupported and fails closed.** Private storage requires the
[caller trust boundary](../../docs/guides/artifact-pull.md#before-you-start-supported-storage-and-trust);
it is not a sandbox against hostile same-user or root processes.

## Usage and options

```text
regshape artifact pull --artifact REGISTRY/REPOSITORY:TAG --output NEW_PATH [OPTIONS]
regshape artifact pull --artifact REGISTRY/REPOSITORY@DIGEST --output NEW_PATH [OPTIONS]
```

Capitalized values are placeholders. Both simple manifests and indexes are
accepted. All platforms, nested indexes, configs, layers, and explicit subjects
are required; no reverse referrers discovery, direct descriptor-URL fetching,
payload extraction, or resume is performed.

| Option | Default / required | Contract |
| --- | --- | --- |
| `--artifact`, `-a` | Required | Registry/repository with tag or digest; an omitted reference uses the shared parser's `latest` default |
| `--image-ref`, `-i` | Legacy alias | Deprecated in 0.2 help/docs; retained without a runtime warning |
| `--output`, `-o` | Required | New directory/file under an existing safe parent; never overwrites |
| `--format` | `oci` | `oci` directory or `oci-tar-gzip`; suffix does not select format |
| `--max-manifest-bytes` | `16777216` | Per manifest/index, including generated wrapper |
| `--max-objects` | `100000` | Distinct content addresses |
| `--max-depth` | `64` | Root depth zero |
| `--max-total-bytes` | `1099511627776` | Unique content bytes, excluding generated metadata |
| `--max-archive-bytes` | `2199023255552` | Expanded tar bytes |
| `--max-archive-input-bytes` | `2199023255552` | Compressed gzip bytes |
| `--json` | Off | One completed result on stdout; no animated progress |
| `--no-progress` | Off | Suppress object and local-phase rows, not transfers/validation |
| `--help` | — | Installed command help |

All six limit options require positive integers. Library-only budgets are
listed in [ArtifactLimits](../operations/artifacts.md#limits). The same six
options apply to [recursive validation](layout.md#layout-validate) and
[layout push](layout-push.md#options), including dry-run.

Source tags must fully match ASCII `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`.
Digests use `sha256:` plus 64 or `sha512:` plus 128 lowercase hex characters.
References must embed a registry and nonempty repository; URL schemes,
credentials/userinfo, traversal, backslashes, whitespace/controls, queries,
fragments, and percent forms are rejected, not sanitized. A nonempty registry
port must be numeric and within the parser's port range; bracketed Internet
Protocol version 6 (IPv6) is supported. Pull preflight currently accepts an empty
port delimiter, such as `registry.example:/repo:tag` or `[::1]:/repo:tag`;
do not rely on it rejecting those spellings. In contrast,
[push destination validation](layout-push.md#reference-resolution) rejects them.
Destination paths reject parent components and symlinks. Preflight precedes
credential lookup, but does not reserve the destination; publication repeats
ownership/collision checks.

Global `--insecure`, `--verbose`, and `--log-file` precede `artifact`.
Command telemetry options are `--time-methods`, `--time-scenarios`,
`--debug-calls`, `--metrics`, `--telemetry-format [text|json]`, and
`--telemetry-verbosity/-tv` (integer 0–2). Treat diagnostic files as sensitive;
never supply or paste signed URLs/credentials as source references.
`--json`, JSON telemetry, nonterminal stderr, and `--no-progress` suppress
all animated rows. Progress/diagnostics use stderr, not stdout.

There are no `--platform`, `--part`, or `--raw` flags on this command.
Standalone manifest/blob commands retain their existing semantics;
[component selection #112](https://github.com/toddysm/regshape/issues/112)
remains Planned.

The [library batch/status APIs](../../docs/library/downloads.md#public-batch-downloads)
do not add CLI options or change this command's stdout/progress schemas.
Artifact blob receipt shares an operation-owned coordinator, but graph
acquisition is still serial. Parallel graph scheduling, range flags and
typed-status/deferred-warning rendering remain separate follow-up work.

## Completed output

Human output contains exactly these fields in this order:
`Downloaded complete artifact.`, `Source`, `Resolved`, `Output`, `Format`,
`Objects`, `Shared references reused`. `Output` is absolute. This capture uses
the [nested fixture](../../src/regshape/tests/artifact_fixtures.py);
only the temporary parent is replaced with `<temporary-parent>`:

```text
Downloaded complete artifact.
Source: registry.example/source:moving
Resolved: registry.example/source@sha256:43ba918cc708b178c6abafac3998bf336f1e5cfa270e29fb09ea26fb05c42352
Output: <temporary-parent>/copy.tar.gz
Format: oci-tar-gzip
Objects: 3 indexes, 2 manifests, 2 blobs
Shared references reused: 5
```

`--json` serializes the following exact `ArtifactPullResult` fields, not a
progress stream. JSON means JavaScript Object Notation.

| Field | JSON type | Meaning |
| --- | --- | --- |
| `source` | string | Selected registry/repository tag/digest reference |
| `resolved_reference` | string | Registry/repository pinned to the verified root digest |
| `root` | object | Descriptor wire form: `mediaType`, `digest`, `size`, plus applicable descriptor metadata, including tag annotation / `artifactType` |
| `output_path` | string | Absolute published path |
| `output_format` | string | `oci` or `oci-tar-gzip` |
| `indexes` | integer | Unique original index objects, excluding the wrapper |
| `manifests` | integer | Unique simple manifests |
| `blobs` | integer | Unique config/layer content addresses |
| `downloaded_objects` | integer | Successfully received network objects |
| `embedded_objects` | integer | Objects stored from embedded descriptor data |
| `reused_references` | integer | Repeated edges to already visited content |
| `bytes_downloaded` | integer | Successful network object bytes, excluding embedded data and failed attempts |
| `bytes_stored` | integer | Unique stored content bytes, excluding generated metadata |

For the captured fixture these counts are respectively `3`, `2`, `2`, `7`,
`0`, `5`, `3260`, `3260`. SHA-256 and SHA-512 (Secure Hash Algorithm variants)
are distinct addresses; identical bytes under different algorithms are not
silently treated as one object.

The result is emitted **after** completed publication, renderer/scenario
diagnostics, and owned log close. Object completion is not artifact completion.
This is not a crash-durability guarantee: publication does not synchronize files
and containing directories with `fsync`. A host crash or power loss can therefore
lose or damage output even after a successful command.
No terminal controls are written to stdout. Usage failures exit 2; operation
failure/cancellation exits nonzero (normally 1) without a success result.
Errors after publication, including stdout/log failure, leave complete output
intact and still fail. A broken stdout can contain a partial write; always check
exit status rather than interpreting a partial record as success.

## Related surfaces

- [How-to, directory/archive layouts, captured frames, mock-safe example](../../docs/guides/artifact-pull.md)
- [Library signatures, observations, budgets, archive profile](../operations/artifacts.md)
- [Layout validation](layout.md#layout-validate)
- [Recursive push, destination tags, and partial outcomes](layout-push.md)
- [Approved design and original baseline](artifact-pull.md)

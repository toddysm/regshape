# CLI: `config get` and `layer get`

<!-- doc-type: reference -->

Delivered command reference for **unreleased 0.2 development builds**. See the
[workflow and recovery guide](../../docs/guides/artifact-components.md) and
[SDK reference](../../docs/library/components.md). The
[approved design](../operations/artifact-components.md) is historical design
evidence; this reference describes the merged implementation.

## Usage and selection

```bash
regshape [GLOBAL OPTIONS] config get --artifact REGISTRY/REPO:TAG --output NEW_FILE [OPTIONS]
regshape [GLOBAL OPTIONS] layer get --artifact REGISTRY/REPO:TAG --index N --output NEW_FILE [OPTIONS]
```

`REGISTRY/REPO@sha256:HEX` and `@sha512:HEX` also work, with complete lowercase
64/128-character hexadecimal digests. An omitted tag defaults to `latest`.
The registry must be embedded; no URL scheme/userinfo or `--registry` option.
Credentials come from the normal Docker credential store; authenticate
separately with [auth login](auth.md).

One simple OCI image manifest or Docker schema-2 image manifest is pinned,
then only its selected registry blob is received. Non-image config/layer
media types are supported. Indexes/lists are rejected; select an explicit
simple child-manifest digest, not a host platform. Subjects are not layers.

| Option | Default | Contract |
| --- | --- | --- |
| `--artifact`, `-a` | Required | Same-value repetitions permitted; conflicting repetitions rejected. No legacy `--image-ref/-i` aliases on either new resource. |
| `--output`, `-o` | Required | Absent file under an existing supported trusted parent. No implicit name, stdout (`-`), overwrite, append, force, resume or verify-only mode. |
| `--index` | Required on `layer get` only | Nonnegative integer, zero-based; one layer means index 0. No valid index for zero layers. |
| `--chunk-size` | `65536` | Positive streaming I/O buffer bytes, not HTTP range size. Pooled reads remain capped at 64 KiB. |
| `--max-manifest-bytes` | `16777216` | Positive verified manifest-byte cap. |
| `--max-total-bytes` | `1099511627776` | Positive unique verified manifest-plus-selected-blob byte budget (1 TiB). Inclusive at equality; reserve the selected size before blob receipt. |
| `--json` | Off | Exactly eight completed-result fields on stdout; suppress animation. |
| `--no-progress` | Off | Suppress animation, not errors, verification, telemetry or warnings. |
| `--help` | N/A | Show installed command help and exit 0. |

There is no `--max-blob-bytes`, `--platform`, `--part`, `--raw`, payload
conversion or integrity-disable option. Empty blobs still require digest and
declared-size verification. A digest shared by manifest and selected blob counts
once toward the total; otherwise both sizes count. Total-budget manifest
overflow can fail during manifest receipt before selection is possible.
Other strict manifest/descriptor structural limits retain their library defaults.

## Shared download controls

| Option | Default | Contract |
| --- | --- | --- |
| `--concurrency` | `4` | Integer 1..32; per-invocation pooled request ceiling. |
| `--ranges-per-blob` | `1` | Integer 1..concurrency; 1 uses full GET. Eligible range receipt starts at four times range size. |
| `--range-size` | `8388608` | Integer 65536..67108864 bytes; separate from I/O chunk size. |
| `--download-retries` | Unset (normally 3 pooled retries) | Integer 0..10; 0 disables request retries. |
| `--scratch-dir` | Private component staging | Existing caller-trusted root for private bounded range spools. |
| `--sequential` | Off | Pooled concurrency/ranges = 1; conflicts with explicitly supplied concurrency/ranges. Not legacy rollback. |
| `--legacy-transport` | Off | Original serial transport; conflicts with explicit pooled concurrency/ranges, sequential, retry or scratch controls. Range size alone does not opt into pooled scheduling. |

One selected blob uses the existing range/retry engine, shared request-slot
cap, scoped authentication and safe redirects. Scratch is bounded by concurrency
times range size in addition to saved bytes and metadata. No other layers are
acquired. See [policy and fallback details](../../docs/library/downloads.md).

Root `--insecure`, `--verbose`, and `--log-file` precede `config`/`layer`.
Leaf telemetry options are `--time-methods`, `--time-scenarios`, `--debug-calls`
and `--metrics` (all off), `--telemetry-format [text|json]` (text), and
`--telemetry-verbosity/-tv` (integer 0..2, default 1). JSON telemetry suppresses
animation. Keep logs separate from component output, including path/hard-link
aliases. On Linux, ambiguous absent same-parent case-/Unicode-equivalent names
fail closed; use names differing beyond case. Diagnostics can be sensitive.

## Output, publication and process status

Payload bytes go **only** into the named file. No reserialization,
decompression, extraction, execution, embedded `data` fallback or descriptor-URL
fetch occurs. If the registry blob is missing, use
[complete-artifact pull](artifact.md) for embedded content, or publish the blob
to the repository before retrying.

Private staged content is verified, then published by exclusive hard link.
Existing files, directories, symlinks and dangling links are refused; preflight
does not reserve the destination. Linux/macOS, trusted no-symlink ancestors,
compatible native operations and private-namespace trust are mandatory.
See [storage prerequisites and cleanup limits](../../docs/guides/artifact-components.md#before-you-start-output-and-trust).

Stdout is metadata only. Successful text contains:

```text
Downloaded config.
Source: registry.example/repo:moving
Resolved: registry.example/repo@sha256:289d5c9e579d51de30e30b67bcd729297b83d15fefbf4fa19a49d49bb7d885e6
Output: /work/config.bin
Digest: sha256:554dcd9fc9e4136d367a50c7f7e05d77b16b05e478e2df92a335d8853ea39223
Size: 77 bytes
```

This is synthetic fixture metadata with an illustrative absolute output path.
For a layer the first line is `Downloaded layer N.`. `--json` replaces text
with one object containing exactly the following fields; it adds no wrapper:

| JSON field | Type | Meaning |
| --- | --- | --- |
| `source` | string | Formatted input artifact reference; omitted tag becomes `latest`. |
| `resolved_reference` | string | Verified digest-pinned simple-manifest reference. |
| `component` | string | `"config"` or `"layer"`. |
| `layer_index` | integer or null | Zero-based layer position; null for config. |
| `digest` | string | Verified selected-blob digest. |
| `size` | integer | Exact selected-blob bytes, including zero. |
| `media_type` | string | Descriptor media type, not a transformation. |
| `output_path` | string | Absolute published-file path. |

Interactive stderr shows manifest/config/layer progress. `--no-progress`,
non-TTY stderr, `--json`, and JSON telemetry suppress animation. Diagnostics,
warnings and enabled telemetry remain on stderr.

The public hard link is the retained-output boundary, not receipt or staging
handoff. Even verified private bytes are removed after prepublication callback
or teardown failures when cleanup is safe. Complete published files are never
deleted on late failure. Cleanup identity checks do not authorize deleting
replacements, and secondary failures are reported without masking the primary.

| Exit | Meaning |
| --- | --- |
| `0` | Operation, required cleanup, metadata emission and final sink handling completed. |
| `1` | Expected selection, auth/acquisition, integrity, cap, output/storage failure or handled interruption. |
| `2` | CLI usage/validation failure, such as a missing option, `-o -`, invalid source/index or conflicting controls. |

Unexpected callback, renderer, telemetry and I/O failures propagate as failures,
not fabricated success. A result-write error can leave partial metadata; final
stdout flush or deferred-warning failure can leave complete metadata with a
nonzero exit. **Check final process status, not file existence, terminal rows,
or parseable JSON.** Independently verify retained bytes before reuse/retry.
See the [failure recovery table](../../docs/guides/artifact-components.md#4-handle-publication-and-failure).

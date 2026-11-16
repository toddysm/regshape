# Monitor live transfer progress

<!-- doc-type: how-to -->

RegShape displays live progress for complete-artifact pulls, Open Container Initiative (OCI) manifest
downloads, blob downloads and uploads, layout pushes, and Docker pushes. Progress is automatic
on an interactive standard-error terminal; it does not require telemetry flags.

These instructions describe the development implementation targeted to 0.2,
not a promise that the latest published package includes it. Check that
`regshape manifest get --help` lists `--no-progress` before trying the manifest
examples; blob-only progress support does not establish manifest support.

## Before you start

- Use a registry and repository you are authorized to access. Replace
  `registry.example.com/team/example` below with that destination.
- For private registries, authenticate with
  [`regshape auth login`](../../specs/cli/auth.md). Other commands resolve the
  stored credentials automatically.
- Use an empty working directory for the example files.
- Output files can contain sensitive artifact metadata. Standalone manifest/blob
  `--output` can overwrite files, and failed writes are not rolled back.
  Complete `artifact pull` instead requires a new destination; see its
  [distinct storage prerequisites and recovery](artifact-pull.md).
- The shell examples use a Bash-compatible shell and `cmp` for local verification.
- Docker access is needed only for `docker push`; a completed local layout is
  needed only for `layout push`.
- The Docker example assumes a local `example:latest` image with a
  `linux/amd64` variant. Substitute an image and platform you actually have.

Uploads change the destination registry and can incur storage/network charges.
Use a test repository when trying the examples.

## Download a manifest or index

This task saves or inspects **one** manifest/index body while showing its
Hypertext Transfer Protocol (HTTP) response consumption. It does not recursively
fetch an index's child manifests, configuration or layer blobs. For the complete
graph, use the [artifact pull workflow](artifact-pull.md), including
[config-inclusive captured progress](artifact-pull.md#progress-captured-screens-not-an-overall-percentage).
No Docker daemon is required for these registry requests.

### Save and verify the result

Replace this example reference with an artifact you can access, then save parsed
JavaScript Object Notation (JSON). Run in the empty working directory prepared
above:

```bash
ARTIFACT='registry.example.com/team/example:v1'
regshape manifest get --artifact "$ARTIFACT" --output manifest.json &&
  python -m json.tool manifest.json >/dev/null
```

Exit status zero and a successful JSON check establish that the requested local
output completed and is valid JSON. The JSON check does not verify a registry
digest or the existence of referenced content. Inspect the file to confirm it is
the artifact you intended.

`--artifact` and `-a` are equivalent. Legacy `--image-ref` and `-i` remain accepted,
but their help/documentation marks them **deprecated in 0.2**; there is no runtime
deprecation warning or scheduled removal version.

For a digest target, replace **both** the example repository and example digest
below with your real values. The shown digest belongs to a synthetic validation
fixture, not an artifact hosted at the example registry:

```bash
regshape manifest get \
  --artifact registry.example.com/team/example@sha256:db6e5e286a62e721ac5374dbc3a051a3e5e027dade718d4fb7454c630f09c01b \
  --output pinned-manifest.json
```

Tag and digest inputs use the same progress path. No extra HEAD request is
issued solely to discover a size or digest for the display.

### Raw text, field extraction and automation

To bypass model parsing:

```bash
regshape manifest get -a "$ARTIFACT" --raw --output manifest.raw.txt
```

Raw means **decoded response text**, not byte-exact export. Requests' existing
charset/detection/fallback behavior is retained, and the output helper retains
its newline behavior. Neither the raw file nor normalized JSON is guaranteed to
hash to the registry's reported digest. This feature adds no digest verification.

To save one field, choose a field that exists on the selected object:

```bash
regshape manifest get -a "$ARTIFACT" --part annotations --output annotations.json
```

The choices are `config`, `layers`, `subject` and `annotations`. An index has no
config/layers, and optional fields can be absent; those selections fail with
exit code 2 rather than producing a successful empty result. `--raw` and
`--part` cannot be combined. `--accept` controls content negotiation, not a local
format conversion; see the [command reference](../../specs/cli/manifest.md#manifest-get).

For a pipeline, keep producer failures visible and optionally disable animation:

```bash
set -o pipefail
regshape manifest get -a "$ARTIFACT" --no-progress |
  python -m json.tool > manifest.pretty.json
```

Check the pipeline's exit status before using the output. Redirecting stdout
alone does not disable progress on interactive stderr, and ordinary manifest
JSON is not a structured-telemetry suppression signal. `--no-progress` suppresses
the display, not streaming, errors, warnings or explicitly enabled telemetry.

### Read the manifest row

One logical transfer keeps **one row**. A tag can start without a known digest;
row identity does not depend on a digest header. `Index` reflects a recognized
advertised index/list media type; a missing/unknown type displays as `Manifest`.
The label is not a claim that the body has been validated.

The following are separate wide-terminal snapshots of the **same overwritten
row** for a synthetic 256 KiB index. The illustrated rate assumes 64 KiB consumed
in two seconds. Sizes use binary units, including kibibytes (KiB).

Before the response advertises its kind:

```text
Manifest 1 Preparing 0 B (total unknown)
```

During consumption:

```text
Index 1 Downloading 64.0 KiB/256.0 KiB [###---------] 25% 32.0 KiB/s ~6s remaining
```

After consumption, decoding and response cleanup:

```text
Index 1 Received 256.0 KiB/256.0 KiB
```

While parsing or extracting the requested field:

```text
Index 1 Processing 256.0 KiB/256.0 KiB
```

While emitting the requested output:

```text
Index 1 Writing 256.0 KiB/256.0 KiB
```

Only after required output and in-context diagnostics succeed:

```text
Index 1 Downloaded 256.0 KiB/256.0 KiB
```

These are not five appended progress lines. Small/fast bodies may finish before
you notice intermediate states; there is no artificial delay. Raw mode skips
Processing, but still must finish Writing. A full byte counter or Received
label is not final command success or digest verification.

Before stdout output, the row is temporarily cleared/suspended through stderr.
It is then restored below the result, preserving the same logical identity
rather than a fixed physical line. Progress controls never enter stdout or
saved files. The renderer coordinates this command's writes, not unrelated
processes writing the same terminal.

Without a comparable total, only actual consumed payload bytes and a meaningful
rate are shown:

```text
Manifest 1 Downloading 64.0 KiB (total unknown) 32.0 KiB/s
```

Missing/invalid lengths and compressed or transfer-encoded bodies can make the
total unknown. A genuine known-empty body uses zero; an unknown size is not
replaced with zero. If successful consumption disagrees with a comparable size
hint, one warning is logged and the total becomes unknown before an impossible
percentage is displayed. Framing/read failures remain errors, not partial success.

### Failure, interruption and recovery

For an illustrative 2 KiB manifest without a subject, `--part subject` leaves
stdout empty, replaces the same row with Failed and exits 2:

```text
Manifest 1 Failed 2.0 KiB/2.0 KiB
Error [registry.example.com/team/example:v1]: manifest has no subject field
```

Choose a supported/present field, or omit `--part` to inspect the full result.
Invalid JSON/model data can fail after receipt too; use `--raw` only when you
intend to inspect decoded text without model validation.

Interrupting the synthetic index during receipt produces:

```text
Index 1 Cancelled 64.0 KiB/256.0 KiB

Aborted!
```

Retry explicitly after fixing the cause. The command does not resume a partial
manifest. If failure/interruption occurs while writing, some stdout bytes or
file contents can already exist; they are preserved, not erased or atomically
restored. Do not treat file existence as success. Terminal cleanup is best-effort
when the terminal itself is failing, and must not mask the primary error.
Later independent log-file cleanup or shell errors can still fail the process
after artifact output completed; always check the exit status.

| Symptom | Check and next action |
| --- | --- |
| No animation | Check interactive stderr, `--no-progress`, and `--telemetry-format json`. Use `regshape manifest get --help` to confirm this feature is installed. |
| Only the final row is noticeable | The body may fit in one fast read; this is normal and no delay is added. |
| Total unknown or a length warning | Check encoding/length headers if needed; trust consumed bytes, not a guessed percentage. A warning does not suppress later read/parse errors. |
| HTTP/token endpoint 401 | Reauthenticate with a fresh credential if it expired; also check credentials and repository pull permissions. A 401 alone does not prove expiry. Progress does not refresh tokens. |
| Missing part or parse failure | Inspect normal/raw content as appropriate, select a field the object actually has, and retry. Receipt alone was not success. |
| Output/file error | Check the destination and permissions, preserve or remove only your own partial file, then rerun the download. |
| `(empty)` preview or zero debug body size despite received bytes | Streamed diagnostics did not capture the body; these values are not payload-size measurements. |

For diagnostics without changing the result stream:

```bash
regshape --log-file manifest.log manifest get -a "$ARTIFACT" \
  --output manifest.json --debug-calls --time-methods --time-scenarios --metrics
```

The log receives diagnostics, not progress controls. Streamed request previews
remain absent (`body_preview: null`, `body_size: 0`; text uses `(empty)`).
Request timing ends at headers; operation/scenario timing includes consumption
and, for the scenario, local processing/output. Some request byte metrics are
header-based hints and can be zero when size information is unavailable; the
progress counter measures consumed payload bytes.

The command observes streams even with animation disabled. If optional transport
caching is enabled by a library caller, observed fetches bypass cache reads and
writes instead of reusing an exhausted response; buffered no-observer fetches
retain cache reuse. Retry-discarded streams close before backoff. These safety
rules do not add a new retry policy, progress-only probe, or partial-body resume.
See the [callback contract](../library/transfer-progress.md#manifest-receive-observations)
for response ownership and exception behavior.

## Upload and download a blob

Create a small, reproducible payload and its matching digest:

```bash
printf 'hello\n' > blob.bin
DIGEST=sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03
```

Upload it with the default streamed monolithic protocol:

```bash
regshape blob upload --repo registry.example.com/team/example \
  --file blob.bin --digest "$DIGEST"
```

The file is streamed through the existing POST-plus-PUT protocol rather than
buffered entirely for progress. To use the registry's chunked protocol:

```bash
regshape blob upload --repo registry.example.com/team/example \
  --file blob.bin --digest "$DIGEST" --chunked --chunk-size 65536
```

Download and verify the same content:

```bash
regshape blob get --repo registry.example.com/team/example \
  --digest "$DIGEST" --output downloaded.bin
cmp blob.bin downloaded.bin
```

Without `--output`, `blob get` still downloads and verifies the content, then
discards it. Use `blob head` when you only need metadata or an existence check.
The tiny example can finish before intermediate frames are visible.

## Read a progress row

Each blob has **one persistent row**, not a line for every state. Byte updates,
retry, verification, commit, success, failure, and cancellation replace that row.
Shorter states erase old suffixes. Optional fields are removed on narrow
terminals so the active row fits.

The following are separate wide-terminal snapshots of **the same row** for a
larger upload. Sizes use binary units: kibibytes (KiB), mebibytes (MiB), and
gibibytes (GiB).

During transfer:

```text
Blob 1/1 sha256:ab12... Uploading 40.0 MiB/100.0 MiB [####--------] 40% 8.0 MiB/s ~8s remaining
```

After all payload bytes, but before final verification succeeds:

```text
Blob 1/1 sha256:ab12... Verifying 100.0 MiB/100.0 MiB
```

Only after the completing request and digest checks succeed:

```text
Blob 1/1 sha256:ab12... Uploaded 100.0 MiB/100.0 MiB
```

These snapshots are not printed as three successive progress lines. Only the
last state remains when the command advances to another blob. Separate error
and telemetry messages are diagnostics, not additional progress rows.

### What the numbers mean

| Transfer | Counted bytes |
| --- | --- |
| Blob download | Payload chunks actually consumed, including verification-only downloads |
| Monolithic upload | Bytes consumed by the HTTP client from the source; not proof of registry acceptance |
| Chunked upload | Bytes in chunks whose requests succeeded; buffered request retries count once |
| Already present | Zero transferred bytes; a unique skipped blob |

Hypertext Transfer Protocol (HTTP) upload completion requires a final PUT with
`201 Created` and the applicable digest checks. A full byte counter alone is
not success. For blob downloads, `Downloaded` follows digest verification.
Manifest `Downloaded` has the different receipt/output semantics described above.

Rate and remaining-time values are estimates for the current logical attempt,
not measurements of physical network traffic. Routine redraws are limited to
ten per second; lifecycle transitions are not throttled away.

### Unknown and empty sizes

When no trustworthy total exists, RegShape shows consumed bytes and a rate
when meaningful, without inventing a percentage or remaining time:

```text
Blob 1/1 sha256:ab12... Uploading 12.0 MiB (total unknown) 6.0 MiB/s
```

Use `--chunked` for pipe-backed or virtual files. Their metadata can report zero
or an allocation size rather than a payload length. The command checks
regular-file size hints at their boundaries and otherwise uses an unknown
total. Genuinely empty regular files retain a known zero total.

Downloads use `Content-Length` only when it unambiguously describes the
consumed payload. Content/transfer encoding can make the total unknown.
Legacy JSON size metadata is unchanged; do not substitute it for actual
progress observations when headers or file metadata are incomplete.

### Retries

A replayable monolithic upload rewinds its source, resets the logical count
and rate, and increments the attempt on the same row:

```text
Blob 1/1 sha256:ab12... Retrying attempt 2 0 B/100.0 MiB
```

Buffered chunk retries do not double-count accepted bytes and need not produce
a separate retry label. Progress does not enable a new retry policy, resume a
failed upload, or repair authentication or transport errors.

### Failure and cancellation

A transfer error replaces the row with failure; the following diagnostic is
separate. The reference and reason depend on the command and error:

```text
Blob 1/1 sha256:ab12... Failed 40.0 MiB/100.0 MiB
Error [blob.bin]: transfer interrupted
```

An interactive interruption replaces it with cancellation:

```text
Blob 1/1 sha256:ab12... Cancelled 40.0 MiB/100.0 MiB
Aborted!
```

Check the command's exit status. Transfer/preparation/manifest failures prevent
normal success output; a later output or telemetry error can still fail a
command after result data was printed. Progress cleanup is best-effort if
terminal output itself is failing;
it must not replace the original transfer exception. A failed operation does
not undo data already accepted by the registry.

## Push a layout or Docker image

For a completed [local OCI layout](create-oci-layout.md):

```bash
regshape layout push --path ./my-image --dest registry.example.com/team/example:v1
```

For a local Docker image:

```bash
regshape docker push --image example:latest \
  --dest registry.example.com/team/example:v1 --platform linux/amd64
```

Docker export/conversion and layout preparation have an indeterminate
preparation row. Docker's advertised image size is not used as a fabricated
transfer total. The current Docker multi-manifest tag restriction is unchanged;
select a platform for those exports.

An example final screen for two unique blobs:

```text
Preparing Docker image ... done
Blob 1 [1 done/0 skipped] sha256:ab12... Uploaded 100.0 MiB/100.0 MiB
Blob 2 [1 done/1 skipped] sha256:cd34... Already present; skipped
```

`done` counts unique successfully uploaded blobs, and `skipped` counts unique
already-present blobs. Repeated shared layer/config references add neither
rows nor transferred bytes and do not downgrade `Uploaded` to `Skipped`.
There is no invented whole-image byte percentage.

Existing result schemas and summary counters remain compatible. In particular,
legacy skipped counts can include repeated manifest references; live counters
are unique. An uploaded blob is not proof that its manifest push succeeded:
a later manifest failure prevents overall push success.

## Control output and combine telemetry

All five commands accept `--no-progress`. It disables the display, not streamed
consumption, applicable validation/protocol checks, or telemetry:

```bash
regshape blob upload --repo registry.example.com/team/example \
  --file blob.bin --digest "$DIGEST" --no-progress
```

| Output choice | Progress behavior |
| --- | --- |
| Interactive stderr | Automatic animation, unless explicitly disabled |
| Redirected/noninteractive stderr | No animation or per-state progress spam |
| `layout push --json` / `docker push --json` | No animated progress; existing JSON remains on stdout |
| `blob get` / `blob upload` | Results always use stdout JSON; interactive stderr can still show progress |
| `manifest get` | JSON/raw/part results remain on stdout or in `--output`; ordinary JSON does not suppress progress |
| `--telemetry-format json` on supported commands | No animation mixed into structured telemetry |
| Root `--log-file` with telemetry-enabled commands | Logs receive telemetry, not progress rows or terminal controls |

Redirecting stdout alone does not disable blob or manifest stderr progress.
For example:

```bash
regshape layout push --path ./my-image \
  --dest registry.example.com/team/example:v1 --json > result.json
```

Text telemetry clears/redraws the active row rather than duplicating it:

```bash
regshape --log-file transfer.log blob upload \
  --repo registry.example.com/team/example --file blob.bin --digest "$DIGEST" \
  --time-scenarios --metrics
```

Progress works without those flags. This feature does **not** make telemetry
complete for every push/pull:

- Docker push does not gain the common telemetry switches or whole-command
  timing. Its library operations can use an already-active telemetry context.
- Token exchanges and failures before a response still have instrumentation gaps.
- Some metrics rely on length headers, and request timing does not cover all
  subsequent streamed-body consumption.
- Nested scenario metric resets remain separate work.

Progress observations deliberately contain no credentials, URLs, headers,
payload content, or raw exception messages. Telemetry has its own existing
output/redaction behavior.

## Cleanup and next steps

Remove only the example files you created when they are no longer needed.
A registry upload is not automatically deleted, rolled back, or resumed.
For blob downloads, digest mismatch removes invalid output; other failures can
leave a partial file. Manifest output has no new digest check or automatic
rollback; keep only results from a successful invocation. Remove only the
manifest/example files you created when they are no longer needed.

Automated coverage uses deterministic mocked network and Docker boundaries.
It does not establish compatibility with every live registry or daemon.

- [Library transfer observers](../library/transfer-progress.md)
- [Manifest command reference](../../specs/cli/manifest.md#manifest-get)
- [Artifact options and legacy-flag migration](artifact-options.md)
- [Blob command reference](../../specs/cli/blob.md)
- [Layout push reference](../../specs/cli/layout-push.md)
- [Docker integration guide](docker-desktop-integration.md)

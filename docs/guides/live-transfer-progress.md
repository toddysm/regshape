# Monitor live transfer progress

<!-- doc-type: how-to -->

RegShape displays live progress for Open Container Initiative (OCI) blob
downloads and uploads, layout pushes, and Docker pushes. Progress is automatic
on an interactive standard-error terminal; it does not require telemetry flags.

These instructions describe the current development implementation. Check that
`regshape blob upload --help` lists `--no-progress`; a packaged release must
contain this feature before its installation provides these options.

## Before you start

- Use a registry and repository you are authorized to access. Replace
  `registry.example.com/team/example` below with that destination.
- For private registries, authenticate with
  [`regshape auth login`](../../specs/cli/auth.md). Other commands resolve the
  stored credentials automatically.
- Use an empty working directory for the example files.
- The shell examples use a Bash-compatible shell and `cmp` for local verification.
- Docker access is needed only for `docker push`; a completed local layout is
  needed only for `layout push`.
- The Docker example assumes a local `example:latest` image with a
  `linux/amd64` variant. Substitute an image and platform you actually have.

Uploads change the destination registry and can incur storage/network charges.
Use a test repository when trying the examples.

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
| Download | Payload chunks actually consumed, including verification-only downloads |
| Monolithic upload | Bytes consumed by the HTTP client from the source; not proof of registry acceptance |
| Chunked upload | Bytes in chunks whose requests succeeded; buffered request retries count once |
| Already present | Zero transferred bytes; a unique skipped blob |

Hypertext Transfer Protocol (HTTP) upload completion requires a final PUT with
`201 Created` and the applicable digest checks. A full byte counter alone is
not success. The final state is `Downloaded` for a verified download.

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

All four commands accept `--no-progress`. It disables the display, not byte
validation, protocol checks, or telemetry:

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
| `--telemetry-format json` on supported commands | No animation mixed into structured telemetry |
| Root `--log-file` with telemetry-enabled commands | Logs receive telemetry, not progress rows or terminal controls |

Redirecting stdout alone does not disable a blob command's stderr progress.
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
For downloads, digest mismatch removes invalid output; other failures can leave
a partial file, which must not be treated as a successful download.

Automated coverage uses deterministic mocked network and Docker boundaries.
It does not establish compatibility with every live registry or daemon.

- [Library transfer observers](../library/transfer-progress.md)
- [Blob command reference](../../specs/cli/blob.md)
- [Layout push reference](../../specs/cli/layout-push.md)
- [Docker integration guide](docker-desktop-integration.md)

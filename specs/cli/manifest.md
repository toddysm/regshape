# CLI: `manifest`

<!-- doc-type: reference -->

## Overview

The `manifest` command group manages OCI manifests and indexes for supported
images and artifacts. It provides five
subcommands that map to the OCI Distribution Spec endpoints:
`get`, `info`, `descriptor`, `put`, and `delete`.

`manifest get` can also extract individual fields from the parsed manifest
model (`config`, `layers`, `subject`, `annotations`) using the `--part`
option, which is useful for scripting and for verifying individual fields
without having to `jq`-parse the raw JSON.

All requests use the shared `RegistryClient` transport and its authentication
and telemetry support.

## Usage

```
regshape manifest get        [OPTIONS]
regshape manifest info       [OPTIONS]
regshape manifest descriptor [OPTIONS]
regshape manifest put        [OPTIONS]
regshape manifest delete     [OPTIONS]
```

---

## Artifact Reference Format

The preferred `--artifact` / `-a` option accepts the existing registry
reference syntax. This interface targets release 0.2; check installed command
help before using it with an older package.

`--image-ref` / `-i` remains supported with this help/documentation notice:
**Deprecated in 0.2; use --artifact / -a. Retained for compatibility.**
No runtime warning is emitted and no removal version is scheduled. Legacy
spellings remain valid; examples below prefer the new option. Identical repeated values select one target;
differing values fail with usage exit 2 before credential lookup or requests.
See the [migration guide](../../docs/guides/artifact-options.md) and
[artifact option design](artifact-options.md).

| Format | Example |
|---|---|
| `registry/repo:tag` | `acr.io/myrepo/myimage:v1` |
| `registry/repo@digest` | `registry.example.com/team/example@sha256:db6e5e286a62e721ac5374dbc3a051a3e5e027dade718d4fb7454c630f09c01b` |

Repository names and digests here are examples; replace them with accessible
targets. The complete digest above belongs to a synthetic validation fixture.

The registry must always be embedded in the `--artifact` value. If no registry
is present, the command exits with an error.

> **Authentication:** Run `regshape auth login` before using manifest commands
> against authenticated registries. Credentials are resolved automatically from
> the Docker-compatible credential store. Public registries can accept anonymous
> requests; an authenticated registry can reject missing, expired or unauthorized
> credentials. Absence of stored credentials alone does not establish a failure.

---

## Subcommands

### `manifest get`

Fetch the manifest for a given artifact reference.
Manifest progress is a development change targeted to 0.2; check installed
`regshape manifest get --help` for `--no-progress` before relying on it.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--artifact` | `-a` | string | required | Artifact reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`); legacy aliases documented above |
| `--accept` | | string | all OCI+Docker types | Set a specific `Accept` header (overrides the default multi-type header) |
| `--part` | | choice | none | Extract a single field from the parsed model: `config`, `layers`, `subject`, or `annotations` |
| `--output` | `-o` | path | stdout | Write manifest (or extracted part) to this file instead of stdout; overwrites existing files, with no atomic rollback |
| `--raw` | | flag | false | Skip model parsing; print decoded response text with the existing newline behavior (not byte-exact export) |
| `--no-progress` | | flag | false | Disable interactive stderr progress without changing streamed consumption or telemetry |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |
| `--metrics` | | flag | false | Print aggregated request metrics |
| `--telemetry-format` | | choice | text | `text` or `json` telemetry; JSON telemetry suppresses animation, unlike ordinary manifest JSON |

#### Behavior

1. Parse `--artifact` (or a legacy alias) to extract registry, repository, and tag or digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Issue `GET /v2/<repo>/manifests/<reference>` with an `Accept` header that
   covers all known OCI and Docker manifest media types (or the single type
   given via `--accept`).
4. On a non-2xx response, read the error body before tolerant OCI error parsing
   and exit with code 1. Read/framing failures retain their original error.
5. If `--raw` is set, print decoded response text without model parsing.
6. Otherwise, parse the response body with `parse_manifest()` and print the
   canonical JSON. If `--part` is given, extract that field from the model:
   - `config` → the config `Descriptor` as a JSON object
   - `layers` → the layers list as a JSON array
   - `subject` → the subject `Descriptor` as a JSON object (error if absent)
   - `annotations` → the annotations dict as a JSON object (error if absent)
7. If `--output` is given, write to that file; otherwise write to stdout.

The command observes actual response-byte consumption and updates one persistent
`Manifest` or `Index` row on interactive stderr. Unknown/encoded lengths show
bytes without a fabricated percentage or remaining-time estimate. Small responses
add no artificial delay. Receipt is not command success: the same row advances
through optional **Processing** and **Writing**, and becomes **Downloaded** only
after required output and in-context telemetry succeed. Parsing/field/output
failure becomes **Failed**, while interruption becomes **Cancelled**.

Before stdout output, the row is cleared and suspended through stderr, then
restored below the result. Its logical identity is unchanged even if the terminal
position moves. Partial output is not erased or rolled back on failure. File
output retains its UTF-8/newline semantics; progress controls never enter stdout,
saved manifests or log files.

`--no-progress`, nonterminal stderr and structured telemetry suppress animation.
Ordinary manifest JSON on stdout does not suppress it. The command still streams
when animation is off. Request diagnostics therefore retain streaming semantics:
no captured body preview (`body_size: 0` is not actual payload size), and
request timing ends at headers; operation/scenario timing includes consumption.
No extra HEAD is issued for progress, and no new digest-validation guarantee is
introduced. See [library observations](../../docs/library/transfer-progress.md#manifest-receive-observations).

The default `Accept` header contains these types, comma-separated in this order:

```
application/vnd.docker.distribution.manifest.list.v2+json,
application/vnd.docker.distribution.manifest.v2+json,
application/vnd.oci.image.index.v1+json,
application/vnd.oci.image.manifest.v1+json
```

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Requested output and required in-command diagnostics completed |
| 1 | Fetch/auth/transport or model-parsing failure; interruption and unexpected output/setup exceptions also fail the process |
| 2 | Missing/incompatible `--part`, invalid choice or usage, including `--raw` with `--part` and conflicting artifact selectors |

Unexpected exceptions preserve their existing diagnostics rather than being
converted into successful output. A later independent process/log cleanup error
can still produce a nonzero exit after output completed. Check status, not merely
file existence or the received-byte count.

#### Examples

```bash
# Replace the example target and authenticate separately if required.
ARTIFACT='registry.example.com/team/example:v1'

# Print the full manifest for a tag
regshape manifest get -a "$ARTIFACT"

# Print layers for a manifest (not an index)
regshape manifest get -a "$ARTIFACT" --part layers

# Save decoded raw text; replace the example repository AND digest
regshape manifest get \
  -a registry.example.com/team/example@sha256:db6e5e286a62e721ac5374dbc3a051a3e5e027dade718d4fb7454c630f09c01b \
  --raw --output manifest.raw.txt

# Request a specific media type; the server decides what it can return
regshape manifest get -a "$ARTIFACT" \
  --accept application/vnd.oci.image.manifest.v1+json

# Save parsed JSON without animation
regshape manifest get -a "$ARTIFACT" --no-progress --output manifest.json
```

For prerequisites, verification, actual overwritten-row snapshots and recovery,
see [Download a manifest or index](../../docs/guides/live-transfer-progress.md#download-a-manifest-or-index).
This command fetches only the selected object, not its referenced manifests/blobs.

#### Output Format

**Plain text (default):**

Canonical JSON of the full manifest (or extracted part), printed to stdout.
Formatted with 2-space indentation for readability.

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config": {
    "mediaType": "application/vnd.oci.image.config.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2
  },
  "layers": []
}
```

**`--part config` example:**

```json
{
  "mediaType": "application/vnd.oci.image.config.v1+json",
  "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "size": 2
}
```

**`--part layers` example:**

```json
[]
```

#### Error Messages

| Scenario | Message |
|---|---|
| Fetch HTTP/auth/transport failure | `Error [<artifact>]: <reported error details>`; details depend on the failing layer and registry |
| Model parse failure | `Error [<artifact>]: Failed to parse manifest: <details>` |
| `--part subject` absent | `Error [<artifact>]: manifest has no subject field` |
| `--part annotations` absent | `Error [<artifact>]: manifest has no annotations field` |
| `--raw` combined with `--part` | Click usage error: `Error: --raw and --part are mutually exclusive` |

---

### `manifest info`

Check manifest existence and retrieve metadata (digest, media type, size)
without downloading the manifest body.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--artifact` | `-a` | string | required | Artifact reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`); legacy aliases documented above |
| `--accept` | | string | all OCI+Docker types | Set a specific `Accept` header |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--artifact` (or a legacy alias) to extract registry, repository, and tag or digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Issue `HEAD /v2/<repo>/manifests/<reference>` with the `Accept` header.
4. On success, read `Docker-Content-Digest`, `Content-Type`, and
   `Content-Length` from the response headers.
5. Print the metadata.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Manifest exists |
| 1 | Manifest not found, auth failure, or connection error |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Check if a manifest exists
regshape manifest info -a acr.io/myrepo/myimage:latest
```

#### Output Format

**Plain text:**

```
Digest:       sha256:abc123...
Media Type:   application/vnd.oci.image.manifest.v1+json
Size:         1234
```

---

### `manifest descriptor`

Return the OCI Descriptor for a given artifact reference as JSON.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--artifact` | `-a` | string | required | Artifact reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`); legacy aliases documented above |
| `--accept` | | string | all OCI+Docker types | Set a specific `Accept` header |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--artifact` (or a legacy alias) to extract registry, repository, and tag or digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Issue `HEAD /v2/<repo>/manifests/<reference>` with the `Accept` header.
4. On success, read `Docker-Content-Digest`, `Content-Type`, and `Content-Length`
   from the response headers.
5. Print a JSON object using the OCI Descriptor wire-format field names
   (`mediaType`, `digest`, `size`).

The output is a valid OCI Descriptor object and can be embedded directly into
another manifest (e.g. as the `subject` field or an entry in `manifests`).

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Manifest not found, auth failure, or connection error |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Get the descriptor for a tag
regshape manifest descriptor -a acr.io/myrepo/myimage:latest

# Get the descriptor for a specific digest
regshape manifest descriptor -a acr.io/myrepo/myimage@sha256:abc...
```

#### Output Format

```json
{
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "digest": "sha256:abc123...",
  "size": 1234
}
```

---

### `manifest put`

Push a manifest to the registry.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--artifact` | `-a` | string | required | Artifact reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`); legacy aliases documented above |
| `--file` | `-f` | path | none | Read manifest JSON from this file (mutually exclusive with `--stdin`) |
| `--stdin` | | flag | false | Read manifest JSON from stdin (mutually exclusive with `--file`) |
| `--content-type` | | string | inferred | Override the `Content-Type` header (useful for break mode testing) |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

Exactly one of `--file` or `--stdin` must be given.

#### Behavior

1. Parse `--artifact` (or a legacy alias) to extract registry, repository, and tag or digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Read the manifest body from `--file` or stdin.
4. Infer `Content-Type` from the `mediaType` field in the JSON, unless
   `--content-type` overrides it.
5. Issue `PUT /v2/<repo>/manifests/<reference>` with the manifest body.
6. On success, print the `Docker-Content-Digest` from the response.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Manifest pushed successfully |
| 1 | HTTP error or connection error |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Push a manifest from a file
regshape manifest put -a acr.io/myrepo/myimage:v2 --file manifest.json

# Push a manifest from stdin
cat manifest.json | regshape manifest put -a acr.io/myrepo/myimage:v2 --stdin

# Push with overridden Content-Type (break mode: wrong content type)
regshape --break manifest put -a acr.io/myrepo/myimage:v2 \
  --file manifest.json --content-type application/octet-stream
```

#### Output Format

**Plain text:**

```
Pushed: sha256:abc123...
```

---

### `manifest delete`

Delete a manifest. The registry spec requires a digest reference (tags are
not accepted by most registries for delete operations).

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--artifact` | `-a` | string | required | Artifact reference with embedded registry; must be a digest reference (`registry/repo@sha256:...`); legacy aliases documented above |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--artifact` (or a legacy alias) — must contain a digest reference (`@sha256:...`).
   If a tag is given, exit with an error advising the user to use a digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Issue `DELETE /v2/<repo>/manifests/<digest>`.
4. On success, print a confirmation message.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Manifest deleted |
| 1 | HTTP error, auth failure, or connection error |
| 2 | The target value is a tag reference, not a digest |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Delete by digest
regshape manifest delete -a acr.io/myrepo/myimage@sha256:abc...
```

#### Output Format

**Plain text:**

```
Deleted: sha256:abc123...
```

---

## Credential Resolution

Manifest commands do not accept inline credentials. Credentials are resolved
automatically from the Docker credential store populated by `auth login`:

1. Docker credential store (`credHelpers` entry in `~/.docker/config.json`)
2. Base64-encoded `auths` entry in `~/.docker/config.json`
3. Unauthenticated (anonymous)

If authentication is required and no stored credentials exist, the registry
returns HTTP 401, which the command surfaces as exit code 1.

**Always run `regshape auth login` before using manifest commands against
authenticated registries.**

---

## Dependencies

**Internal:**
- `regshape.libs.auth.credentials` — `resolve_credentials`
- `regshape.libs.auth.registryauth` — `authenticate`
- `regshape.libs.models.manifest` — `parse_manifest`, `ImageManifest`, `ImageIndex`
- `regshape.libs.models.mediatype` — `ALL_MANIFEST_MEDIA_TYPES`
- `regshape.libs.decorators` — `telemetry_options`, `track_scenario`, `track_time`
- `regshape.libs.decorators.call_details` — `http_request`
- `regshape.libs.errors` — `ManifestError`

**External:**
- `click` — CLI framework
- `requests` — HTTP (temporary; migrates to `RegistryClient`)

---

## Open Questions

- [ ] Should `manifest get` for an Image Index follow up with individual
      `manifest get` calls for each platform entry (like `docker manifest
      inspect`)? Current proposal: no — return the index as-is and let the
      caller decide.
- [ ] Should `--part` work on Image Index as well (e.g. extract individual
      `manifests` entries by platform)? Current proposal: defer to a
      future `--platform` filter option.

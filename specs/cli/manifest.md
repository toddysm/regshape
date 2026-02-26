# CLI: `manifest`

## Overview

The `manifest` command group manages OCI image manifests. It provides five
subcommands that map to the OCI Distribution Spec endpoints:
`get`, `info`, `descriptor`, `put`, and `delete`.

`manifest get` can also extract individual fields from the parsed manifest
model (`config`, `layers`, `subject`, `annotations`) using the `--part`
option, which is useful for scripting and for verifying individual fields
without having to `jq`-parse the raw JSON.

> **Note:** Until `libs/transport/` is implemented, all commands issue HTTP
> requests directly via the `requests` library using the existing
> `libs/auth/` helpers. Migration to `RegistryClient` is tracked as a
> follow-up task.

## Usage

```
regshape manifest get        [OPTIONS]
regshape manifest info       [OPTIONS]
regshape manifest descriptor [OPTIONS]
regshape manifest put        [OPTIONS]
regshape manifest delete     [OPTIONS]
```

---

## Image Reference Format

The `--image-ref` / `-i` option accepts the standard container image reference syntax:

| Format | Example |
|---|---|
| `registry/repo:tag` | `acr.io/myrepo/myimage:v1` |
| `registry/repo@digest` | `acr.io/myrepo/myimage@sha256:abc...` |

The registry must always be embedded in the `--image-ref` value. If no registry
is present, the command exits with an error.

> **Authentication:** Run `regshape auth login` before using manifest commands
> against authenticated registries. Credentials are resolved automatically from
> the Docker credential store. Commands return exit code 1 with an authentication
> error if no stored credentials exist for the registry.

---

## Subcommands

### `manifest get`

Fetch the manifest for a given image reference.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Image reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`) |
| `--accept` | | string | all OCI+Docker types | Set a specific `Accept` header (overrides the default multi-type header) |
| `--part` | | choice | none | Extract a single field from the parsed model: `config`, `layers`, `subject`, or `annotations` |
| `--output` | `-o` | path | stdout | Write manifest (or extracted part) to this file instead of stdout |
| `--raw` | | flag | false | Skip model parsing; print the raw response body as-is (useful for break mode verification) |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and tag or digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Issue `GET /v2/<repo>/manifests/<reference>` with an `Accept` header that
   covers all known OCI and Docker manifest media types (or the single type
   given via `--accept`).
4. On a non-2xx response, parse the OCI error body and print an error message,
   then exit with code 1.
5. If `--raw` is set, print the response body without further processing.
6. Otherwise, parse the response body with `parse_manifest()` and print the
   canonical JSON. If `--part` is given, extract that field from the model:
   - `config` → the config `Descriptor` as a JSON object
   - `layers` → the layers list as a JSON array
   - `subject` → the subject `Descriptor` as a JSON object (error if absent)
   - `annotations` → the annotations dict as a JSON object (error if absent)
7. If `--output` is given, write to that file; otherwise write to stdout.

The default `Accept` header value sent when `--accept` is not set:

```
application/vnd.oci.image.manifest.v1+json,
application/vnd.oci.image.index.v1+json,
application/vnd.docker.distribution.manifest.v2+json,
application/vnd.docker.distribution.manifest.list.v2+json
```

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | HTTP error (manifest not found, auth failure, connection error) |
| 2 | `--part` requested a field that is absent in the manifest |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Print the full manifest for a tag
regshape manifest get -i acr.io/myrepo/myimage:latest

# Print just the layers
regshape manifest get -i acr.io/myrepo/myimage:latest --part layers

# Save raw manifest JSON to a file
regshape manifest get -i acr.io/myrepo/myimage@sha256:abc... -o manifest.json

# Force a specific Accept header (useful for break mode testing)
regshape manifest get -i acr.io/myrepo/myimage:latest \
  --accept application/vnd.oci.image.manifest.v1+json

# Print raw response body without parsing (break mode verification)
regshape --break manifest get -i acr.io/myrepo/myimage:latest --raw
```

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
    "digest": "sha256:44136fa355ba...",
    "size": 2
  },
  "layers": [ ... ]
}
```

**`--part config` example:**

```json
{
  "mediaType": "application/vnd.oci.image.config.v1+json",
  "digest": "sha256:44136fa355ba...",
  "size": 2
}
```

**`--part layers` example:**

```json
[
  {
    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
    "digest": "sha256:abc...",
    "size": 5678
  }
]
```

#### Error Messages

| Scenario | Message |
|---|---|
| Manifest not found | `Error: manifest not found: <image-ref>` |
| Auth failure | `Error: authentication failed for <registry>` |
| Connection error | `Error: cannot connect to <registry>: <reason>` |
| `--part subject` absent | `Error: manifest has no subject field` |
| `--part annotations` absent | `Error: manifest has no annotations field` |

---

### `manifest info`

Check manifest existence and retrieve metadata (digest, media type, size)
without downloading the manifest body.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Image reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`) |
| `--accept` | | string | all OCI+Docker types | Set a specific `Accept` header |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and tag or digest.
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
regshape manifest info -i acr.io/myrepo/myimage:latest
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

Return the OCI Descriptor for a given image reference as JSON.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Image reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`) |
| `--accept` | | string | all OCI+Docker types | Set a specific `Accept` header |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and tag or digest.
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
regshape manifest descriptor -i acr.io/myrepo/myimage:latest

# Get the descriptor for a specific digest
regshape manifest descriptor -i acr.io/myrepo/myimage@sha256:abc...
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
| `--image-ref` | `-i` | string | required | Image reference with embedded registry (e.g., `registry/repo:tag`, `registry/repo@sha256:...`) |
| `--file` | `-f` | path | none | Read manifest JSON from this file (mutually exclusive with `--stdin`) |
| `--stdin` | | flag | false | Read manifest JSON from stdin (mutually exclusive with `--file`) |
| `--content-type` | | string | inferred | Override the `Content-Type` header (useful for break mode testing) |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

Exactly one of `--file` or `--stdin` must be given.

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and tag or digest.
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
regshape manifest put -i acr.io/myrepo/myimage:v2 --file manifest.json

# Push a manifest from stdin
cat manifest.json | regshape manifest put -i acr.io/myrepo/myimage:v2 --stdin

# Push with overridden Content-Type (break mode: wrong content type)
regshape --break manifest put -i acr.io/myrepo/myimage:v2 \
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
| `--image-ref` | `-i` | string | required | Image reference with embedded registry — must be a digest reference (`registry/repo@sha256:...`) |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` — must contain a digest reference (`@sha256:...`).
   If a tag is given, exit with an error advising the user to use a digest.
2. Resolve credentials from the Docker credential store for the extracted registry.
3. Issue `DELETE /v2/<repo>/manifests/<digest>`.
4. On success, print a confirmation message.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Manifest deleted |
| 1 | HTTP error, auth failure, or connection error |
| 2 | `--image-ref` value is a tag reference, not a digest |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Delete by digest
regshape manifest delete -i acr.io/myrepo/myimage@sha256:abc...
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

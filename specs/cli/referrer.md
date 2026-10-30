# CLI: `referrer`

## Overview

The `referrer` command group discovers OCI referrers (supply-chain artifacts
such as SBOMs, signatures, and attestations) linked to a manifest via the
OCI Referrers API.

| Subcommand | Method | Endpoint |
|---|---|---|
| `list` (tag resolution only) | `HEAD` | `/v2/<name>/manifests/<tag>` |
| `list` | `GET` | `/v2/<name>/referrers/<digest>[?artifactType=<type>]` |

> **Note:** The referrers endpoint is read-only — referrers are created
> implicitly when a manifest with a `subject` field is pushed via
> `manifest put`. No `create` or `delete` subcommand is needed.

---

## Usage

```
regshape referrer list [OPTIONS]
```

---

## Image Reference Format

The `referrer list` subcommand accepts `--image-ref` / `-i` using the standard
container image reference syntax with the registry always embedded. Tags are
resolved to a digest with a manifest `HEAD` request before querying the referrers
API. Digest references are used directly, with no manifest lookup.

| Subcommand | Accepted formats | Notes |
|---|---|---|
| `list` | `registry/repo:tag` | Resolve the tag using `Docker-Content-Digest` |
| `list` | `registry/repo@sha256:...`, `registry/repo@sha512:...` | Use the digest directly |
| `list` | `registry/repo` | Resolve the default tag, `latest` |

> **Authentication:** Run `regshape auth login` before using referrer commands
> against authenticated registries. Credentials are resolved automatically
> from the Docker credential store. Commands return exit code 1 with an
> authentication error if no stored credentials exist for the registry.

---

## Subcommands

### `referrer list`

List referrers for the manifest identified by tag or digest in IMAGE_REF.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Image reference with embedded registry and tag or digest (e.g., `registry/repo:v1.0` or `registry/repo@sha256:abc...`) |
| `--artifact-type` | `-t` | string | none | Filter referrers to this artifact type (e.g., `application/vnd.example.sbom.v1`) |
| `--all` | | flag | false | Follow pagination and return all referrers (default: single page only) |
| `--json` | | flag | false | Output the full `ReferrerList` JSON object instead of one referrer per line |
| `--output` | `-o` | path | stdout | Write output to this file instead of stdout |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and reference. Explicit
   digest references must use `sha256` or `sha512`; unsupported digest algorithms
   are rejected with exit code 2.
2. Resolve credentials from the Docker credential store for the extracted
   registry. For a tag reference, call `head_manifest()` using the same registry
   client and accepting all supported manifest and index media types. Use the
   returned `Docker-Content-Digest` as the subject digest. A missing or blank
   digest header, authentication failure, missing tag, or transport failure
   produces exit code 1 without querying referrers or writing the output file.
3. If `--all` is set, call `list_referrers_all()` which follows `Link`
   pagination headers to retrieve every page and returns a merged
   `ReferrerList`. Otherwise, call `list_referrers()` which returns a
   single page.
4. Parse the response body into a `ReferrerList` model.
5. If `--artifact-type` was provided but the registry did not apply
   server-side filtering (missing `OCI-Filters-Applied` header), the
   operations layer performs client-side filtering transparently.
6. If `--json` is set, print the full Image Index JSON object. Otherwise,
   print one referrer per line in the format:
   ```
   <digest> <artifactType> <size>
   ```
   This tabular format is suitable for scripting with `awk`, `grep`, etc.
7. If `--output` is given, write to that file instead of stdout.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success (including when there are zero referrers) |
| 1 | Reference parsing error, tag resolution failure (including a missing digest header), or registry/transport error |
| 2 | Command usage error or unsupported explicit digest algorithm |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# List referrers by tag (automatically resolved to a digest)
regshape referrer list -i acr.io/myrepo/myimage:v1.0

# Resolve a tag, fetch all pages, filter, and save JSON
regshape referrer list -i acr.io/myrepo/myimage:v1.0 --all \
    --artifact-type application/vnd.example.sbom.v1 --json -o referrers.json

# List all referrers for a manifest by digest (single page)
regshape referrer list -i acr.io/myrepo/myimage@sha256:abc123...

# List all referrers across all pages
regshape referrer list -i acr.io/myrepo/myimage@sha256:abc123... --all

# Filter referrers by artifact type (only SBOMs)
regshape referrer list -i acr.io/myrepo/myimage@sha256:abc123... \
    --artifact-type application/vnd.example.sbom.v1

# Output as JSON
regshape referrer list -i acr.io/myrepo/myimage@sha256:abc123... --json

# Save referrer list to a file
regshape referrer list -i acr.io/myrepo/myimage@sha256:abc123... -o referrers.txt

# Combine with tools — count signatures
regshape referrer list -i acr.io/myrepo/myimage@sha256:abc123... \
    --artifact-type application/vnd.cncf.notary.signature | wc -l
```

---

## Output Format

### Plain text (default)

One referrer per line with digest, artifact type, and size:

```
sha256:a1b2c3d4e5f6... application/vnd.example.sbom.v1 1234
sha256:f6e5d4c3b2a1... application/vnd.cncf.notary.signature 567
```

When no referrers exist, output is empty (no output, exit code 0).

### JSON (`--json`)

Full OCI Image Index response:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:a1b2c3d4e5f6...",
      "size": 1234,
      "artifactType": "application/vnd.example.sbom.v1",
      "annotations": {
        "org.opencontainers.image.created": "2025-06-15T10:30:00Z"
      }
    }
  ]
}
```

---

## Error Messages

| Scenario | Message | Exit Code |
|----------|---------|-----------|
| Unsupported digest algorithm | `Error [<ref>]: digest references must use sha256 or sha512` | 2 |
| Tag HEAD response lacks a digest | `Error [<ref>]: Cannot resolve tag '<tag>'` with detail `HEAD response is missing Docker-Content-Digest` | 1 |
| Auth failure | `Error [<ref>]: Authentication failed for <registry>: <detail>` | 1 |
| Tag not found | `Error [<ref>]: Manifest not found: <registry>/<repo>:<tag>` | 1 |
| Manifest not found | `Error [<ref>]: Manifest not found: <registry>/<repo>@<digest>` | 1 |
| Connection error | `Error [<ref>]: <exception message>` | 1 |

---

## Registration

The `referrer` command group is registered in `src/regshape/cli/main.py`:

```python
from regshape.cli.referrer import referrer

regshape.add_command(referrer)
```

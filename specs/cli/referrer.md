# CLI: `referrer`

## Overview

The `referrer` command group discovers OCI referrers (supply-chain artifacts
such as SBOMs, signatures, and attestations) linked to a manifest via the
OCI Referrers API.

| Subcommand | Method | Endpoint |
|---|---|---|
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
container image reference syntax with the registry always embedded. The
reference **must** include a digest — tag-only references are rejected because
the referrers API requires a digest.

| Subcommand | Accepted formats | Notes |
|---|---|---|
| `list` | `registry/repo@sha256:...` | Digest is required |

> **Authentication:** Run `regshape auth login` before using referrer commands
> against authenticated registries. Credentials are resolved automatically
> from the Docker credential store. Commands return exit code 1 with an
> authentication error if no stored credentials exist for the registry.

---

## Subcommands

### `referrer list`

List all referrers for the manifest identified by digest in IMAGE_REF.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Image reference with embedded registry and digest (e.g., `registry/repo@sha256:abc...`) |
| `--artifact-type` | `-t` | string | none | Filter referrers to this artifact type (e.g., `application/vnd.example.sbom.v1`) |
| `--all` | | flag | false | Follow pagination and return all referrers (default: single page only) |
| `--json` | | flag | false | Output the full `ReferrerList` JSON object instead of one referrer per line |
| `--output` | `-o` | path | stdout | Write output to this file instead of stdout |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and digest. Reject
   tag-only references with exit code 2 and an error message directing the
   user to use a digest reference.
2. Resolve credentials from the Docker credential store for the extracted
   registry.
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
| 1 | HTTP error (manifest not found, auth failure, connection error) |
| 2 | Invalid reference (tag-only reference instead of digest) |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

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
| Tag-only reference | `Error [<ref>]: referrer list requires a digest reference (registry/repo@sha256:...); use 'manifest get' to resolve a tag to a digest` | 2 |
| Auth failure | `Error [<ref>]: Authentication failed for <registry>: <detail>` | 1 |
| Manifest not found | `Error [<ref>]: Manifest not found: <registry>/<repo>@<digest>` | 1 |
| Connection error | `Error [<ref>]: <exception message>` | 1 |

---

## Registration

The `referrer` command group is registered in `src/regshape/cli/main.py`:

```python
from regshape.cli.referrer import referrer

regshape.add_command(referrer)
```

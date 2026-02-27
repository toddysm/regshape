# CLI: `tag`

## Overview

The `tag` command group manages OCI image tags. It provides two subcommands
that map to the OCI Distribution Spec endpoints:

| Subcommand | Method | Endpoint |
|---|---|---|
| `list` | `GET` | `/v2/<name>/tags/list[?n=<n>&last=<last>]` |
| `delete` | `DELETE` | `/v2/<name>/manifests/<tag>` |

Tag creation and movement are implicit side-effects of `manifest put` and are
not exposed here.

> **Note:** Until `libs/transport/` is implemented, all commands issue HTTP
> requests directly via the `requests` library using the existing
> `libs/auth/` helpers. Migration to `RegistryClient` is tracked as a
> follow-up task.

---

## Usage

```
regshape tag list   [OPTIONS]
regshape tag delete [OPTIONS]
```

---

## Image Reference Format

Both subcommands accept `--image-ref` / `-i` using the standard container
image reference syntax with the registry always embedded:

| Subcommand | Accepted formats | Notes |
|---|---|---|
| `list` | `registry/repo` or `registry/repo:tag` | Tag is ignored for list operations |
| `delete` | `registry/repo:tag` | Tag name is required; digest references are rejected |

> **Authentication:** Run `regshape auth login` before using tag commands
> against authenticated registries. Credentials are resolved automatically
> from the Docker credential store. Commands return exit code 1 with an
> authentication error if no stored credentials exist for the registry.

---

## Subcommands

### `tag list`

List all tags for a repository, with optional pagination.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Repository reference with embedded registry (e.g., `registry/repo`, `registry/repo:tag`) |
| `--n` | | integer | none | Maximum number of tags to return per page |
| `--last` | | string | none | Return tags lexicographically after this value (pagination cursor) |
| `--json` | | flag | false | Output the raw `TagList` JSON object instead of one tag per line |
| `--output` | `-o` | path | stdout | Write output to this file instead of stdout |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry and repository (tag or digest is
   ignored if present).
2. Resolve credentials from the Docker credential store for the extracted
   registry.
3. Issue `GET /v2/<name>/tags/list`, appending `?n=<n>` and/or
   `?last=<last>` when the respective options are provided.
4. Parse the response body into a `TagList` model.
5. If `--json` is set, print the JSON object. Otherwise, print one tag per
   line (suitable for scripting with `xargs` or other tools).
6. If `--output` is given, write to that file instead of stdout.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | HTTP error (repo not found, auth failure, connection error) |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# List all tags for a repository
regshape tag list -i acr.io/myrepo/myimage

# List with pagination (first 10 tags)
regshape tag list -i acr.io/myrepo/myimage --n 10

# List tags after v1.5 (pagination cursor)
regshape tag list -i acr.io/myrepo/myimage --n 10 --last v1.5

# Output as JSON
regshape tag list -i acr.io/myrepo/myimage --json

# Save tag list to a file
regshape tag list -i acr.io/myrepo/myimage -o tags.txt
```

#### Output Format

**Default (one tag per line):**

```
latest
v1.0
v1.1
v2.0
```

**`--json`:**

```json
{
  "name": "myrepo/myimage",
  "tags": ["latest", "v1.0", "v1.1", "v2.0"]
}
```

#### Error Messages

| Scenario | Message |
|---|---|
| Repository not found | `Error [acr.io/myrepo/myimage]: Manifest not found: ...` |
| Auth failure | `Error [acr.io/myrepo/myimage]: Authentication failed for acr.io` |
| Connection error | `Error [acr.io/myrepo/myimage]: <transport error>` |

---

### `tag delete`

Delete a tag from a repository.

> **Note:** The OCI Distribution Spec routes tag deletion through the manifest
> endpoint (`DELETE /v2/<name>/manifests/<tag>`). Deleting a tag does **not**
> delete the underlying manifest, which remains addressable by digest.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Image reference with embedded registry and tag (e.g., `registry/repo:tag`) |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Parse `--image-ref` to extract registry, repository, and tag.
2. Reject digest references (`@sha256:...`) with exit code 2 — deleting by
   digest targets the manifest, not the tag.
3. Resolve credentials from the Docker credential store for the extracted
   registry.
4. Issue `DELETE /v2/<name>/manifests/<tag>`.
5. On `202 Accepted`, print a confirmation and exit 0.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Tag deleted |
| 1 | HTTP error (tag not found, auth failure, connection error) |
| 2 | `--image-ref` is a digest reference (use `manifest delete` instead) |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# Delete a tag
regshape tag delete -i acr.io/myrepo/myimage:v1.0
```

#### Output Format

```
Deleted tag: acr.io/myrepo/myimage:v1.0
```

#### Error Messages

| Scenario | Message |
|---|---|
| Tag not found | `Error [acr.io/myrepo/myimage:v1.0]: Tag not found: ...` |
| Digest reference supplied | `Error [acr.io/myrepo/myimage@sha256:...]: tag delete requires a tag reference; use manifest delete for digest references` |
| Auth failure | `Error [acr.io/myrepo/myimage:v1.0]: Authentication failed for acr.io` |
| Tag deletion disabled | `Error [acr.io/myrepo/myimage:v1.0]: Tag deletion is not supported by this registry` |

---

## OCI Spec References

- **List Tags** `GET /v2/<name>/tags/list` — end-8a / end-8b. Supports
  `?n=` and `?last=` query parameters for pagination. Response body MUST
  contain `"name"` and `"tags"` keys.
- **Delete Tag** `DELETE /v2/<name>/manifests/<tag>` — end-9. Returns
  `202 Accepted` on success. A registry MAY return `405 Method Not Allowed`
  or `400 Bad Request` if tag deletion is disabled.

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

All requests use the shared `RegistryClient` transport and its authentication
and telemetry support.

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

List one page of tags, or opt into automatic traversal of all remaining pages.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--image-ref` | `-i` | string | required | Repository reference with embedded registry (e.g., `registry/repo`, `registry/repo:tag`) |
| `--n` | | integer | none; 100 with `--all` | Tags requested per page; must be positive with `--all` |
| `--last` | | string | none | Return tags lexicographically after this value (pagination cursor) |
| `--all` | | flag | false | Retrieve and aggregate every remaining page after the optional cursor |
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
3. Without `--all`, issue one `GET /v2/<name>/tags/list`, appending `?n=<n>`
   and/or `?last=<last>` when provided. Existing single-page behavior, including
   `--n 0`, is unchanged.
   With `--all`, call `list_tags_all()` with a positive page size (default 100)
   and the optional initial tag cursor. Follow next links when present;
   otherwise continue full pages using the last returned tag value.
4. Parse the response body into a `TagList` model.
5. If `--json` is set, print the JSON object. Otherwise, print one tag per
   line (suitable for scripting with `xargs` or other tools).
6. If `--output` is given, write to that file instead of stdout. All-pages
   results are emitted only after the entire traversal succeeds; a later-page
   failure does not print a successful partial list or overwrite an output file.
   Repeated tags, including partial overlap between pages, fail with exit code 1
   rather than producing duplicates or silently deduplicating the response.
   A page that includes an excluded `--last` or server-provided cursor tag also
   fails instead of returning that tag.
   If concurrent repository changes cause overlap, retry once the listing is
   stable.

Pagination follows [OCI Distribution Specification v1.1.1, Listing
Tags](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#listing-tags).
`Link` is optional; when present, its `next` target takes precedence even for
short/empty pages. Without a next link, a full page advances using an exclusive
tag-value cursor. The effective page size can change in a server-provided link.
See the [library operation](../operations/tags.md#list_tags_all) for safety
checks and stopping rules. The 100-tag default is a client policy, not an OCI
requirement, and `--n` does not limit the total size of an all-pages result.
Registry and continuation URL authorities must use ASCII spelling; use the
punycode hostname for an international domain.

As a client safety policy, `--all` fails on the **tenth consecutive empty page
with a next link**, even when continuation links change. Receiving tags resets
the counter; an empty page without a next link completes normally. This limit
is not an OCI requirement or a cap on total pages. Failure exits with code 1,
prints no partial list, and leaves output files untouched.

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Registry/transport error, invalid response, or unsafe/non-progressing pagination |
| 2 | Usage error, including non-positive `--n` with `--all` |

#### Examples

```bash
# Log in first
regshape auth login -r acr.io -u alice

# List one page using registry defaults
regshape tag list -i acr.io/myrepo/myimage

# Retrieve every tag (100 requested per page by default)
regshape tag list -i acr.io/myrepo/myimage --all

# Retrieve all remaining tags after v1.5, requesting 10 per page
regshape tag list -i acr.io/myrepo/myimage --all --n 10 --last v1.5

# List with pagination (first 10 tags)
regshape tag list -i acr.io/myrepo/myimage --n 10

# List tags after v1.5 (pagination cursor)
regshape tag list -i acr.io/myrepo/myimage --n 10 --last v1.5

# Output as JSON
regshape tag list -i acr.io/myrepo/myimage --json

# Save tag list to a file
regshape tag list -i acr.io/myrepo/myimage -o tags.txt

# Save the complete aggregated response
regshape tag list -i acr.io/myrepo/myimage --all --json -o tags.json
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
| Repository not found | `Error [acr.io/myrepo/myimage]: Registry error: repository not found` |
| Auth failure | `Error [acr.io/myrepo/myimage]: Authentication failed for acr.io` |
| Connection error | `Error [acr.io/myrepo/myimage]: <transport error>` |
| Invalid all-pages size | Usage error: `--n` must be positive with `--all` |
| Unsafe next link | `Error [<ref>]: Unsafe or invalid tag pagination URL: <detail>` |
| Repeated page/cursor | `Error [<ref>]: Tag pagination repeated a page/cursor for <registry>/<repo>` |
| Empty-page limit | `Error [<ref>]: Tag pagination returned 10 consecutive empty linked pages` |

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

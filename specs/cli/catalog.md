# CLI: `catalog`

## Overview

The `catalog` command group lists repositories hosted on an OCI registry.
It provides a single subcommand:

| Subcommand | Method | Endpoint |
|---|---|---|
| `list` | `GET` | `/v2/_catalog[?n=<n>&last=<last>]` |

The catalog endpoint is registry-scoped, not repository-scoped. The input
is a bare registry hostname, not an image reference.

> **Note:** The catalog endpoint is non-standard (absent from the OCI
> Distribution Spec v1.1 normative table) but widely implemented. Registries
> that do not support it return `404` or `405`; this is surfaced to the user
> as a specific "not supported" exit code (see Exit Codes below) so callers
> can distinguish it from other errors.

---

## Usage

```
regshape catalog list [OPTIONS]
```

---

## Registration in `main.py`

```python
from regshape.cli.catalog import catalog
regshape.add_command(catalog)
```

`catalog` is added alongside `auth`, `blob`, `manifest`, and `tag`.

---

## Module Structure

```
src/regshape/cli/
└── catalog.py   # NEW: Click command group + catalog list subcommand
```

---

## Subcommands

### `catalog list`

List repositories hosted on a registry, with optional pagination.

#### Options

| Option | Short | Type | Default | Description |
|---|---|---|---|---|
| `--registry` | `-r` | string | required | Registry hostname (e.g. `registry.example.com` or `acr.io`) |
| `--n` | | integer | none | Maximum number of repositories to return per page (OCI `n` parameter) |
| `--last` | | string | none | Return repositories after this value — pagination cursor (OCI `last` parameter) |
| `--all` | | flag | false | Fetch all pages and return the merged result; incompatible with `--last` |
| `--json` | | flag | false | Output the full `RepositoryCatalog` JSON object instead of one repository per line |
| `--output` | `-o` | path | stdout | Write output to this file instead of stdout |
| `--time-methods` | | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | | flag | false | Print request/response headers for each HTTP call |

#### Behavior

1. Validate options: if both `--all` and `--last` are supplied, print an
   error and exit `2`.
2. Construct a `RegistryClient` for `--registry` using `TransportConfig`.
3. If `--all` is set, call `list_catalog_all(client, page_size=n)`.
   Otherwise call `list_catalog(client, page_size=n, last=last)`.
4. If `--json` is set, output `json.dumps(catalog.to_dict(), indent=2)`.
   Otherwise print one repository name per line.
5. If `--output` is given, write to that file instead of stdout.

**Credentials** are resolved automatically from the Docker credential store
(same as all other commands). Run `regshape auth login -r <registry>` first
for authenticated registries.

#### Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | HTTP error (auth failure, transport error, or other non-2xx) |
| `2` | Usage error (`--all` and `--last` supplied together) |
| `3` | Registry does not support the catalog API (`404` / `405`) |

Exit code `3` is specific to catalog so that scripts can distinguish
"endpoint not available" from a general request failure without parsing
error message text.

#### Examples

```bash
# Log in first (if the registry requires authentication)
regshape auth login -r acr.io -u alice

# List all repositories (single page, registry default page size)
regshape catalog list -r acr.io

# Limit to first 50 repositories
regshape catalog list -r acr.io --n 50

# Paginate manually (second page starting after myrepo/other)
regshape catalog list -r acr.io --n 50 --last myrepo/other

# Fetch ALL repositories across all pages
regshape catalog list -r acr.io --all

# Output as JSON
regshape catalog list -r acr.io --json

# Save to a file
regshape catalog list -r acr.io --all -o repos.txt
```

#### Output Format

**Default (one repository per line):**

```
library/ubuntu
myrepo/myimage
myrepo/other
```

**`--json`:**

```json
{
  "repositories": [
    "library/ubuntu",
    "myrepo/myimage",
    "myrepo/other"
  ]
}
```

#### Error Messages

| Scenario | Exit | Message |
|---|---|---|
| `--all` and `--last` both set | `2` | `Error [acr.io]: --all and --last are mutually exclusive` |
| Registry does not support catalog | `3` | `Error [acr.io]: Registry does not support the catalog API` |
| Auth failure | `1` | `Error [acr.io]: Authentication failed for acr.io` |
| Connection error | `1` | `Error [acr.io]: <transport error>` |
| Other registry error | `1` | `Error [acr.io]: <error message>` |

Error format: `Error [<registry>]: <message>` — consistent with the pattern
used by `tag`, `blob`, and `manifest` command groups.

---

## Exception-to-Exit-Code Mapping

| Exception | Exit code |
|---|---|
| `CatalogNotSupportedError` | `3` |
| `AuthError` | `1` |
| `CatalogError` | `1` |
| `requests.exceptions.RequestException` | `1` |

`CatalogNotSupportedError` is caught first (it is a subclass of
`CatalogError`), then the broader `CatalogError`, then transport exceptions.

---

## Implementation Skeleton

```python
@catalog.command("list")
@telemetry_options
@click.option("--registry", "-r", required=True, ...)
@click.option("--n", "page_size", type=int, default=None, ...)
@click.option("--last", default=None, ...)
@click.option("--all", "fetch_all", is_flag=True, default=False, ...)
@click.option("--json", "as_json", is_flag=True, default=False, ...)
@click.option("--output", "-o", type=click.Path(), default=None, ...)
@click.pass_context
@track_scenario("catalog list")
def catalog_list(ctx, registry, page_size, last, fetch_all, as_json, output):
    if fetch_all and last:
        _error(registry, "--all and --last are mutually exclusive")
        sys.exit(2)

    insecure = ctx.obj.get("insecure", False) if ctx.obj else False
    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        if fetch_all:
            result = list_catalog_all(client, page_size=page_size)
        else:
            result = list_catalog(client, page_size=page_size, last=last)
    except CatalogNotSupportedError as exc:
        _error(registry, str(exc))
        sys.exit(3)
    except (AuthError, CatalogError, requests.exceptions.RequestException) as exc:
        _error(registry, str(exc))
        sys.exit(1)

    if as_json:
        _write(output, json.dumps(result.to_dict(), indent=2))
    else:
        _write(output, "\n".join(result.repositories))
```

---

## OCI Spec References

- **Listing repositories**: `GET /v2/_catalog` — non-normative, widely
  implemented. Supports `?n=<count>&last=<last-repo>` pagination with
  `Link` response header for the next page.

---

## Dependencies

**Internal:**
- `regshape.libs.catalog` — `list_catalog`, `list_catalog_all`
- `regshape.libs.errors` — `AuthError`, `CatalogError`, `CatalogNotSupportedError`
- `regshape.libs.transport` — `RegistryClient`, `TransportConfig`
- `regshape.libs.decorators` — `telemetry_options`
- `regshape.libs.decorators.scenario` — `track_scenario`

**External:**
- `click`, `json`, `sys`, `requests`

# CLI: `auth`

## Overview

The `auth` command group manages credentials for OCI registries. It provides
`login` and `logout` subcommands that persist credentials locally (via the
Docker credential store or `~/.docker/config.json`) and verify them against
the registry before storing.

All other `regshape` commands resolve credentials automatically using the
**credential resolution chain** (see [Credential Resolution](#credential-resolution)).

## Usage

```
regshape auth login  [OPTIONS] <registry>
regshape auth logout [OPTIONS] <registry>
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `login`    | Authenticate against a registry and persist credentials |
| `logout`   | Remove persisted credentials for a registry |

---

## `auth login`

### Arguments

| Argument     | Required | Description                     |
|--------------|----------|---------------------------------|
| `<registry>` | Yes      | Registry hostname (e.g., `registry.example.com`) |

### Options

| Option              | Short | Type   | Default        | Description |
|---------------------|-------|--------|----------------|-------------|
| `--username`        | `-u`  | string | prompt         | Username (overrides global `--username`) |
| `--password`        | `-p`  | string | prompt (hidden)| Password (overrides global `--password`) |
| `--password-stdin`  |       | flag   | false          | Read password from stdin instead of prompting |
| `--docker-config`   |       | path   | none           | Alternate Docker config file path |

### Behavior

1. Resolve credentials from flags (or prompt interactively if omitted).
2. Issue `GET /v2/` via `RegistryClient` (so `AuthMiddleware` handles the
   full Bearer challenge/401-retry cycle automatically — required for Docker Hub
   and other token-based registries).
3. If the final response is `200` or `401` on retry, treat as credential failure.
4. On success, persist credentials:
   - If the registry has a `credHelpers` entry in `~/.docker/config.json`,
     use `dockercredstore.store()`.
   - Otherwise, write Base64-encoded `username:password` into the `auths`
     section of `~/.docker/config.json`.

### Exit Codes

| Code | Meaning |
|------|---------|
| 0    | Login successful |
| 1    | Authentication failed (wrong credentials or registry unreachable) |

### Examples

```bash
# Interactive prompt for credentials
regshape auth login registry.example.com

# Explicit credentials
regshape auth login -u alice -p s3cr3t registry.example.com

# Read password from stdin (e.g., from a secrets manager)
echo "$MY_TOKEN" | regshape auth login -u alice --password-stdin registry.example.com

# JSON output
regshape --json auth login -u alice -p s3cr3t registry.example.com
```

### Output Format

#### Plain text (default)

```
Login succeeded.
```

#### JSON (`--json`)

```json
{"status": "success", "registry": "registry.example.com"}
```

#### Error (plain text)

```
Error: Login failed for registry.example.com: <reason>
```

---

## `auth logout`

### Arguments

| Argument     | Required | Description |
|--------------|----------|-------------|
| `<registry>` | Yes      | Registry hostname |

### Options

| Option            | Short | Type | Default | Description |
|-------------------|-------|------|---------|-------------|
| `--docker-config` |       | path | none    | Alternate Docker config file path |

### Behavior

1. If the registry has a `credHelpers` entry in `~/.docker/config.json`,
   call `dockercredstore.erase()`.
2. Otherwise, remove the registry's entry from the `auths` section of
   `~/.docker/config.json`.
3. If no credentials are found for the registry, exit `0` with an
   informational message (idempotent).

### Exit Codes

| Code | Meaning |
|------|---------|
| 0    | Logout successful (or no credentials to remove) |
| 1    | Error removing credentials |

### Examples

```bash
regshape auth logout registry.example.com
regshape --json auth logout registry.example.com
```

### Output Format

#### Plain text (default)

```
Removing login credentials for registry.example.com.
```

or, if no stored credentials:

```
Not logged in to registry.example.com.
```

#### JSON (`--json`)

```json
{"status": "success", "registry": "registry.example.com"}
```

---

## Credential Resolution

All `regshape` commands (not just `auth`) resolve credentials using the
following priority chain implemented in `libs/auth/credentials.resolve_credentials()`:

| Priority | Source | Notes |
|----------|--------|-------|
| 1 | `--username` / `--password` global flags | Explicit always wins |
| 2 | Docker `credHelpers` for the registry | `dockercredstore.get()` |
| 3 | `~/.docker/config.json` `auths` section | `dockerconfig.load_config()` |
| 4 | Anonymous | `username=None, password=None` |

Anonymous credentials still work with registries such as Docker Hub because
`AuthMiddleware` completes the Bearer challenge exchange without credentials,
which those registries accept for public repositories.

---

## Implementation

| File | Role |
|------|------|
| `src/regshape/libs/auth/credentials.py` | `resolve_credentials()` helper |
| `src/regshape/cli/auth.py` | Click `auth` group + `login` / `logout` commands |
| `src/regshape/cli/main.py` | Top-level group; registers `auth` group; calls `resolve_credentials()` |

---

## Error Messages

| Scenario | Message |
|----------|---------|
| Wrong credentials | `Error: Login failed for <registry>: authentication rejected` |
| Registry unreachable | `Error: Login failed for <registry>: <connection error>` |
| No cred helper found | Falls back to docker config file silently |
| Credential store error | `Error: Could not store credentials: <reason>` |

---

## Open Questions

- [ ] Should `login` validate the token scope (e.g., `push`/`pull`) or just `/v2/` reachability?
- [ ] Should `auth status <registry>` be added to display current credential source?

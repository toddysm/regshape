# CLI: `ping`

## Overview

Implements a top-level `regshape ping` command that performs `GET /v2/` against a target registry to verify connectivity, authentication, and OCI Distribution API support. This is a connectivity check command — it does not operate on repositories or images.

## Usage

```
regshape ping --registry <registry> [--json]
```

## Arguments

None.

## Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--registry` | `-r` | string | required | Registry hostname (e.g. `ghcr.io`, `localhost:5000`) |
| `--json` | | flag | false | Output as JSON |

## Library Layer

### Module: `src/regshape/libs/ping/`

#### `ping(client: RegistryClient) -> PingResult`

Issues `GET /v2/` via the provided `RegistryClient` and returns a `PingResult` describing the outcome.

**Parameters:**
- `client` — A configured `RegistryClient` instance targeting the registry.

**Returns:** `PingResult` dataclass.

**Raises:**
- `AuthError` — When the registry returns `401 Unauthorized`.
- `PingError` — When the registry is unreachable (DNS failure, connection refused, timeout).

#### `PingResult` dataclass

```python
@dataclasses.dataclass
class PingResult:
    reachable: bool          # True if HTTP 200
    status_code: int         # HTTP status code returned
    api_version: str | None  # Docker-Distribution-API-Version header value
    latency_ms: float        # Round-trip time in milliseconds

    def to_dict(self) -> dict: ...
```

### Error Class: `PingError`

Added to `src/regshape/libs/errors.py`:

```python
class PingError(RegShapeError):
    """Error caused by a failed registry ping (connection, DNS, timeout)."""
    pass
```

## Protocol Flow

```
Client                          Registry
  |                                |
  |--- GET /v2/  ----------------->|
  |<-- 200 OK + headers ----------|
```

The `GET /v2/` endpoint is the OCI Distribution Spec API version check. A `200 OK` response confirms the registry is reachable and speaks the Distribution API. The response may include a `Docker-Distribution-API-Version` header (typically `registry/2.0`).

### Behavior by HTTP Status

| Status | Interpretation | Result |
|--------|---------------|--------|
| `200` | Registry reachable and authenticated | `PingResult(reachable=True, ...)` |
| `401` | Authentication required or failed | Raises `AuthError` |
| Other 4xx/5xx | Registry responded but endpoint unsupported | `PingResult(reachable=False, ...)` |
| Connection error / timeout | Registry unreachable | Raises `PingError` |

### Latency Measurement

Round-trip latency is measured using `time.monotonic()` around the `client.get()` call and reported in milliseconds.

## CLI Layer

### File: `src/regshape/cli/ping.py`

A **top-level command** (not a command group), registered directly on the `regshape` group in `main.py`.

**Implementation pattern** (follows `tag.py`):
- Creates `RegistryClient(TransportConfig(registry=registry, insecure=insecure))`
- Calls `ping(client)` from the operations module
- Catches `(AuthError, PingError, requests.exceptions.RequestException)`
- Uses `@telemetry_options` and `@track_scenario("ping")` decorators

### Registration in `main.py`

```python
from regshape.cli.ping import ping
regshape.add_command(ping)
```

## Examples

```bash
# Basic ping
regshape ping --registry ghcr.io

# Ping with JSON output
regshape ping --registry ghcr.io --json

# Ping an insecure (HTTP) registry
regshape --insecure ping --registry localhost:5000
```

## Output Format

### Plain text (default)

Success:

```
Registry ghcr.io is reachable
  API Version: registry/2.0
  Latency:     42ms
```

Failure (non-200 response):

```
Error: Registry ghcr.io is not reachable (HTTP 503)
```

Failure (connection error):

```
Error: Registry ghcr.io is not reachable: Connection refused
```

Failure (auth error):

```
Error: Registry ghcr.io requires authentication: 401 Unauthorized
```

### JSON (`--json`)

Success:

```json
{
  "registry": "ghcr.io",
  "reachable": true,
  "status_code": 200,
  "api_version": "registry/2.0",
  "latency_ms": 42.3
}
```

Failure:

```json
{
  "registry": "ghcr.io",
  "reachable": false,
  "status_code": 503,
  "api_version": null,
  "latency_ms": 150.7
}
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Registry is reachable (HTTP 200) |
| 1 | Registry is not reachable or error occurred |

## Error Messages

| Scenario | Message |
|----------|---------|
| Connection refused | `Error: Registry <host> is not reachable: Connection refused` |
| DNS resolution failed | `Error: Registry <host> is not reachable: Name resolution failed` |
| Timeout | `Error: Registry <host> is not reachable: Connection timed out` |
| 401 Unauthorized | `Error: Registry <host> requires authentication: 401 Unauthorized` |
| Non-200 HTTP status | `Error: Registry <host> is not reachable (HTTP <code>)` |

## Files to Create/Modify

| File | Action |
|------|--------|
| `src/regshape/libs/ping/__init__.py` | Create — re-exports `ping` and `PingResult` |
| `src/regshape/libs/ping/operations.py` | Create — `ping()` function and `PingResult` dataclass |
| `src/regshape/libs/errors.py` | Modify — add `PingError` |
| `src/regshape/cli/ping.py` | Create — Click command |
| `src/regshape/cli/main.py` | Modify — import and register `ping` command |
| `src/regshape/tests/test_ping_operations.py` | Create — operations layer tests |
| `src/regshape/tests/test_ping_cli.py` | Create — CLI layer tests |

## Test Plan

### Operations tests (`test_ping_operations.py`)

- Mock `RegistryClient.get()` returning 200 with `Docker-Distribution-API-Version` header → `PingResult(reachable=True, api_version="registry/2.0")`
- Mock returning 200 without `Docker-Distribution-API-Version` header → `PingResult(reachable=True, api_version=None)`
- Mock returning non-200 status (e.g. 503) → `PingResult(reachable=False, status_code=503)`
- Mock raising `AuthError` (401) → propagated to caller
- Mock raising `ConnectionError` → `PingError`
- Mock raising `Timeout` → `PingError`
- Verify latency is measured (non-negative value)

### CLI tests (`test_ping_cli.py`)

- Successful ping → exit code 0, plain text contains "is reachable"
- Successful ping with `--json` → exit code 0, valid JSON with expected keys
- Unreachable registry → exit code 1, error message
- Auth failure → exit code 1, auth error message
- `--registry` option is required → exit code 2 (Click usage error)

## Dependencies

- Internal: `regshape.libs.transport` (`RegistryClient`, `TransportConfig`), `regshape.libs.errors`, `regshape.libs.decorators`
- External: `requests`, `click`

## Open Questions

- [ ] Should `--registry` also accept a full image reference (and extract the registry), or strictly a hostname? Current design uses hostname-only since this is a connectivity check, not a repository operation.

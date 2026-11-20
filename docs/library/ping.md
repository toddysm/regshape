# Registry ping SDK

<!-- doc-type: reference -->

The `regshape.libs.ping` module exports `ping` and `PingResult`. It checks
`GET /v2/` using a caller-configured `RegistryClient`; it does not print output
or depend on Click. This reference describes the development API, not release
availability.

## `ping`

```python
def ping(client: RegistryClient, *, raise_on_auth: bool = True) -> PingResult: ...
```

The default preserves existing SDK behavior: HTTP 200 returns a reachable
result, HTTP 401 or a transport `AuthError` raises `AuthError`, and other HTTP
responses (including a raw 403) return `reachable=False`.

Pass `raise_on_auth=False` to receive authentication outcomes as data:

| Outcome | Result |
| --- | --- |
| HTTP 200 | `reachable=True`, `auth_required=False` |
| HTTP 401/403 response | `reachable=True`, `auth_required=True`; actual response status, API-version header and measured latency |
| Transport raises `AuthError` | `reachable=True`, `auth_required=True`; status, API version and latency are `None`, original diagnostic in `error` |
| Other HTTP response | `reachable=False`, `auth_required=False`; response status and measured latency |
| Connection, DNS, timeout or other Requests transport failure | Raises `PingError` in either mode |

Authentication-required means the authentication check requires attention,
not that the credentials grant access. A token-server error's status is not
necessarily the registry's status, so the SDK never parses exception text to
invent response metadata.

```python
from regshape.libs.errors import PingError
from regshape.libs.ping import ping
from regshape.libs.transport import RegistryClient, TransportConfig

client = RegistryClient(TransportConfig(registry="registry.example.com"))
try:
    result = ping(client, raise_on_auth=False)
except PingError as error:
    print(error.detail)
else:
    if result.auth_required:
        print("Authentication required:", result.error)
    elif result.reachable:
        print("Registry API available:", result.api_version)
    else:
        print("Unexpected HTTP status:", result.status_code)
```

## `PingResult`

The first four constructor arguments retain their order. New authentication
fields are optional and default to `False` and `None`.

| Field | Type | Meaning |
| --- | --- | --- |
| `reachable` | `bool` | HTTP 200 or an authentication-required outcome |
| `status_code` | `int \| None` | Response status, or unknown after a transport authentication exception |
| `api_version` | `str \| None` | `Docker-Distribution-API-Version` header, if known |
| `latency_ms` | `float \| None` | Round-trip milliseconds rounded to one decimal, or unknown without a response |
| `auth_required` | `bool` | Authentication is required or failed |
| `error` | `str \| None` | Original formatted authentication diagnostic; `None` for ordinary HTTP results |

`to_dict()` includes all six SDK fields, including null values. The
`auth_required` and `error` keys are additive to the original SDK dictionary.
This SDK dictionary is **not** the CLI JSON contract.

`PingResult.from_auth_error(error, *, status_code=None, api_version=None,
latency_ms=None)` converts a legacy `AuthError` into the same result state.
Supply metadata only when known independently of the exception's text.

## `PingError.detail`

`PingError` is exported from `regshape.libs.errors`. Its `detail` property
returns the direct chained cause's text when present, otherwise the existing
formatted error text. Both the original exception chain and `str(error)` are
preserved. The SDK and CLI therefore use the same transport diagnostic.

## CLI compatibility

The [ping command](../../specs/cli/ping.md) opts in to result-based authentication
handling. Existing CLI text, JSON keys and exit codes remain unchanged. Auth
JSON deliberately omits `status_code` and `auth_required`, and renders API
version and latency as null even when the SDK has response metadata.

A raw HTTP 403 response retains the legacy CLI failure contract: exit code 1,
`Error: Registry <host> is not reachable: HTTP 403` in text mode, or
`{"registry": "<host>", "reachable": false, "error": "HTTP 403"}` in JSON mode.
This CLI presentation does not change the SDK's opt-in 403 result, which still
reports `reachable=True` and `auth_required=True`. In contrast, a transport
`AuthError` (including one whose diagnostic mentions a token-server 403) has no
known registry status and retains the login hint and exit code 0.

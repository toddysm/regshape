# Registry authentication

<!-- doc-type: reference -->

Import `login` and `verify_credentials` from `regshape.libs.auth`. Both accept
explicit credentials and run without Click, a command context or interactive
prompts. Credential resolution and prompting remain separate concerns.

## `verify_credentials`

```python
verify_credentials(
    registry: str,
    username: str,
    password: str,
    *,
    insecure: bool = False,
    timeout: int = 10,
) -> None
```

Issue `GET /v2/` using `RegistryClient`, including its authentication challenge
and retry handling. Only a final HTTP `200` succeeds. The response is closed
before returning or raising; no credentials are stored.

| Parameter | Meaning |
| --- | --- |
| `registry` | Hostname, optionally with a port, without a URL scheme |
| `username` | Explicit username |
| `password` | Password or access token |
| `insecure` | Use HTTP instead of HTTPS; default `False` |
| `timeout` | Registry request timeout in seconds; default `10` |

Returns `None` on success. Raises `AuthError` on authentication failure or a
non-200 final response. Requests transport exceptions propagate unchanged.
An empty registry or one containing a URL scheme raises `ValueError`.
If either credential is `None`, raises `AuthError` before constructing the
transport or looking up stored credentials. Login requires explicit values;
it never substitutes stored or anonymous credentials for missing arguments.

A registry that allows anonymous access to `/v2/` may return `200` without
challenging the supplied credentials. Success does not establish repository
pull/push permissions or token scope.

Verification retains `@track_time`. Its method-timing label remains
`_verify_credentials` for compatibility with existing CLI telemetry. The public
function is an alias, so introspection (`__name__` and `help()`) and tracebacks
also retain that private implementation name. Use a
[library telemetry session](telemetry.md) to collect SDK timing or HTTP diagnostics.

## `login`

```python
login(
    registry: str,
    username: str,
    password: str,
    *,
    insecure: bool = False,
    docker_config_path: str | None = None,
    timeout: int = 10,
) -> None
```

Verify the explicit credentials, then call `store_credentials`. Verification
failure never attempts storage. Connection parameters have the same meaning
as in `verify_credentials`. `docker_config_path` selects an alternate Docker
configuration file according to the existing credential-storage lookup rules.
Without an override, storage uses the usual Docker configuration.

Storage uses the configured registry credential helper or global `credsStore`.
Without a helper, it writes a Base64-encoded `auths` entry in the Docker config.
Base64 is not encryption; use a credential helper when available.

Returns `None` without printing a success message. Verification errors propagate
as described above. A storage-layer `AuthError` becomes
`regshape.libs.errors.CredentialStoreError`, an `AuthError` subclass. Its `error`
attribute and chained cause retain the original storage error. Other storage
exceptions propagate unchanged.

```python
from getpass import getpass

from regshape.libs.auth import login

username = input("Registry username: ")
password = getpass("Registry password or token: ")
login("registry.example.com", username, password)
```

The application owns credential input and success/error presentation.
To verify without persisting, call `verify_credentials` instead.

The [CLI authentication command](../../specs/cli/auth.md) uses this library
operation after resolving flags, stored credentials or interactive input.

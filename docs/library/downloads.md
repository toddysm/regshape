# Download policy and pooled transport

<!-- doc-type: reference -->

This development-build reference covers the transport foundation from
[#134](https://github.com/toddysm/regshape/issues/134), targeting 0.2, not a
published release. See the [approved design](../../specs/operations/parallel-downloads.md).
The [blob API](../../specs/operations/blobs.md#get_blob) and
[artifact API](../../specs/operations/artifacts.md#signatures-and-caller-ownership)
accept keyword-only `download_options`; existing positional arguments, results,
output ownership and progress observers are unchanged.

## Upgrade note: default transport changes

Omitting `download_options` on `get_blob` or `pull_artifact` now selects a pooled
worker for supported clients, even if the borrowed client has
`enable_retries=False`. Unless an explicit retry policy says otherwise, pooled
downloads retry 429/transient 5xx responses up to three times, honor bounded
`Retry-After` directives, and log each attempt. Responses update the worker's
`last_response`, not the borrowed client's. Result types and positional
arguments are unchanged; these transport and observability changes are intentional.

Pass `download_options=DownloadOptions.legacy()` to either operation to retain
the original client's retry, logging/middleware and `last_response` behavior.
`DownloadOptions.sequential()` still uses pooled transport and is **not** a
compatibility rollback. The legacy example below shows the explicit rollback.

## Policy

Import the same frozen `DownloadOptions` type from `regshape.libs.blobs` or
`regshape.libs.artifacts`.

| Field | Default | Allowed values |
| --- | --- | --- |
| `concurrency` | `4` | Integer 1..32 |
| `ranges_per_blob` | `1` | Integer 1..concurrency |
| `range_size` | `8388608` | Integer 65536..67108864 bytes |
| `max_retries` | `None` | None or integer 0..10 |
| `scratch_dir` | `None` | Existing caller-trusted directory path |
| `transport` | `"pooled"` | `"pooled"` or `"legacy"` |

Booleans are not valid integer options. Invalid policy and unsupported explicit
pooled customization raise `ValueError` before requests or output creation.
Scratch paths are pinned to absolute paths and revalidated before acquisition;
the library cannot establish the caller's trust in that directory.

Pooled retries default to three, including HTTP 429 and the existing transient
server statuses. An enabled explicit `TransportConfig.retry_config` instead
supplies its original budget, backoff, statuses and exception policy.
An explicit retry configuration with `enable_retries=False` retains its policy
with a zero budget unless a download override is supplied. The resolved budget,
including one inherited from the client, must be a non-boolean integer in 0..10
and is checked before auth/pool creation.
`max_retries` explicitly overrides its budget, including zero to disable retries.
Each request uses the original finite client timeout.
Pooled 429/503 retries honor a valid `Retry-After` delay or HTTP date, waiting at
least the greater of that delay and configured exponential backoff after releasing
the response slot. Malformed values emit a sanitized diagnostic and retain normal
backoff. Server-directed delays above 300 seconds raise `ValueError` after the
response is closed, rather than hanging acquisition or retrying early.
Legacy retry timing is unchanged.
One retry owner wraps the complete authentication flow, so registry status and
transport retries share their budget across authentication resends. If a
headerless object 401 requires a `/v2/` challenge probe, retryable probe responses
use that same budget and bounded `Retry-After` policy, not a separate retry loop.

`DownloadOptions.sequential()` means pooled C=W=1. `DownloadOptions.legacy()`
means C=W=1 with the **original borrowed-client transport**; it requires
`max_retries=None` and `scratch_dir=None`. It never initializes the new auth
provider, shared adapter, worker session or coordinator. It retains the original
client's retry/middleware/progress and `last_response` behavior.

```python
from regshape.libs.blobs import DownloadOptions, get_blob

# client is an already configured RegistryClient; no new credential lookup.
info = get_blob(client, "team/image", digest,
                download_options=DownloadOptions.sequential())

# Genuine rollback, including unsupported existing custom clients.
info = get_blob(client, "team/image", digest,
                download_options=DownloadOptions.legacy())
```

## Scoped authentication and ownership

The operation resolves no credentials: it snapshots the borrowed client's
already resolved credentials and connection configuration. Root/manifest/blob
clients share one operation-local single-flight provider. One leader discovers
the same repository's challenge and exchanges a token while followers wait.
Unsuccessful first responses do not mark discovery complete; waiting peers
still share serialized challenge discovery while a throttled request backs off.
Tokens are bound to the registry origin, credential snapshot, validated
realm/service and repository pull scope. Expiry metadata or rejection of a
generation causes one shared refresh. Anonymous and Basic flows remain supported;
foreign 401 responses cannot select token realms or warm this provider.
Concurrent rejected requests reuse a refreshed generation when their parsed
challenges match or the response omits its challenge; an explicit different realm
or service is negotiated separately.
Exchange failures are not cached: a later request can retry without replaying
another worker's exception. Fractional RFC 3339 `issued_at` timestamps, including
Docker Hub's nanosecond precision, are normalized for Python 3.10 compatibility.
Scopes use the terminal manifest/blob resource; nested repository components
named `manifests` or `blobs` remain part of the repository identity.

Token exchanges use the same instrumented HTTP hook, finite timeout and pool.
Both GET and refresh-token POST request identity encoding rather than inheriting
the session's optional compression codecs.
Token GET connection/timeouts preserve their retryable Requests exception type,
with sanitized messages, so they share the operation's retry budget. Exhaustion
surfaces that sanitized transport exception; refresh POST failures retain the
original GET 401 as described below.
External registry-issued token realms are supported without credential-bearing
URLs or HTTPS-to-HTTP downgrade. Token-endpoint redirects are rejected. The
ACR refresh-token POST fallback requires the sentinel username
`00000000-0000-0000-0000-000000000000` and a nonempty password, sends
`client_id=regshape`, and retains the original GET 401 if the fallback fails.
Ordinary rejected username/password credentials do not trigger this POST.
Challenges may omit `service` or include additional actions such as `pull,push`
on the same repository; token requests still ask only for the expected pull
scope. Different repositories or scopes without pull are rejected.
GET and refresh-token POST reject redirects in a response hook before Requests
can buffer their bodies, preserving the token-response byte limit.

Worker-local Requests sessions isolate headers, cookies, caches, retry state and
`last_response`. One blocking adapter bounds its origin-pool cache and per-origin
pool size to C; operation-wide slots stay reserved until streamed responses close,
including redirect/token requests. Sessions preserve Requests' environment proxy
and CA configuration and normal redirect credential stripping. Proxy-manager
initialization is single-flight, with at most max(2, C) proxy managers per operation.
No streamed GET
is cached. Close each response and stop/join peers before closing the context.
Closing a worker session detaches the borrowed adapter, never closes a peer's
pool. The operation closes its adapter exactly once, including when closure
detects a still-open response and raises `RuntimeError`. Callers must still close
their responses and join workers; cleanup does not make premature closure safe.
The original client is neither closed nor mutated.

The extension hook is `RegistryClient.fork_for_download(*, context)`. An exact
base client with configured built-ins is supported. Subclasses, custom middleware
or unrecognized runtime request/pipeline changes require an explicit override
that preserves customization and returns a distinct client, private configuration
and worker-local session attached to that context. Omitted options on the two
existing operations instead retain unsupported custom clients' original serial
path with one logged notice. New batch/status consumers have no such exception.
Connection/retry snapshots do not copy custom middleware; the override must
reconstruct it with isolated state, including locks. Nested blob/artifact calls
inherit their existing context's policy without reforking or compatibility
warnings, and reject a conflicting explicit policy.

`DownloadTransport(client, options)` is an internal building block for the
scheduler. It allows one discovery/root session and at most C open worker
sessions, reclaiming closed session records. Worker callers must not share one
client/session concurrently.

## Delivery boundary

This foundation retains **serial graph acquisition and full-stream blob receipt**.
The policy carries concurrency/range controls for downstream integration; it
does not yet enable object/range scheduling, new batch/status result models or
CLI flags. Those remain owned by
[#135](https://github.com/toddysm/regshape/issues/135),
[#144](https://github.com/toddysm/regshape/issues/144),
[#136](https://github.com/toddysm/regshape/issues/136) and
[#137](https://github.com/toddysm/regshape/issues/137).
Existing callbacks run synchronously on the invoking thread, and standalone
verify-only downloads still hash/discard the received bytes.

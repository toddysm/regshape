# RegShape Architecture

## Overview

This document defines the overall architecture for RegShape, a Python CLI tool and library for OCI registry manipulation. RegShape provides standard OCI Distribution Spec operations alongside a unique "break mode" for deliberately sending malformed requests to test registry implementations.

## Table of Contents

- [1. Layered Architecture](#1-layered-architecture)
- [2. HTTP Transport Layer](#2-http-transport-layer)
- [3. Module Dependency Graph](#3-module-dependency-graph)
- [4. Data Model Overview](#4-data-model-overview)
- [5. Break Mode Architecture](#5-break-mode-architecture)
- [6. Error Handling Strategy](#6-error-handling-strategy)
- [7. CLI Command Structure](#7-cli-command-structure)
- [8. Telemetry Decorators](#8-telemetry-decorators)
- [9. Implementation Sequence](#9-implementation-sequence)
- [Open Questions](#open-questions)

---

## 1. Layered Architecture

RegShape is organized into three layers. Each layer depends only on the layers below it. The library layer (`libs/`) is fully usable without the CLI.

```
+-------------------------------------------------------+
|                     CLI Layer                          |
|  src/regshape/cli/                                     |
|  Click command groups, argument parsing, output        |
|  formatting, exit code mapping                         |
+-------------------------------------------------------+
          |  calls into
          v
+-------------------------------------------------------+
|                  Domain Operations Layer               |
|  src/regshape/libs/manifests/                          |
|  src/regshape/libs/blobs/                              |
|  src/regshape/libs/tags/                               |
|  src/regshape/libs/referrers/                          |
|  src/regshape/libs/catalog/                            |
|                                                        |
|  Each module exposes high-level functions for one      |
|  OCI Distribution Spec endpoint group.                 |
+-------------------------------------------------------+
          |  uses
          v
+-------------------------------------------------------+
|                Foundation Layer                         |
|  src/regshape/libs/transport/   (HTTP client + middleware)|
|  src/regshape/libs/auth/        (existing)             |
|  src/regshape/libs/models/      (data models)          |
|  src/regshape/libs/breakmode/   (break mode config)    |
|  src/regshape/libs/errors.py    (existing)             |
|  src/regshape/libs/constants.py (existing)             |
|  src/regshape/libs/decorators/  (telemetry decorators)  |
+-------------------------------------------------------+
```

### Layer Responsibilities

**CLI Layer** -- Translates user input (commands, options, arguments) into calls to the domain operations layer. Handles output formatting (plain text vs JSON), progress display, and maps exceptions to exit codes and error messages. Never contains protocol logic.

**Domain Operations Layer** -- Implements the business logic for each OCI endpoint group. Each module accepts typed parameters (models, strings, streams) and returns typed results. These modules use the transport layer to make HTTP requests and the models layer for data structures. Each module is independently importable.

**Foundation Layer** -- Provides cross-cutting infrastructure: the HTTP transport client with its middleware pipeline (auth, break mode, logging), data models, error types, and constants. The transport layer is the single point through which all HTTP traffic flows.

### Key Design Constraint

Domain operation modules never call `requests.get/post/...` directly. All HTTP traffic goes through `libs/transport/`, which is the single chokepoint for auth injection, break mode interception, and request/response logging.

---

## 2. HTTP Transport Layer

The transport layer (`src/regshape/libs/transport/`) wraps the `requests` library and provides a middleware pipeline that every HTTP request passes through.

### Module Structure

```
src/regshape/libs/transport/
├── __init__.py          # Exports: RegistryClient, TransportConfig
├── client.py            # RegistryClient class
├── middleware.py         # Middleware protocol and built-in middleware
└── logging.py           # Request/response logging utilities
```

### TransportConfig

```python
@dataclass
class TransportConfig:
    """Configuration for the HTTP transport layer.

    :param base_url: The registry base URL (e.g., ``https://registry.example.com``).
    :param username: Optional username for authentication.
    :param password: Optional password for authentication.
    :param insecure: Allow HTTP instead of HTTPS.
    :param user_agent: User-Agent header value.
    :param timeout: Request timeout in seconds.
    :param break_mode_config: Optional break mode configuration.
    """
    base_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    insecure: bool = False
    user_agent: str = "regshape/0.1"
    timeout: int = 30
    break_mode_config: Optional['BreakModeConfig'] = None
```

### RegistryClient

The central HTTP client. All domain modules receive a `RegistryClient` instance rather than constructing their own HTTP sessions.

```python
class RegistryClient:
    """HTTP client for OCI registry communication.

    All registry HTTP traffic flows through this client. Middleware is applied
    in order for requests and in reverse order for responses.

    :param config: Transport configuration.
    """

    def __init__(self, config: TransportConfig) -> None: ...

    def request(
        self,
        method: str,
        path: str,
        headers: Optional[dict] = None,
        body: Optional[bytes] = None,
        stream: bool = False,
        **kwargs
    ) -> 'RegistryResponse': ...

    # Convenience methods
    def get(self, path: str, **kwargs) -> 'RegistryResponse': ...
    def head(self, path: str, **kwargs) -> 'RegistryResponse': ...
    def put(self, path: str, **kwargs) -> 'RegistryResponse': ...
    def post(self, path: str, **kwargs) -> 'RegistryResponse': ...
    def patch(self, path: str, **kwargs) -> 'RegistryResponse': ...
    def delete(self, path: str, **kwargs) -> 'RegistryResponse': ...
```

### Middleware Protocol

Each middleware receives a `RegistryRequest` and a `next_handler` callable, and returns a `RegistryResponse`. This allows each middleware to inspect/modify both the request (before calling next) and the response (after calling next).

```python
class Middleware(Protocol):
    """Protocol for transport middleware."""

    def __call__(
        self,
        request: 'RegistryRequest',
        next_handler: Callable[['RegistryRequest'], 'RegistryResponse']
    ) -> 'RegistryResponse': ...
```

### Request Flow

```
Domain Module
     |
     v
RegistryClient.request()
     |
     v  (middleware pipeline, outermost first)
+--------------------------------------------+
|  LoggingMiddleware                          |
|    records full request, delegates,         |
|    records full response                    |
+--------------------------------------------+
     |
     v
+--------------------------------------------+
|  BreakModeMiddleware                        |
|    if break mode active: mutates request    |
|    per BreakModeConfig rules                |
|    if break mode inactive: passes through   |
+--------------------------------------------+
     |
     v
+--------------------------------------------+
|  AuthMiddleware                             |
|    uses libs/auth/ to obtain credentials    |
|    adds Authorization header                |
|    handles 401 retry with fresh token       |
+--------------------------------------------+
     |
     v
+--------------------------------------------+
|  requests.Session.request()                 |
|    actual HTTP call                         |
+--------------------------------------------+
```

The ordering is intentional:

- **LoggingMiddleware** is outermost so it captures the final request (after break mode mutations) and the raw response. This is critical for break mode analysis.
- **BreakModeMiddleware** sits between logging and auth so it can tamper with auth headers, skip auth entirely, or inject malformed headers. When break mode is inactive, this middleware is a no-op passthrough.
- **AuthMiddleware** is innermost, closest to the actual HTTP call. It uses the existing `libs/auth/registryauth.py` functions. On a 401 response, it re-authenticates and retries once.

### RegistryRequest and RegistryResponse

Internal representations that flow through the middleware pipeline.

```python
@dataclass
class RegistryRequest:
    """Internal representation of an outgoing HTTP request.

    `body` may be either a fully-buffered bytes object or an iterable of
    bytes chunks for streaming large payloads.
    """
    method: str
    url: str
    headers: dict
    body: Optional[bytes | Iterable[bytes]] = None
    stream: bool = False

@dataclass
class RegistryResponse:
    """Internal representation of an HTTP response."""
    status_code: int
    headers: dict
    body: bytes
    raw_response: requests.Response  # preserved for streaming
```

---

## 3. Module Dependency Graph

Arrows indicate "depends on" (imports from).

```
                        CLI Layer
                   +----------------+
                   |  cli/main.py   |
                   |  cli/manifest.py|
                   |  cli/blob.py   |
                   |  cli/tag.py    |
                   |  cli/referrer.py|
                   |  cli/catalog.py|
                   +-------+--------+
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
   libs/manifests/   libs/blobs/    libs/tags/
   libs/referrers/   libs/catalog/
          |                |                |
          +--------+-------+-------+--------+
                   |               |
                   v               v
           libs/transport/   libs/models/
                   |
          +--------+--------+
          |        |        |
          v        v        v
    libs/auth/  libs/     libs/
               breakmode/ errors.py
                   |
                   v
            libs/constants.py
            libs/decorators/
```

### Dependency Rules

1. **CLI modules** depend on domain operations modules and `libs/models/`. They never import from `libs/transport/` directly (the `RegistryClient` is constructed in CLI setup and passed down).
2. **Domain operations modules** (manifests, blobs, tags, referrers, catalog) depend on `libs/transport/` and `libs/models/`. They do not depend on each other (except blobs may be referenced by manifests via model types).
3. **Transport** depends on `libs/models/` (for `RegistryRequest` / `RegistryResponse` types), `libs/auth/`, `libs/breakmode/`, `libs/errors.py`, and `libs/decorators/`.
4. **Models** (including `RegistryRequest` / `RegistryResponse` types) depend only on `libs/constants.py` and the standard library.
5. **Auth** depends on `libs/errors.py`, `libs/constants.py`, `libs/decorators/`.
6. **Break mode** depends on `libs/models/` (including `RegistryRequest` / `RegistryResponse` types) and `libs/errors.py`.

The dependency graph is a DAG with no circular dependencies.

---

## 4. Data Model Overview

All data models live in `src/regshape/libs/models/` as Python `dataclass` types with full type hints. Models handle serialization to and from JSON (the OCI wire format).

### Module Structure

```
src/regshape/libs/models/
├── __init__.py          # Exports all model classes
├── descriptor.py        # Descriptor, Platform
├── manifest.py          # ImageManifest, ImageIndex
├── blob.py              # BlobUploadSession, BlobInfo
├── tag.py               # TagList
├── catalog.py           # CatalogList
├── referrer.py          # ReferrerList
├── error.py             # OCI error response models
└── mediatype.py         # Media type constants
```

### Core Types

#### Descriptor

The fundamental building block of the OCI model. References a piece of content by digest and size.

```python
@dataclass
class Descriptor:
    """OCI content descriptor.

    :param media_type: Media type of the referenced content.
    :param digest: Digest of the content (e.g., ``sha256:abc123...``).
    :param size: Size in bytes.
    :param annotations: Optional annotations.
    :param artifact_type: Optional artifact type.
    :param urls: Optional external URLs for content.
    """
    media_type: str
    digest: str
    size: int
    annotations: Optional[dict[str, str]] = None
    artifact_type: Optional[str] = None
    urls: Optional[list[str]] = None

    def to_dict(self) -> dict: ...

    @classmethod
    def from_dict(cls, data: dict) -> 'Descriptor': ...
```

#### Platform

```python
@dataclass
class Platform:
    """OCI platform specification for multi-arch manifests.

    :param architecture: CPU architecture (e.g., ``amd64``).
    :param os: Operating system (e.g., ``linux``).
    :param os_version: Optional OS version.
    :param os_features: Optional OS features.
    :param variant: Optional CPU variant (e.g., ``v8``).
    """
    architecture: str
    os: str
    os_version: Optional[str] = None
    os_features: Optional[list[str]] = None
    variant: Optional[str] = None
```

#### ImageManifest

```python
@dataclass
class ImageManifest:
    """OCI Image Manifest.

    :param schema_version: Schema version (always 2).
    :param media_type: Manifest media type.
    :param config: Config descriptor.
    :param layers: Layer descriptors.
    :param subject: Optional subject descriptor (for referrers).
    :param annotations: Optional annotations.
    :param artifact_type: Optional artifact type.
    """
    schema_version: int
    media_type: str
    config: Descriptor
    layers: list[Descriptor]
    subject: Optional[Descriptor] = None
    annotations: Optional[dict[str, str]] = None
    artifact_type: Optional[str] = None

    def to_json(self) -> str: ...
    def digest(self) -> str: ...

    @classmethod
    def from_json(cls, data: str) -> 'ImageManifest': ...
```

#### ImageIndex

```python
@dataclass
class ImageIndex:
    """OCI Image Index (multi-arch manifest list).

    :param schema_version: Schema version (always 2).
    :param media_type: Index media type.
    :param manifests: List of manifest descriptors with optional platform info.
    :param annotations: Optional annotations.
    """
    schema_version: int
    media_type: str
    manifests: list[Descriptor]
    annotations: Optional[dict[str, str]] = None

    def to_json(self) -> str: ...
    def digest(self) -> str: ...

    @classmethod
    def from_json(cls, data: str) -> 'ImageIndex': ...
```

#### BlobUploadSession

Tracks the state of an in-progress blob upload (monolithic or chunked).

```python
@dataclass
class BlobUploadSession:
    """Tracks state of an in-progress blob upload.

    :param upload_url: The URL for continuing the upload (from Location header).
    :param session_id: The upload session identifier.
    :param offset: Current byte offset for chunked uploads.
    :param digest: Expected final digest of the blob.
    :param total_size: Expected total size of the blob.
    """
    upload_url: str
    session_id: str
    offset: int = 0
    digest: Optional[str] = None
    total_size: Optional[int] = None
```

#### TagList and CatalogList

```python
@dataclass
class TagList:
    """Response from the tags/list endpoint.

    :param name: Repository name.
    :param tags: List of tag strings.
    """
    name: str
    tags: list[str]

@dataclass
class CatalogList:
    """Response from the _catalog endpoint.

    :param repositories: List of repository names.
    """
    repositories: list[str]
```

#### OCI Error Models

```python
@dataclass
class OciErrorDetail:
    """Single error from an OCI error response.

    :param code: OCI error code (e.g., ``MANIFEST_UNKNOWN``).
    :param message: Human-readable message.
    :param detail: Optional additional detail.
    """
    code: str
    message: str
    detail: Optional[dict] = None

@dataclass
class OciErrorResponse:
    """Parsed OCI error response body.

    :param errors: List of error details.
    """
    errors: list[OciErrorDetail]

    @classmethod
    def from_json(cls, data: str) -> 'OciErrorResponse': ...
```

#### Media Type Constants

```python
# src/regshape/libs/models/mediatype.py

OCI_IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_IMAGE_LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"
OCI_IMAGE_LAYER_TAR_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
OCI_IMAGE_LAYER_TAR_ZSTD = "application/vnd.oci.image.layer.v1.tar+zstd"
OCI_EMPTY = "application/vnd.oci.empty.v1+json"
DOCKER_MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_MANIFEST_LIST_V2 = "application/vnd.docker.distribution.manifest.list.v2+json"
```

---

## 5. Break Mode Architecture

Break mode is RegShape's distinguishing feature. It allows deliberate mutation of any part of an HTTP request to test how registries handle malformed or non-conformant traffic. The architecture ensures that break mode is a cross-cutting concern implemented as transport middleware, keeping the domain operation code completely clean.

### Module Structure

```
src/regshape/libs/breakmode/
├── __init__.py          # Exports: BreakModeConfig, BreakRule
├── config.py            # BreakModeConfig, BreakRule dataclasses
├── rules.py             # Built-in rule factories (convenience functions)
└── middleware.py         # BreakModeMiddleware (transport middleware)
```

### BreakModeConfig and BreakRule

Break mode is configured by composing a list of `BreakRule` objects. Each rule specifies what to mutate, when to apply (optional predicate), and how to mutate it.

```python
@dataclass
class BreakRule:
    """A single request mutation rule.

    :param name: Human-readable name for logging.
    :param target: What to mutate. One of: ``header``, ``body``, ``method``,
        ``path``, ``query``, ``digest``, ``content_type``, ``auth``.
    :param action: How to mutate. One of: ``replace``, ``remove``, ``append``,
        ``corrupt``, ``skip``.
    :param key: For header/query targets, which key to modify.
    :param value: The replacement/appended value (interpretation depends on action).
    :param predicate: Optional callable that receives a RegistryRequest and
        returns True if this rule should apply to that request.
    """
    name: str
    target: str
    action: str
    key: Optional[str] = None
    value: Optional[Any] = None
    predicate: Optional[Callable[['RegistryRequest'], bool]] = None


@dataclass
class BreakModeConfig:
    """Configuration for break mode.

    :param enabled: Master switch. Break mode is only active when True.
    :param rules: Ordered list of mutation rules to apply.
    :param log_requests: Whether to log full request/response pairs.
    :param log_file: Optional file path for break mode logs.
    """
    enabled: bool = False
    rules: list[BreakRule] = field(default_factory=list)
    log_requests: bool = True
    log_file: Optional[str] = None
```

### Built-in Rule Factories

`src/regshape/libs/breakmode/rules.py` provides convenience functions for common break scenarios:

```python
def wrong_digest(...) -> BreakRule:
    """Replace digest values in the request path or query string."""

def invalid_content_type(...) -> BreakRule:
    """Replace the Content-Type header with an invalid value."""

def skip_auth() -> BreakRule:
    """Remove the Authorization header entirely."""

def expired_token(token: str) -> BreakRule:
    """Replace the Authorization header with a known expired token."""

def corrupt_body(...) -> BreakRule:
    """Append corruption bytes to the request body."""

def oversized_payload(extra_bytes: int = 1024 * 1024) -> BreakRule:
    """Pad the request body with extra null bytes."""

def wrong_method(method: str) -> BreakRule:
    """Replace the HTTP method (e.g., send GET instead of PUT)."""

def custom_header(key: str, value: str) -> BreakRule:
    """Add or replace an arbitrary header."""
```

### BreakModeMiddleware

```python
class BreakModeMiddleware:
    """Transport middleware that applies break mode mutations.

    When break mode is disabled, this middleware is a zero-cost passthrough.
    When enabled, it iterates through the configured rules and applies
    each matching rule to the request before passing it to the next handler.
    """

    def __init__(self, config: BreakModeConfig) -> None: ...

    def __call__(
        self,
        request: RegistryRequest,
        next_handler: Callable[[RegistryRequest], RegistryResponse]
    ) -> RegistryResponse:
        if not self.config.enabled:
            return next_handler(request)

        mutated_request = self._apply_rules(request)
        return next_handler(mutated_request)
```

### Design Principles

1. **Explicit opt-in only.** `BreakModeConfig.enabled` defaults to `False`. The CLI requires `--break` flag to activate.
2. **Domain modules are unaware.** Manifest, blob, tag, referrer, and catalog modules never import from `libs/breakmode/`. Break mode is injected at the transport layer.
3. **Composable rules.** Multiple rules can be stacked. Rules are applied in order. A predicate can limit a rule to specific request patterns.
4. **Full logging.** When break mode is active, the `LoggingMiddleware` (which wraps break mode) captures the final mutated request and the registry's response.

### Library Usage Example

```python
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.breakmode import BreakModeConfig
from regshape.libs.breakmode.rules import wrong_digest, skip_auth
from regshape.libs.manifests import manifest_ops

config = TransportConfig(
    base_url="https://registry.example.com",
    username="user",
    password="pass",
    break_mode_config=BreakModeConfig(
        enabled=True,
        rules=[wrong_digest(), skip_auth()],
        log_requests=True,
    ),
)
client = RegistryClient(config)
response = manifest_ops.get_manifest(client, "myrepo", "latest")
```

---

## 6. Error Handling Strategy

### Exception Hierarchy

Extends the existing `RegShapeError` -> `AuthError` hierarchy with domain-specific errors mapped to OCI error codes.

```
RegShapeError (base, existing)
├── AuthError (existing)
├── RegistryError
│   ├── ManifestError
│   │   ├── ManifestNotFoundError      # MANIFEST_UNKNOWN (404)
│   │   ├── ManifestInvalidError       # MANIFEST_INVALID (400)
│   │   └── ManifestBlobUnknownError   # MANIFEST_BLOB_UNKNOWN (404)
│   ├── BlobError
│   │   ├── BlobNotFoundError          # BLOB_UNKNOWN (404)
│   │   ├── BlobUploadInvalidError     # BLOB_UPLOAD_INVALID (400)
│   │   ├── BlobUploadUnknownError     # BLOB_UPLOAD_UNKNOWN (404)
│   │   └── DigestInvalidError         # DIGEST_INVALID (400)
│   ├── NameError_
│   │   ├── NameInvalidError           # NAME_INVALID (400)
│   │   └── NameUnknownError           # NAME_UNKNOWN (404)
│   ├── SizeInvalidError               # SIZE_INVALID (400)
│   ├── DeniedError                    # DENIED (403)
│   ├── UnsupportedError               # UNSUPPORTED (405)
│   └── TooManyRequestsError           # TOOMANYREQUESTS (429)
├── TransportError
│   ├── RegistryConnectionError
│   ├── RegistryTimeoutError
│   └── TlsError
└── BreakModeError
    └── BreakModeConfigError
```

### OCI Error Code Mapping

The transport layer parses OCI error response bodies and raises the appropriate typed exception.

```python
OCI_ERROR_MAP: dict[str, type[RegistryError]] = {
    "BLOB_UNKNOWN": BlobNotFoundError,
    "BLOB_UPLOAD_INVALID": BlobUploadInvalidError,
    "BLOB_UPLOAD_UNKNOWN": BlobUploadUnknownError,
    "DIGEST_INVALID": DigestInvalidError,
    "MANIFEST_BLOB_UNKNOWN": ManifestBlobUnknownError,
    "MANIFEST_INVALID": ManifestInvalidError,
    "MANIFEST_UNKNOWN": ManifestNotFoundError,
    "NAME_INVALID": NameInvalidError,
    "NAME_UNKNOWN": NameUnknownError,
    "SIZE_INVALID": SizeInvalidError,
    "UNAUTHORIZED": AuthError,
    "DENIED": DeniedError,
    "UNSUPPORTED": UnsupportedError,
    "TOOMANYREQUESTS": TooManyRequestsError,
}
```

### Error Handling Pattern by Layer

1. **Transport layer** -- Catches `requests` exceptions and wraps them in `TransportError` subtypes. Parses OCI error response JSON and raises the mapped `RegistryError` subtype.
2. **Domain operations layer** -- May catch and re-raise with additional context (e.g., adding the repository name and reference to a `ManifestNotFoundError`).
3. **CLI layer** -- Catches all `RegShapeError` subtypes and maps them to user-friendly messages and exit codes.

### RegistryError Attributes

```python
class RegistryError(RegShapeError):
    """Raised when a registry operation fails.

    :param message: Human-readable error message.
    :param cause: Underlying cause description.
    :param oci_code: The OCI error code string (e.g., ``MANIFEST_UNKNOWN``).
    :param status_code: HTTP status code from the registry response.
    :param detail: Optional detail dict from the OCI error response.
    """
    def __init__(
        self,
        message: str = None,
        cause: str = None,
        oci_code: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: Optional[dict] = None,
        *args: object
    ) -> None: ...
```

---

## 7. CLI Command Structure

The CLI uses Click with a top-level group and subcommand groups for each domain.

### CLI Design Principles

- **Always use named flags; never use positional arguments.** Every input is expressed as a named option (e.g. `--image-ref`, `--repo`, `--digest`). This makes commands self-documenting, composable in scripts without relying on position, and easier to extend without breaking callers.
- Per-command flags (e.g. `--registry/-r`, `--image-ref/-i`) take precedence over global options for the same concept.
- Short aliases are provided for the most frequently-used options.

### Entry Point

```
regshape [GLOBAL OPTIONS] <command-group> <command> [OPTIONS]
```

### Global Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--insecure` | | flag | false | Allow HTTP (no TLS) |
| `--verbose` | `-v` | flag | false | Verbose output |
| `--break` | | flag | false | Enable break mode |
| `--break-rules` | | string | none | Path to break mode rules file |
| `--log-file` | | string | none | Path for request/response log output |

### Command Groups and Commands

```
regshape
├── ping --registry <registry>               # GET /v2/
├── manifest
│   ├── get    --image-ref <ref> [OPTIONS]   # GET /v2/<name>/manifests/<ref>
│   ├── info       --image-ref <ref> [OPTIONS]   # HEAD /v2/<name>/manifests/<ref>
│   ├── descriptor --image-ref <ref> [OPTIONS]   # HEAD /v2/<name>/manifests/<ref> → Descriptor JSON
│   ├── put    --image-ref <ref> [OPTIONS]   # PUT /v2/<name>/manifests/<ref>
│   └── delete --image-ref <ref> [OPTIONS]   # DELETE /v2/<name>/manifests/<ref>
├── blob
│   ├── get    --repo <repo> --digest <digest> [--output <path>]           # GET /v2/<name>/blobs/<digest>
│   ├── head   --repo <repo> --digest <digest>                             # HEAD /v2/<name>/blobs/<digest>
│   ├── delete --repo <repo> --digest <digest>                             # DELETE /v2/<name>/blobs/<digest>
│   ├── upload --repo <repo> --file <file> [--chunked] [--chunk-size <n>]  # POST + PUT (or POST + PATCH + PUT)
│   └── mount  --repo <repo> --digest <digest> --from <source>             # POST with mount param
├── tag
│   └── list --repo <repo> [--limit <n>] [--last <tag>]   # GET /v2/<name>/tags/list
├── referrer
│   └── list --repo <repo> --digest <digest> [--type <filter>]  # GET /v2/<name>/referrers/<digest>
└── catalog
    └── list [--limit <n>] [--last <repo>]                # GET /v2/_catalog
```

### CLI Module Structure

```
src/regshape/cli/
├── __init__.py          # Empty or exports main group
├── main.py              # Top-level Click group, global options, client setup
├── manifest.py          # manifest command group
├── blob.py              # blob command group
├── tag.py               # tag command group
├── referrer.py          # referrer command group
├── catalog.py           # catalog command group
└── formatting.py        # Output formatting helpers (plain text, JSON)
```

### CLI-to-Library Wiring

The CLI `main.py` constructs a `RegistryClient` from global options and stores it in the Click context. Subcommands retrieve the client from context and call domain operation functions.

```python
@click.group()
@click.option("--insecure", is_flag=True, help="Allow HTTP")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--break", "break_mode", is_flag=True, help="Enable break mode")
@click.option("--break-rules", type=click.Path(exists=True), help="Break rules file")
@click.option("--log-file", type=click.Path(), help="Request/response log file")
@click.pass_context
def regshape(ctx, insecure, verbose, break_mode, break_rules, log_file):
    """RegShape - OCI registry manipulation tool."""
    ctx.ensure_object(dict)
    # ... construct TransportConfig and RegistryClient ...
    ctx.obj["insecure"] = insecure
    ctx.obj["verbose"] = verbose
    ctx.obj["break_mode"] = break_mode
    ctx.obj["break_rules"] = break_rules
    ctx.obj["log_file"] = log_file
```

---

## 8. Telemetry Decorators

RegShape provides three decorator-based telemetry capabilities for measuring execution time, tracking multi-step workflows, and inspecting HTTP call details. All three are implemented in `libs/decorators/` and are controlled by dedicated CLI flags.

### Module Structure

```
src/regshape/libs/decorators/
├── __init__.py          # Exports: track_time, track_scenario, debug_call, telemetry_options, …
├── timing.py            # @track_time decorator — accumulates into TelemetryConfig.method_timings
├── scenario.py          # @track_scenario decorator — renders telemetry block + clears timings
├── call_details.py      # @debug_call decorator, format_curl_debug, http_request
└── output.py            # print_telemetry_block, flush_telemetry — single rendering path
```

### CLI Flags

Three leaf-command flags control telemetry output. They are independent of each other and of `--verbose`, and they appear **after** the subcommand name so the command reads naturally:

```
regshape auth login --time-methods -r registry.example.com
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--time-methods` | flag | false | Print execution time for individual method calls |
| `--time-scenarios` | flag | false | Print execution time for multi-step workflows |
| `--debug-calls` | flag | false | Print request/response headers for each HTTP call |

These flags are applied to every leaf command via the centralized `@telemetry_options` decorator defined in `libs/decorators/__init__.py`. The decorator adds the three Click options **and** automatically calls `configure_telemetry()` before the command body runs, so individual command implementations do not need to handle telemetry setup themselves.

```python
# Applied once per leaf command; no per-command telemetry boilerplate needed.
@some_group.command("name")
@telemetry_options
@click.option("--registry", "-r", required=True, ...)
...
def my_command(ctx, registry, ...):
    # configure_telemetry() has already been called by @telemetry_options
    ...
```

`telemetry_options` is defined in `libs/decorators/__init__.py` and exported alongside `track_time`, `track_scenario`, and `debug_call`.

### TelemetryConfig

A simple configuration object that the decorators read at runtime.

```python
@dataclass
class TelemetryConfig:
    """Runtime configuration for telemetry decorators.

    :param time_methods_enabled: When True, @track_time accumulates per-method
        timing entries into method_timings.
    :param time_scenarios_enabled: When True, @track_scenario renders the
        telemetry summary block at the end of the decorated workflow.
    :param debug_calls_enabled: When True, @debug_call prints each HTTP
        round-trip in curl -v style.
    :param output: Writable stream for telemetry output (defaults to stderr).
    :param method_timings: Ordered list of (qualname, elapsed) pairs accumulated
        by @track_time. Consumed and cleared by @track_scenario, or by
        flush_telemetry() for commands with no scenario wrapper.
    """
    time_methods_enabled: bool = False
    time_scenarios_enabled: bool = False
    debug_calls_enabled: bool = False
    output: IO = field(default=sys.stderr)
    method_timings: list[tuple[str, float]] = field(default_factory=list)
```

The active `TelemetryConfig` is stored in a module-level context variable so decorators can access it without threading configuration through every function signature.

```python
# src/regshape/libs/decorators/__init__.py
from contextvars import ContextVar

_telemetry_config: ContextVar[TelemetryConfig] = ContextVar(
    'telemetry_config', default=TelemetryConfig()
)

def configure_telemetry(config: TelemetryConfig) -> None:
    """Set the active telemetry configuration."""
    _telemetry_config.set(config)

def get_telemetry_config() -> TelemetryConfig:
    """Get the active telemetry configuration."""
    return _telemetry_config.get()
```

### Decorator: `@track_time`

Measures and prints the execution time of a single function or method.

```python
def track_time(func: Callable) -> Callable:
    """Decorator that accumulates per-method execution time into
    TelemetryConfig.method_timings when --time-methods is enabled.

    Does not emit any output itself. Entries are rendered as part of the
    telemetry block by @track_scenario or by flush_telemetry().

    When --time-methods is not enabled, this decorator acts as a lightweight
    passthrough: calls still go through the wrapper, incur a single boolean
    check, and then immediately dispatch to the original function without
    executing any timing logic.

    Usage:
        @track_time
        def get_manifest(client, repo, reference): ...
    """
```

Applied to individual functions in the domain operations layer (e.g., `get_manifest`, `put_blob`, `delete_tag`) and any other function where per-call timing is valuable.

### Decorator: `@track_scenario`

Measures and prints the execution time of a multi-step workflow. A scenario is a logical operation that may involve multiple HTTP calls (e.g., a chunked blob upload is POST + N×PATCH + PUT).

```python
def track_scenario(name: str) -> Callable:
    """Decorator that renders a telemetry summary block when --time-scenarios
    is enabled, incorporating any method timings accumulated by @track_time
    during the workflow.

    :param name: Human-readable scenario name (e.g., ``"chunked blob upload"``).

    Output format (all three flags active)::

        ── telemetry ──────────────────────────────────────────────────────
          scenario  auth login                                    0.523s
            method  _verify_credentials                           0.231s
            method  store_credentials                             0.045s
        ───────────────────────────────────────────────────────────────────

    With --time-scenarios only (no --time-methods)::

        ── telemetry ──────────────────────────────────────────────────────
          scenario  auth login                                    0.523s
        ───────────────────────────────────────────────────────────────────

    With --time-methods only (flush_telemetry() renders on command exit)::

        ── telemetry ──────────────────────────────────────────────────────
            method  _verify_credentials                           0.231s
            method  store_credentials                             0.045s
        ───────────────────────────────────────────────────────────────────

    Usage:
        @track_scenario("chunked blob upload")
        def upload_blob_chunked(client, repo, stream, chunk_size): ...
    """
```

Applied to higher-level workflow functions that orchestrate multiple operations. The distinction from `@track_time` is semantic: `@track_time` is for atomic operations, `@track_scenario` is for named workflows that compose multiple atomic operations. After rendering the block, `method_timings` is cleared so each scenario produces exactly one block.

### Decorator: `@debug_call`

Prints request and response headers for an HTTP call. This decorator is designed to wrap functions that make HTTP requests via the transport layer.

```python
def debug_call(func: Callable) -> Callable:
    """Decorator that prints each HTTP round-trip in curl -v style when
    --debug-calls is enabled.

    Expects the decorated function to return a RegistryResponse (or an object
    with ``status_code``, ``reason``, and ``headers`` attributes). When ``self``
    exposes ``config.base_url`` (or a top-level ``base_url`` attribute), the
    relative path is prepended with the base URL so the ``* Connected to`` line
    always carries the correct host and port.

    Output format (curl -v style)::

        * Connected to registry.example.com port 443
        > GET /v2/ HTTP/1.1
        > Host: registry.example.com
        > User-Agent: regshape/0.1
        >
        < HTTP/1.1 401 Unauthorized
        < Www-Authenticate: Bearer realm="https://auth.example.com/token"
        <

    Usage:
        @debug_call
        def request(self, method, path, ...): ...
    """
```

The shared :func:`format_curl_debug` helper (also exported from `libs/decorators/__init__.py`) implements the formatting logic. All HTTP debug output goes through this single function regardless of call site.

Applied to `RegistryClient.request()` (or the inner transport call) so that every HTTP round-trip can be inspected. The decorator extracts request details from the function arguments and response details from the return value.

For HTTP calls that cannot go through `RegistryClient` (such as the temporary direct `requests` calls in `cli/auth.py` prior to the transport layer being implemented), the shared `http_request(url, method, headers, **kwargs)` helper in `libs/decorators/call_details.py` (exported from `libs/decorators/`) should be used instead of calling `requests.get` / `requests.request` directly. It is already decorated with `@debug_call`, so any call site that imports and uses it gets `--debug-calls` output for free:

```python
# Any CLI command that needs a raw HTTP call before RegistryClient exists:
from regshape.libs.decorators import http_request

response = http_request(url, "GET", headers=auth_headers, timeout=10)
```

This ensures there is no manual debug logging code anywhere in `cli/`; the decorator is the single mechanism for all HTTP debug output. Once `RegistryClient` is implemented, `http_request` is retired and `@debug_call` is applied directly to `RegistryClient.request()`.

### Where Decorators Are Applied

| Decorator | Applied To | Layer |
|-----------|-----------|-------|
| `@track_time` | Individual domain operation functions (`get_manifest`, `head_blob`, `list_tags`, etc.) | Domain Operations |
| `@track_scenario` | Multi-step workflow functions (`upload_blob_chunked`, `upload_blob_monolithic`, `mount_blob`, etc.) | Domain Operations |
| `@debug_call` | `RegistryClient.request()` | Foundation (Transport) |

### Output Destination

All telemetry output goes to **stderr** so it does not interfere with the structured output on stdout (plain text or JSON). This ensures that piping `regshape manifest get ... --json` to another tool works correctly even when `--time-methods`, `--time-scenarios`, or `--debug-calls` is active.

### Design Principles

1. **Minimal overhead when disabled.** When the corresponding CLI flag is not set, decorators perform only a lightweight wrapper call and configuration check, skipping all timing and I/O work.
2. **Separate from logging middleware.** The `LoggingMiddleware` in the transport layer is for recording full request/response pairs (potentially to a file) for break mode analysis. The `@debug_call` decorator is for interactive inspection during CLI use.
3. **Composable.** A function can have both `@track_time` and `@debug_call` applied simultaneously.
4. **No side effects on return values.** Decorators never modify the arguments or return values of the decorated function.

---

## 9. Implementation Sequence

The recommended order for implementing RegShape, based on dependencies.

### Phase 1: Foundation

1. **`libs/models/`** -- Data models (Descriptor, ImageManifest, ImageIndex, media type constants, OciErrorResponse). No external dependencies.
2. **`libs/errors.py`** -- Extend the existing error hierarchy with all RegistryError subtypes.
3. **`libs/decorators/`** -- TelemetryConfig, `@track_time`, `@track_scenario`, `@debug_call` decorators. No dependencies beyond standard library.
4. **`libs/transport/`** -- RegistryClient, RegistryRequest, RegistryResponse, middleware protocol, AuthMiddleware (wiring in existing `libs/auth/`), LoggingMiddleware. Apply `@debug_call` to `RegistryClient.request()`.

### Phase 2: Domain Operations

5. **`libs/manifests/`** -- GET/HEAD/PUT/DELETE manifest operations. First domain module; validates the transport layer works end-to-end. Apply `@track_time` to individual operations and `@track_scenario` to multi-step workflows.
6. **`libs/blobs/`** -- GET/HEAD/DELETE blob, plus monolithic upload, chunked upload, and cross-repo mount. Apply `@track_time` to atomic operations, `@track_scenario` to `upload_blob_chunked`, `upload_blob_monolithic`, and `mount_blob`.
7. **`libs/tags/`** -- Tag listing with pagination.
8. **`libs/referrers/`** -- Referrer listing with filtering.
9. **`libs/catalog/`** -- Catalog listing with pagination.

### Phase 3: CLI

10. **`cli/main.py`** -- Top-level group, global options (including `--time-methods`, `--time-scenarios`, and `--debug-calls`), client construction, `TelemetryConfig` setup via `configure_telemetry()`.
11. **`cli/manifest.py`** through **`cli/catalog.py`** -- Command groups wired to domain operations.
12. **`cli/formatting.py`** -- Plain text and JSON output helpers.

### Phase 4: Break Mode

13. **`libs/breakmode/`** -- BreakModeConfig, BreakRule, rule factories, BreakModeMiddleware.
14. **CLI `--break` wiring** -- Loading break rules from file, injecting BreakModeConfig into TransportConfig.

### Phase 5: Polish

15. Pagination helpers (for tags, catalog, referrers).
16. Docker config credential auto-discovery in CLI, building on the auth helpers in `libs/auth/dockerconfig.py` and `libs/auth/dockercredstore.py` once those modules are implemented.
17. Comprehensive test suite with mocked HTTP responses.

---

## Open Questions

- [ ] Should break mode rules support a YAML/JSON file format for CLI use, or only programmatic configuration? (This spec assumes both: programmatic via Python, file-based via CLI.)
- [ ] Should the transport layer support async (`aiohttp`) in addition to synchronous `requests`? (This spec assumes synchronous only for v1, with the middleware pattern being portable to async later.)
- [ ] Should `RegistryClient` manage a `requests.Session` for connection pooling and cookie persistence, or create fresh connections per request? (Recommendation: use a Session for connection reuse.)
- [ ] Should chunked blob uploads support configurable chunk size? (Recommendation: yes, with a sensible default like 5MB.)
- [ ] Registry and image reference semantics: The current CLI does not expose a global `--registry` option. Manifest commands accept a single `--image-ref` that MUST be fully qualified, including the registry host[:port] (for example, `registry.example.com/repo:tag`), and the registry is always derived from this string (as in `_parse_image_ref(..., None)`). If we later introduce a separate `--registry` flag (or per-command registry options), we must define how it interacts with `--image-ref` and any `--repo` parameters to avoid ambiguous registry selection (for example, disallowing both an explicit registry flag and an embedded registry at the same time, and requiring `--repo` to omit the registry component).

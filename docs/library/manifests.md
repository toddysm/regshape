# Manifest operations

<!-- doc-type: reference -->

## Overview

`regshape.libs.manifests` provides registry manifest operations independently of
the CLI. The defaults below target the 0.2 development interface. Authenticate
separately with [the authentication API](auth.md) or `regshape auth login`
before accessing a private registry.

## Protocol defaults

```python
from regshape.libs.manifests import (
    DEFAULT_MANIFEST_ACCEPT,
    get_manifest,
    head_manifest,
    push_manifest,
)
from regshape.libs.transport import RegistryClient, TransportConfig

client = RegistryClient(TransportConfig(registry="registry.example.com"))
body, media_type, digest = get_manifest(client, "team/image", "v1")
digest, media_type, size = head_manifest(client, "team/image", "v1")
```

Replace the synthetic registry/repository/tag before executing these network
calls. The caller owns the client.

### `get_manifest` and `head_manifest`

Both operations accept `accept: str | None = None`. Omission or `None` selects
`DEFAULT_MANIFEST_ACCEPT`: the comma-joined, sorted `ALL_MANIFEST_MEDIA_TYPES`
set of supported OCI and Docker manifest/index types.

`get_manifest` returns `(body_text, content_type, digest)`; `head_manifest`
returns `(digest, media_type, size)` without downloading the body. These
operations do not verify the registry's reported digest.

### `push_manifest`

```python
from pathlib import Path

# Publish an existing manifest file under a new tag. This writes remotely.
payload = Path("manifest.json").read_bytes()
published_digest = push_manifest(
    client, "team/image", "copy", payload,
)
```

Place a valid manifest in `manifest.json` before running this example.
`content_type: str | None = None` uses the JSON object's `mediaType` field.
A missing field, invalid/undecodable JSON or a non-object JSON body falls back
to `application/vnd.oci.image.manifest.v1+json`. Inference does not rewrite
the supplied bytes or validate/normalize a present field value.

The return value is the registry's `Docker-Content-Digest` header (possibly
empty). For byte-verified publication, pass `expected_digest`; see the
[strict publication contract](../../specs/operations/manifests.md#push_manifest).

### Explicit overrides

```python
head_manifest(client, "team/image", "v1", accept="application/x.test")
push_manifest(client, "team/image", "probe", b"invalid JSON", content_type="")
```

Explicit header strings, including empty strings, are sent verbatim and never
replaced by defaults. This supports deliberate non-conformant requests;
registry errors raise `ManifestError`, authentication errors retain
`AuthError`, and transport exceptions propagate. Strict publication still
applies its input/acknowledgement checks when `expected_digest` is supplied.

See the [complete operations reference](../../specs/operations/manifests.md)
and [transfer observations](transfer-progress.md).

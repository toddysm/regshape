"""Synthetic byte-exact registries and layout writers for artifact boundary tests.

No sockets, credentials or external registries are used. Response raw streams
exercise the actual bounded receive primitives and record requested read sizes.
"""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import requests
from requests.structures import CaseInsensitiveDict
from urllib3.response import HTTPResponse

from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST


def address(raw: bytes, algorithm: str = "sha256") -> str:
    """Calculate the strict content address used in synthetic descriptor fixtures."""
    return f"{algorithm}:{hashlib.new(algorithm, raw).hexdigest()}"


def edge(raw: bytes, media_type: str = "application/octet-stream", **extra: object) -> dict:
    """Build a strict descriptor, allowing deliberate negative-field overrides."""
    return {"mediaType": media_type, "digest": address(raw), "size": len(raw), **extra}


def document(
    *, children: list[dict] | None = None, config: dict | None = None,
    layers: list[dict] | None = None, subject: dict | None = None,
    media_type: str | None = None,
) -> bytes:
    """Serialize a schema-2 fixture with deliberately noncanonical whitespace and unknown metadata."""
    body = {
        "schemaVersion": 2,
        "mediaType": media_type or (OCI_INDEX if children is not None else OCI_MANIFEST),
        "unrecognized": {"retained": "雪", "quoted": '[[{"}]'},
    }
    if children is not None:
        body["manifests"] = children
    else:
        body["config"] = config
        body["layers"] = layers or []
    if subject is not None:
        body["subject"] = subject
    return json.dumps(body, indent=3, ensure_ascii=False).encode("utf-8") + b"\n"


class StreamResponse(requests.Response):
    """A real Requests response backed by a bounded observable urllib3 byte stream."""

    def __init__(
        self, content: bytes, *, status: int = 200, headers: dict | None = None,
    ) -> None:
        """Create an unread entity stream and counters without opening a socket."""
        super().__init__()
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.raw = HTTPResponse(body=io.BytesIO(content), preload_content=False)
        self.close_calls = 0

    def close(self) -> None:
        """Count close attempts while retaining Requests' real ownership behavior."""
        self.close_calls += 1
        super().close()


class Registry:
    """Borrowable fake registry whose path log exposes ordering and forbidden discovery."""

    def __init__(self, root: bytes, objects: dict[str, bytes] | None = None) -> None:
        """Install root under a moving tag and its immutable address."""
        self.config = SimpleNamespace(registry="registry.example")
        self.root = root
        self.objects = {address(root): root, **(objects or {})}
        self.calls: list[str] = []
        self.responses: list[StreamResponse] = []
        self.root_headers: dict[str, str] = {}
        self.headers_by_reference: dict[str, dict[str, str]] = {}
        self.after_get = None

    def get(self, path: str, **kwargs: object) -> StreamResponse:
        """Return exact bytes, distinguishing manifest/blob endpoints without referrer support."""
        assert kwargs["stream"] is True
        self.calls.append(path)
        reference = path.rsplit("/", 1)[-1]
        content = self.root if reference == "moving" else self.objects.get(reference)
        headers = (
            self.root_headers if reference == "moving"
            else self.headers_by_reference.get(reference, {})
        )
        response = StreamResponse(
            content if content is not None else b'{"errors":[{"code":"BLOB_UNKNOWN"}]}',
            status=200 if content is not None else 404, headers=headers,
        )
        self.responses.append(response)
        if self.after_get is not None:
            self.after_get(path)
        return response


def nested_registry() -> tuple[Registry, bytes, dict[str, bytes]]:
    """Return all-platform/shared/subject/nested content with config/layer role reuse."""
    config = b'{"architecture":"opaque"}'
    layer = b"\x00opaque payload, not a tar file\xff"
    subject = document(children=[])
    first = document(config=edge(config), layers=[edge(layer)], subject=edge(subject, OCI_INDEX))
    second = document(config=edge(config), layers=[edge(layer), edge(config)])
    nested = document(children=[edge(first, OCI_MANIFEST), edge(second, OCI_MANIFEST)])
    root = document(children=[
        edge(nested, OCI_INDEX, platform={"os": "linux", "architecture": "amd64"}),
        edge(first, OCI_MANIFEST, platform={"os": "windows", "architecture": "arm64"}),
    ], subject=edge(subject, OCI_INDEX))
    objects = {address(value): value for value in (root, nested, first, second, subject, config, layer)}
    return Registry(root, objects), root, objects


def write_layout(path: Path, root: bytes, objects: dict[str, bytes], *, root_type: str = OCI_INDEX) -> None:
    """Write a synthetic source layout, never using production storage helpers to build expected data."""
    path.mkdir()
    (path / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    (path / "index.json").write_text(json.dumps({
        "schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [edge(root, root_type)],
    }))
    for digest, raw in {address(root): root, **objects}.items():
        algorithm, encoded = digest.split(":")
        directory = path / "blobs" / algorithm
        directory.mkdir(parents=True, exist_ok=True)
        (directory / encoded).write_bytes(raw)

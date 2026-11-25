"""Deterministic stored layouts and observable read-only inspection boundaries.

Fixture construction may write/hash metadata. Inspection must only read reachable
metadata, never opaque payloads, credentials or a daemon. The BusyBox reference
retains verified public metadata bytes; its offline copy substitutes two-byte
payloads and adjusted sizes under the original names, not verified blob content.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST
from regshape.libs.artifacts.models import digest_parts
from regshape.libs.artifacts.storage import SafeDirectory
from regshape.libs.layout import inspection
from regshape.tests.artifact_fixtures import address, edge

FIXTURES = Path(__file__).parent / "fixtures/layout-inspection"
FAMILY = ("simple", "nested", "subject", "shared", "mixed", "incomplete", "busybox")
STRESS_COUNTS = {"wide": (1025, 3072, 3074), "reused-index": (66, 292, 19302)}
PAYLOAD = b"{}"


def raw(value: dict) -> bytes:
    """Serialize stable ASCII metadata, preserving source field and descriptor order."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def index(children: list[dict], **extra: object) -> bytes:
    """Build a schema-2 OCI index; explicit extensions belong to its source document."""
    return raw(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": children, **extra}
    )


def manifest(**extra: object) -> bytes:
    """Build a one-layer artifact with a shared opaque config/content address."""
    return raw(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "artifactType": "application/vnd.example.payload",
            "config": edge(PAYLOAD, "application/vnd.oci.empty.v1+json"),
            "layers": [edge(PAYLOAD)],
            **extra,
        }
    )


def busybox() -> tuple[bytes, dict[str, bytes]]:
    """Derive an offline local copy of pinned metadata without downloading any payload.

    Only descriptor sizes change to fit stand-ins and derived documents. Stored
    names, ordering, source annotations and platforms remain the captured ones.
    The separately retained reference contains the unmodified digest-verified bytes.
    :raises RuntimeError: If dependent size fields do not converge within ten passes.
    """
    reference = json.loads((FIXTURES / "busybox-metadata.json").read_text())
    documents = {
        digest: json.loads(value) for digest, value in reference["documents"].items()
    }
    objects = {
        descriptor["digest"]: PAYLOAD
        for document in documents.values()
        for descriptor in [document.get("config"), *document.get("layers", [])]
        if descriptor is not None
    }
    objects.update({digest: raw(value) for digest, value in documents.items()})
    for _ in range(10):
        before = objects.copy()
        for digest, document in documents.items():
            descriptors = [
                document.get("config"),
                document.get("subject"),
                *document.get("layers", []),
                *document.get("manifests", []),
            ]
            for descriptor in descriptors:
                if descriptor is not None:
                    descriptor["size"] = len(objects[descriptor["digest"]])
            objects[digest] = raw(document)
        if objects == before:
            break
    else:
        raise RuntimeError("BusyBox offline metadata sizes did not converge")
    root = reference["root"]
    roots = [
        {
            "mediaType": documents[root]["mediaType"],
            "digest": root,
            "size": len(objects[root]),
            "annotations": {"org.opencontainers.image.ref.name": "1.36"},
        },
        *[
            {"mediaType": OCI_MANIFEST, "digest": digest, "size": len(objects[digest])}
            for digest, document in documents.items()
            if "subject" in document
        ],
    ]
    return index(roots), objects


def build_layout(path: Path, shape: str) -> Path:
    """Write one stable fixture into a nonexistent directory; propagate IO failures.

    :param shape: A named FAMILY member or approved wide/reused-index stress shape.
    :raises ValueError: For an unknown shape before creating the directory.
    """
    first = manifest(annotations={"fixture": "first"})
    second = manifest(annotations={"fixture": "second"}, layers=[edge(PAYLOAD)] * 2)
    objects = {address(value): value for value in (PAYLOAD, first)}
    roots = [edge(first, OCI_MANIFEST)]
    if shape == "nested":
        nested = index(
            [
                edge(first, OCI_MANIFEST),
                edge(
                    first,
                    OCI_MANIFEST,
                    annotations={"edge": "nested"},
                    platform={"os": "linux", "architecture": "arm64"},
                ),
            ]
        )
        objects[address(nested)] = nested
        roots = [
            edge(nested, OCI_INDEX),
            edge(
                first,
                OCI_MANIFEST,
                annotations={"edge": "root"},
                platform={"os": "linux", "architecture": "amd64"},
            ),
        ]
    elif shape == "subject":
        subject_only = manifest(annotations={"fixture": "subject-only"})
        second = manifest(subject=edge(subject_only, OCI_MANIFEST))
        objects.update({address(second): second, address(subject_only): subject_only})
        roots.append(edge(second, OCI_MANIFEST))
    elif shape == "shared":
        objects[address(second)] = second
        roots = [edge(first, OCI_MANIFEST), edge(second, OCI_MANIFEST)] * 3
    elif shape == "mixed":
        unknown = b"not metadata; never parsed"
        objects.update({address(second): second, address(unknown): unknown})
        roots += [
            edge(unknown, "application/vnd.example.unknown"),
            edge(second, OCI_MANIFEST),
        ]
    elif shape == "incomplete":
        objects.pop(address(PAYLOAD))
        roots *= 3
    elif shape == "busybox":
        entry, objects = busybox()
        roots = json.loads(entry)["manifests"]
    elif shape in STRESS_COUNTS:
        manifests = [
            manifest(annotations={"fixture": str(n)})
            for n in range(1024 if shape == "wide" else 64)
        ]
        objects = {address(value): value for value in [PAYLOAD, *manifests]}
        roots = [edge(value, OCI_MANIFEST) for value in manifests]
        if shape == "reused-index":
            nested = index(roots)
            objects[address(nested)] = nested
            roots = [edge(nested, OCI_INDEX)] * 100
    elif shape != "simple":
        raise ValueError(f"Unknown layout fixture: {shape}")
    path = path.resolve()
    path.mkdir()
    (path / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    (path / "index.json").write_bytes(index(roots))
    for digest, value in objects.items():
        algorithm, name = digest_parts(digest)
        directory = path / "blobs" / algorithm
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(value)
    return path


def metadata_files(path: Path) -> dict[str, int]:
    """Inventory fixture metadata before observation, excluding opaque/unlisted objects.

    :raises ValueError: If a descriptor digest is not a safe supported content address.
    """
    found = {
        name: (path / name).stat().st_size for name in ("oci-layout", "index.json")
    }
    pending = [json.loads((path / "index.json").read_bytes())]
    seen: set[str] = set()
    while pending:
        document = pending.pop()
        for descriptor in document.get("manifests", []):
            digest = descriptor["digest"]
            if digest in seen or descriptor["mediaType"] not in (
                OCI_INDEX,
                OCI_MANIFEST,
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ):
                continue
            seen.add(digest)
            algorithm, name = digest_parts(digest)
            target = path / "blobs" / algorithm / name
            found[name] = target.stat().st_size
            pending.append(json.loads(target.read_bytes()))
    return found


@dataclass
class IOFacts:
    """Instrumentation totals for one invocation, keyed by local name/source document.

    Stat includes handle-verification calls; per-address caching refers to opaque
    target stats and metadata reads/parses, not every security identity recheck.
    """

    opens: int = 0
    stats: int = 0
    metadata_opens: Counter[str] = field(default_factory=Counter)
    reads: Counter[str] = field(default_factory=Counter)
    parses: Counter[str] = field(default_factory=Counter)
    target_stats: Counter[str] = field(default_factory=Counter)
    metadata_bytes: int = 0


class _Stream:
    """Borrow a real binary stream while counting only returned metadata bytes."""

    def __init__(self, stream: BinaryIO, name: str, facts: IOFacts) -> None:
        """Keep ownership with the surrounding SafeFile context."""
        self.stream, self.name, self.facts = stream, name, facts

    def fileno(self) -> int:
        """Expose the unchanged file descriptor for production identity checks."""
        return self.stream.fileno()

    def read(self, size: int) -> bytes:
        """Record bounded calls including EOF; propagate original read failures."""
        self.facts.reads[self.name] += 1
        value = self.stream.read(size)
        self.facts.metadata_bytes += len(value)
        return value


@contextmanager
def observe_io(metadata: dict[str, int]) -> Iterator[IOFacts]:
    """Reject payload opens/writes and count native IO during one SDK/CLI operation.

    Hash/decode/network/daemon/credential calls fail immediately. Capability sets
    temporarily include patched native functions so safe-open checks stay honest.
    Fixture discovery and post-operation preservation checks belong outside here.
    """
    facts = IOFacts()
    real_open, real_stat, real_read = os.open, os.stat, SafeDirectory.read_file
    real_json = inspection._json
    metadata_fds: set[int] = set()

    def opened(name, flags, *args, **kwargs):
        """Refuse write flags and non-directory opaque/unlisted file opens."""
        assert not flags & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        )
        if not flags & os.O_DIRECTORY:
            assert Path(name).name in metadata, f"Forbidden payload open: {name}"
        facts.opens += 1
        fd = real_open(name, flags, *args, **kwargs)
        if not flags & os.O_DIRECTORY:
            metadata_fds.add(fd)
        return fd

    def guarded_file_open(factory):
        """Guard Python factories, including pathlib's cached opener on Python 3.10."""

        def file_open(file, mode="r", *args, **kwargs):
            """Permit metadata reads or standard output wrappers, rejecting payload/unlisted files."""
            if isinstance(file, int) and file in (1, 2):
                return factory(file, mode, *args, **kwargs)
            assert not any(flag in mode for flag in "wax+"), "Forbidden file write"
            if isinstance(file, int):
                assert file in metadata_fds, "Forbidden unobserved file descriptor"
            else:
                name = Path(file).name
                assert name in metadata, f"Forbidden payload open: {file}"
                facts.opens += 1
                facts.metadata_opens[name] += 1
            return factory(file, mode, *args, **kwargs)

        return file_open

    def stated(name, *args, **kwargs):
        """Keep actual no-follow stats, including cached opaque target observations."""
        facts.stats += 1
        if isinstance(name, str) and len(name) in (64, 128):
            facts.target_stats[name] += 1
        return real_stat(name, *args, **kwargs)

    @contextmanager
    def read(directory, name):
        """Allow only known reachable metadata and retain SafeFile close semantics."""
        assert name in metadata, f"Forbidden payload read: {name}"
        facts.metadata_opens[name] += 1
        with real_read(directory, name) as stream:
            yield _Stream(stream, name, facts)

    def parsed(value, limits, source):
        """Count actual strict parser invocations, including the marker/entry point."""
        facts.parses[source.document] += 1
        return real_json(value, limits, source)

    with ExitStack() as stack:
        for target in (
            "hashlib.new",
            "hashlib.sha256",
            "socket.create_connection",
            "requests.sessions.Session.request",
            "subprocess.Popen",
            "docker.from_env",
            "regshape.libs.auth.credentials.resolve_credentials",
            "regshape.libs.transport.client.resolve_credentials",
            "gzip.decompress",
            "tarfile.open",
            "os.mkdir",
            "os.unlink",
            "os.rename",
            "os.chmod",
        ):
            stack.enter_context(
                patch(target, side_effect=AssertionError(f"Forbidden: {target}"))
            )
        stack.enter_context(patch.object(os, "open", opened))
        stack.enter_context(
            patch.object(builtins, "open", guarded_file_open(builtins.open))
        )
        python_open = guarded_file_open(io.open)
        stack.enter_context(patch.object(io, "open", python_open))
        stack.enter_context(patch.object(Path, "open", python_open))
        stack.enter_context(patch.object(os, "stat", stated))
        stack.enter_context(
            patch.object(os, "supports_dir_fd", os.supports_dir_fd | {opened, stated})
        )
        stack.enter_context(
            patch.object(
                os, "supports_follow_symlinks", os.supports_follow_symlinks | {stated}
            )
        )
        stack.enter_context(patch.object(SafeDirectory, "read_file", read))
        stack.enter_context(patch.object(inspection, "_json", parsed))
        yield facts

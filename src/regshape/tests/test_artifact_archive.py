"""Synthetic archive framing, closure, resource and native-publication regressions.

Fixtures use the standard tar/gzip writers independently of production parsing.
No registry sockets, credentials, payload extraction or large data allocations
are involved. Native tests run on Linux/Darwin; Windows exercises explicit guards.
"""

import dataclasses
import errno
import gzip
import io
import json
import os
import struct
import tarfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.artifacts import (
    ArchiveSnapshot,
    ArtifactError,
    ArtifactFormat,
    ArtifactLimits,
    DirectorySnapshot,
    archive,
    pull_artifact,
    snapshot_layout,
)
from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST
from regshape.libs.artifacts.native import NativeFilesystem
from regshape.libs.artifacts.storage import (
    CHUNK_SIZE,
    SafeDirectory,
    require_safe_filesystem,
)
from regshape.libs.layout import validate_layout
from regshape.tests.artifact_fixtures import (
    Registry,
    address,
    document,
    edge,
    nested_registry,
)

MARKER = b'{"imageLayoutVersion":"1.0.0"}'
ZERO = bytes(512)


@pytest.fixture(autouse=True)
def supported_archive(request: pytest.FixtureRequest) -> None:
    """Run platform-independent parser/guard cases everywhere and skip native work where unavailable."""
    if "unsupported_platform" in request.node.name or "raw_" in request.node.name:
        return
    try:
        require_safe_filesystem()
        NativeFilesystem()
    except ArtifactError:
        pytest.skip("Safe artifact operations deliberately unsupported on this platform")


def _files(*, sha512: bool = False) -> dict[str, bytes]:
    """Return a complete single-manifest closure with noncanonical byte and wrapper metadata."""
    blob = b"\xffopaque config\x00"
    digest = address(blob, "sha512" if sha512 else "sha256")
    root = document(config=edge(blob, digest=digest), layers=[edge(blob, digest=digest)])
    root_digest = address(root, "sha512" if sha512 else "sha256")
    wrapper = json.dumps({
        "schemaVersion": 2, "mediaType": OCI_INDEX,
        "manifests": [edge(
            root, OCI_MANIFEST, digest=root_digest,
            annotations={"org.opencontainers.image.ref.name": "source", "retained": "雪"},
            platform={"os": "other", "architecture": "unknown"}, extra={"untouched": True},
        )],
        "unknown": ["preserved"],
    }, ensure_ascii=False, indent=4).encode()
    return {
        "oci-layout": MARKER, "index.json": wrapper,
        "blobs/" + root_digest.replace(":", "/"): root,
        "blobs/" + digest.replace(":", "/"): blob,
    }


def _tar(files: dict[str, bytes], *, directories: bool = False) -> bytes:
    """Use an independent POSIX writer for complete fixture archives, including optional directories."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as writer:
        if directories:
            for name in ("blobs/", "blobs/sha256/", "blobs/sha512/"):
                item = tarfile.TarInfo(name)
                item.type = tarfile.DIRTYPE
                writer.addfile(item)
        for name, value in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(value)
            writer.addfile(item, io.BytesIO(value))
    return stream.getvalue()


def _source(tmp_path: Path, raw: bytes, *, compressed: bool = False) -> Path:
    """Write one fixture source; never let fixture creation use production archive helpers."""
    path = tmp_path / "input.data"
    path.write_bytes(raw if compressed else gzip.compress(raw, mtime=0))
    return path


def _header(name: str = "oci-layout", *, size: int = 0, kind: bytes = b"0") -> bytes:
    """Build one independent raw USTAR header with controlled declared size and member type."""
    item = tarfile.TarInfo(name)
    item.size, item.type = size, kind
    return item.tobuf(format=tarfile.USTAR_FORMAT)


def _changed_header(raw: bytes, start: int, value: bytes) -> bytes:
    """Change one fixed header field and repair checksum so a test reaches its intended guard."""
    header = bytearray(raw)
    header[start:start + len(value)] = value
    header[148:156] = b"        "
    header[148:156] = f"{sum(header):06o}\0 ".encode()
    return bytes(header)


def _pax(key: str, value: str) -> bytes:
    """Build a POSIX record independently using a converging decimal length."""
    record = (key + "=" + value + "\n").encode()
    length = len(record) + 2
    while len(str(length)) + 1 + len(record) != length:
        length = len(str(length)) + 1 + len(record)
    return str(length).encode() + b" " + record


def _extension(body: bytes) -> bytes:
    """Frame an arbitrary per-file extension body, including deliberately invalid record content."""
    return _header("PaxHeader", size=len(body), kind=b"x") + body + bytes(-len(body) % 512)


def _custom_gzip(raw: bytes, optional: bytes, flags: int) -> bytes:
    """Attach arbitrary gzip optional fields to a valid independent deflate/trailer fixture."""
    basic = gzip.compress(raw, mtime=0)
    fixed = basic[:3] + bytes([flags]) + basic[4:10]
    return fixed + optional + basic[10:]


def _pull(tmp_path: Path, *, limits: ArtifactLimits | None = None) -> Path:
    """Pull a real synthetic registry through the public archive output API."""
    output = tmp_path / "output.gz"
    registry, _, _ = nested_registry()
    result = pull_artifact(
        registry, "repo", "moving", output,
        output_format=ArtifactFormat.OCI_TAR_GZIP, limits=limits,
    )
    assert result.output_format == ArtifactFormat.OCI_TAR_GZIP
    return output


def test_archive_directory_raw_closure_parity(tmp_path: Path) -> None:
    """Directory and gzip-tar preserve identical closure bytes, counters, root identity and wrapper."""
    registry, root, objects = nested_registry()
    directory = tmp_path / "layout"
    first = pull_artifact(registry, "repo", "moving", directory)
    registry, _, _ = nested_registry()
    second = pull_artifact(
        registry, "repo", "moving", tmp_path / "archive",
        output_format=ArtifactFormat.OCI_TAR_GZIP,
    )
    assert first.root == second.root
    for field in ("indexes", "manifests", "blobs", "bytes_stored", "bytes_downloaded", "reused_references"):
        assert getattr(first, field) == getattr(second, field)
    with ArchiveSnapshot(second.output_path) as snapshot, DirectorySnapshot(directory) as original:
        assert snapshot.wrapper_bytes == original.wrapper_bytes
        assert snapshot.graph == original.graph
        assert {item.digest for item in snapshot.graph.objects} == set(objects)
        for item in snapshot.graph.objects:
            assert snapshot.store.verify(item.digest, item.size, cap=item.size + 1) == objects[item.digest]
        assert snapshot.graph.objects[-1].digest == address(root)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["archive", "layout"]


def test_output_order_metadata_and_gzip_are_deterministic(tmp_path: Path) -> None:
    """Repeated pulls normalize tar ownership/modes/order and gzip time without leaking staging names."""
    first = _pull(tmp_path)
    data = first.read_bytes()
    first.unlink()
    assert _pull(tmp_path).read_bytes() == data
    assert data[3] == 0 and data[4:8] == bytes(4)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as reader:
        names = reader.getnames()
        assert names[:2] == ["oci-layout", "index.json"]
        assert names[2:] == sorted(names[2:])
        for item in reader:
            assert item.isfile() and item.uid == item.gid == item.mtime == 0
            assert item.uname == item.gname == "" and item.mode == 0o644
            assert "regshape" not in item.name and not item.name.startswith("/")


@pytest.mark.parametrize("sha512", [False, True])
@pytest.mark.parametrize("directories", [False, True])
def test_snapshot_original_wrapper_and_source_lifetime(
    tmp_path: Path, sha512: bool, directories: bool,
) -> None:
    """Independent archives support both digest algorithms and detach mutable source bytes completely."""
    files = _files(sha512=sha512)
    source = _source(tmp_path, _tar(files, directories=directories))
    with snapshot_layout(source) as snapshot:
        assert isinstance(snapshot, ArchiveSnapshot)
        assert snapshot.wrapper_bytes == files["index.json"]
        assert not snapshot.namespace.exists("archive.tar")
        source.write_bytes(b"mutated after verification")
        source.unlink()
        for item in snapshot.graph.objects:
            path = "blobs/" + item.digest.replace(":", "/")
            assert snapshot.store.verify(item.digest, item.size, cap=item.size + 1) == files[path]
    with pytest.raises(ArtifactError, match="closed"):
        snapshot.store.verify(snapshot.graph.objects[0].digest, snapshot.graph.objects[0].size)


def test_archive_recursive_validate_cli_and_shallow_rejection(tmp_path: Path) -> None:
    """Recursive library/CLI accept archives; shallow validation never silently handles or ignores them."""
    source = _source(tmp_path, _tar(_files()))
    validate_layout(source, recursive=True)
    runner = CliRunner()
    result = runner.invoke(regshape, ["layout", "validate", "--recursive", "--path", str(source)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
    result = runner.invoke(regshape, ["layout", "validate", "--path", str(source)])
    assert result.exit_code != 0 and "directory" in result.output
    result = runner.invoke(regshape, [
        "layout", "validate", "--recursive", "--max-archive-input-bytes", "1", "--path", str(source),
    ])
    assert result.exit_code != 0 and "max_archive_input_bytes" in result.output


@pytest.mark.parametrize("field", ["max_archive_bytes", "max_archive_input_bytes", "max_archive_entries"])
def test_import_archive_budgets_exact_and_plus_one(tmp_path: Path, field: str) -> None:
    """Every independently charged archive budget permits equality and rejects the next byte/header."""
    files = _files()
    raw = _tar(files, directories=True)
    source = _source(tmp_path, raw)
    counts = {
        "max_archive_bytes": len(raw), "max_archive_input_bytes": source.stat().st_size,
        "max_archive_entries": len(files) + 3,
    }
    cap = counts[field]
    with ArchiveSnapshot(source, limits=ArtifactLimits(**{field: cap})):
        pass
    with pytest.raises(ArtifactError, match=field), ArchiveSnapshot(
        source, limits=ArtifactLimits(**{field: cap - 1}),
    ):
        pytest.fail("Boundary+1 archive entered")


@pytest.mark.parametrize("field", ["max_archive_bytes", "max_archive_input_bytes", "max_archive_entries"])
def test_output_archive_budgets_exact_and_plus_one(tmp_path: Path, field: str) -> None:
    """Writer budget accounting includes framing and never publishes one-byte/one-header overflow."""
    output = _pull(tmp_path)
    data = output.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as reader:
        count = len(reader.getmembers())
    counts = {
        "max_archive_bytes": len(gzip.decompress(data)), "max_archive_input_bytes": len(data),
        "max_archive_entries": count,
    }
    output.unlink()
    _pull(tmp_path, limits=ArtifactLimits(**{field: counts[field]})).unlink()
    with pytest.raises(ArtifactError, match=field):
        _pull(tmp_path, limits=ArtifactLimits(**{field: counts[field] - 1}))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flag,optional", [
    (0, b""), (4, b"\x03\0abc"), (8, b"file\0"), (16, b"comment\0"),
    (28, b"\x02\0abfile\0comment\0"),
])
def test_raw_gzip_optional_header_budget(flag: int, optional: bytes) -> None:
    """Every optional field, including its delimiter/length word, belongs to the header budget."""
    raw = 2 * ZERO
    compressed = _custom_gzip(raw, optional, flag)
    limit = 10 + len(optional)
    result = io.BytesIO()
    assert archive._inflate(io.BytesIO(compressed), result, ArtifactLimits(max_gzip_header_bytes=limit)) == len(raw)
    assert result.getvalue() == raw
    with pytest.raises(ArtifactError, match="max_gzip_header_bytes"):
        archive._inflate(io.BytesIO(compressed), io.BytesIO(), ArtifactLimits(max_gzip_header_bytes=limit - 1))


def test_raw_gzip_header_crc_valid_and_invalid() -> None:
    """The optional header checksum is validated independently of the payload trailer checksum."""
    basic = gzip.compress(2 * ZERO, mtime=0)
    fixed = basic[:3] + b"\x02" + basic[4:10]
    checksum = struct.pack("<H", zlib.crc32(fixed) & 0xFFFF)
    valid = fixed + checksum + basic[10:]
    archive._inflate(io.BytesIO(valid), io.BytesIO(), ArtifactLimits(max_gzip_header_bytes=12))
    with pytest.raises(ArtifactError, match="max_gzip_header_bytes"):
        archive._inflate(io.BytesIO(valid), io.BytesIO(), ArtifactLimits(max_gzip_header_bytes=11))
    with pytest.raises(ArtifactError, match="header CRC"):
        archive._inflate(io.BytesIO(fixed + b"XX" + basic[10:]), io.BytesIO(), ArtifactLimits())


@pytest.mark.parametrize("suffix", [b"\0", b"garbage", gzip.compress(b"", mtime=0), gzip.compress(ZERO, mtime=0)])
def test_raw_gzip_rejects_all_trailing_data_and_members(suffix: bytes) -> None:
    """Even zero padding and empty additional gzip members are forbidden."""
    with pytest.raises(ArtifactError, match="Trailing data"):
        archive._inflate(io.BytesIO(gzip.compress(2 * ZERO, mtime=0) + suffix), io.BytesIO(), ArtifactLimits())


@pytest.mark.parametrize("variant", ["flags", "method", "crc", "isize", "truncated", "deflate", "empty-members"])
def test_raw_gzip_malformed_framing(variant: str) -> None:
    """Bad framing/trailer/deflate and empty-member amplification fail without accepting a prefix."""
    data = bytearray(gzip.compress(2 * ZERO, mtime=0))
    if variant == "flags":
        data[3] = 0xE0
    elif variant == "method":
        data[2] = 0
    elif variant == "crc":
        data[-8] ^= 1
    elif variant == "isize":
        data[-4] ^= 1
    elif variant == "truncated":
        data = data[:-3]
    elif variant == "deflate":
        data[10] = 0xFF
    else:
        data = bytearray(gzip.compress(b"", mtime=0) * 10000)
    with pytest.raises(ArtifactError):
        archive._inflate(io.BytesIO(data), io.BytesIO(), ArtifactLimits())


@pytest.mark.parametrize("flag,optional", [(8, b"a" * 100000), (16, b"a" * 100000), (4, b"\xff\xff")])
def test_raw_gzip_huge_optional_header_stops_before_reading_body(flag: int, optional: bytes) -> None:
    """An overlong name/comment/extra field cannot consume the large compressed-input budget."""
    source = io.BytesIO(_custom_gzip(b"", optional, flag))
    with pytest.raises(ArtifactError, match="max_gzip_header_bytes"):
        archive._inflate(source, io.BytesIO(), ArtifactLimits(max_gzip_header_bytes=20))
    assert source.tell() <= 20


def test_raw_gzip_bomb_never_writes_over_expanded_cap() -> None:
    """A tiny deflate bomb is decoded with bounded allocation and rejected before overflow reaches disk."""
    source = io.BytesIO(gzip.compress(bytes(4 * CHUNK_SIZE), mtime=0))
    target = io.BytesIO()
    with pytest.raises(ArtifactError, match="max_archive_bytes"):
        archive._inflate(source, target, ArtifactLimits(max_archive_bytes=CHUNK_SIZE))
    assert len(target.getvalue()) == CHUNK_SIZE


@pytest.mark.parametrize("kind", [b"1", b"2", b"3", b"4", b"6", b"7", b"S", b"L", b"K", b"g", b"V"])
def test_raw_tar_forbids_links_devices_sparse_and_global_extensions(kind: bytes) -> None:
    """Only regular files, directories and bounded local POSIX attributes reach data copying."""
    raw = _header(kind=kind) + 2 * ZERO
    with pytest.raises(ArtifactError, match="type/link/extension"):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())


@pytest.mark.parametrize("name", [
    "../oci-layout", "/oci-layout", "./oci-layout", "a/../oci-layout", "a//b",
    "C:/oci-layout", r"blobs\sha256\abc", "blobs/sha256/..", "blobs//abc",
    "blobs/sha1/" + "a" * 40, "blobs/sha256/" + "A" * 64, "outer/oci-layout",
    "oci-layout/", ".regshape-incomplete", "blobs", "index.json/child",
])
def test_raw_tar_unsafe_and_nonlayout_names(name: str) -> None:
    """Unsafe path spelling and unsupported layouts are rejected, never normalized into valid paths."""
    item = tarfile.TarInfo(name)
    raw = item.tobuf(format=tarfile.PAX_FORMAT) + 2 * ZERO
    with pytest.raises(ArtifactError, match="path"):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())


@pytest.mark.parametrize("variant", [
    "checksum", "magic", "binary-size", "negative-size", "hidden-name", "linkname",
    "device-number", "tail", "truncated-header", "no-end", "one-end", "nonzero-end",
    "unaligned-end", "data-padding", "size-bounds", "directory-data",
])
def test_raw_tar_header_padding_and_bounds(variant: str) -> None:
    """Framing/profile failures are detected during raw preflight, before file creation."""
    header = _header()
    raw = header + 2 * ZERO
    changes = {
        "magic": (257, b"ustar "), "binary-size": (124, b"\x80"),
        "negative-size": (124, b"-0000000001\0"), "hidden-name": (20, b"x"),
        "linkname": (157, b"target"), "device-number": (329, b"0000001\0"),
        "tail": (500, b"x"),
    }
    if variant in changes:
        raw = _changed_header(header, *changes[variant]) + 2 * ZERO
    elif variant == "checksum":
        raw = b"X" + raw[1:]
    elif variant == "truncated-header":
        raw = header[:100]
    elif variant == "no-end":
        raw = header
    elif variant == "one-end":
        raw = header + ZERO
    elif variant == "nonzero-end":
        raw += b"x" + bytes(511)
    elif variant == "unaligned-end":
        raw += b"\0"
    elif variant == "data-padding":
        raw = _header(size=1) + b"x!" + bytes(510) + 2 * ZERO
    elif variant == "size-bounds":
        raw = _header(size=1000000) + 2 * ZERO
    elif variant == "directory-data":
        raw = _header("blobs/", size=1, kind=b"5") + bytes(512) + 2 * ZERO
    with pytest.raises(ArtifactError):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())


@pytest.mark.parametrize("name,kind", [("oci-layout", b"0"), ("blobs/", b"5")])
def test_raw_tar_duplicate_names(name: str, kind: bytes) -> None:
    """Duplicates fail even when both headers describe identical empty content."""
    raw = _header(name, kind=kind) * 2 + 2 * ZERO
    with pytest.raises(ArtifactError, match="Duplicate"):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())


@pytest.mark.parametrize("key", ["uid", "gid", "GNU.sparse.size", "GNU.sparse.map", "linkpath", "hdrcharset", "comment"])
def test_raw_tar_unsupported_pax_attributes(key: str) -> None:
    """Unknown/owner/sparse POSIX attributes never reach a permissive standard-library decoder."""
    raw = _extension(_pax(key, "1")) + _header() + 2 * ZERO
    with pytest.raises(ArtifactError, match="Unsupported POSIX"):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())


@pytest.mark.parametrize("body", [
    b"", b"999 path=oci-layout\n", b"0 path=x\n", b"12 path=x", b"x path=x\n",
    b"99999999999999999999999999999999999 path=x\n", b"10 path=x\n",
    _pax("path", "../oci-layout"), _pax("path", "oci-layout\0hidden"),
    _pax("path", "oci-layout") * 2, _pax("size", "-1"), _pax("size", "1.5"),
    _pax("mtime", "NaN"), _pax("mtime", "1e10"),
])
def test_raw_tar_invalid_pax_records(body: bytes) -> None:
    """Lengths, duplicate fields and unsafe effective paths are checked before data-offset use."""
    raw = _extension(body) + _header() + 2 * ZERO
    with pytest.raises(ArtifactError):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())


def test_raw_tar_extension_budgets_and_pending_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Extension bodies are capped before read; aggregate charges and raw entry counts are independent."""
    body = _pax("mtime", "-0.125") + _pax("size", "0") + _pax("path", "oci-layout")
    raw = _extension(body) + _header("ignored") + 2 * ZERO
    policy = ArtifactLimits(max_archive_metadata_bytes=len(body), max_archive_entries=2)
    result = archive._preflight(io.BytesIO(raw), len(raw), policy)
    assert result[0].name == "oci-layout" and result[0].size == 0
    for change, message in [
        ({"max_archive_metadata_bytes": len(body) - 1}, "max_archive_metadata_bytes"),
        ({"max_archive_entries": 1}, "max_archive_entries"),
    ]:
        with pytest.raises(ArtifactError, match=message):
            archive._preflight(io.BytesIO(raw), len(raw), dataclasses.replace(policy, **change))
    monkeypatch.setattr(archive, "_METADATA_TOTAL", len(body) - 1)
    with pytest.raises(ArtifactError, match="aggregate"):
        archive._preflight(io.BytesIO(raw), len(raw), policy)
    monkeypatch.setattr(archive, "_METADATA_TOTAL", 100000)
    for invalid in (_extension(body) + 2 * ZERO, _extension(body) * 2 + _header() + 2 * ZERO):
        with pytest.raises(ArtifactError, match="Dangling|Stacked"):
            archive._preflight(io.BytesIO(invalid), len(invalid), ArtifactLimits())


def test_raw_tar_huge_extension_is_rejected_before_body_read() -> None:
    """A giant declared local-extension allocation is rejected after exactly one raw header."""
    raw = _header("PaxHeader", size=8 * 1024**3 - 1, kind=b"x")
    source = io.BytesIO(raw)
    with pytest.raises(ArtifactError, match="max_archive_metadata_bytes"):
        archive._preflight(source, 16 * 1024**3, ArtifactLimits())
    assert source.tell() == 512


def test_raw_tar_many_empty_members_hit_header_budget() -> None:
    """Many zero-byte distinct files cannot bypass exact raw-header count boundaries."""
    raw = b"".join(_header("blobs/sha256/" + f"{index:064x}") for index in range(1000)) + 2 * ZERO
    members = archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits(max_archive_entries=1000))
    assert len(members) == 1000 and sum(item.size for item in members) == 0
    with pytest.raises(ArtifactError, match="max_archive_entries"):
        archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits(max_archive_entries=999))


def test_large_posix_size_and_sha512_offsets_use_sparse_spool(tmp_path: Path) -> None:
    """A real sparse >8-GiB tar proves size overrides drive seek offsets without allocating payload."""
    size = 8 * 1024**3 + 17
    name = "blobs/sha512/" + "a" * 128
    header, entries, metadata = archive._headers(name, size, ArtifactLimits())
    assert entries == 2 and metadata > 0
    path = tmp_path / "sparse.tar"
    with path.open("w+b") as spool:
        spool.write(header)
        spool.seek(size, os.SEEK_CUR)
        spool.write(bytes(-size % 512) + 2 * ZERO)
        total = spool.tell()
        result = archive._preflight(spool, total, ArtifactLimits())
    assert result == (archive._Member(name, size, len(header)),)
    assert path.stat().st_blocks * 512 < 1024 * 1024


@pytest.mark.parametrize("field,name", [
    ("max_layout_marker_bytes", "oci-layout"), ("max_manifest_bytes", "index.json"),
])
def test_archive_metadata_caps_include_whitespace(tmp_path: Path, field: str, name: str) -> None:
    """Marker/wrapper whitespace counts before JSON allocation, independently of content-byte limits."""
    files = _files()
    # The wrapper must also accommodate each required manifest.
    cap = max(len(raw) for raw in files.values()) + 10
    files[name] += b" " * (cap - len(files[name]))
    source = _source(tmp_path, _tar(files))
    with ArchiveSnapshot(source, limits=ArtifactLimits(**{field: cap})):
        pass
    with pytest.raises(ArtifactError, match=field), ArchiveSnapshot(
        source, limits=ArtifactLimits(**{field: cap - 1}),
    ):
        pytest.fail("Oversize metadata entered")


@pytest.mark.parametrize("variant", ["missing", "corrupt", "extra", "json-depth", "duplicate-json"])
def test_archive_graph_and_exact_closure(tmp_path: Path, variant: str) -> None:
    """Valid tar framing cannot excuse missing/corrupt objects, extra content or invalid graph metadata."""
    files = _files()
    blob = next(name for name in files if name.startswith("blobs/"))
    limits = ArtifactLimits()
    if variant == "missing":
        del files[blob]
    elif variant == "corrupt":
        files[blob] = b"x" * len(files[blob])
    elif variant == "extra":
        files["blobs/" + address(b"extra").replace(":", "/")] = b"extra"
    elif variant == "json-depth":
        limits = ArtifactLimits(max_json_depth=2)
    else:
        files["oci-layout"] = b'{"imageLayoutVersion":"1.0.0","imageLayoutVersion":"1.0.0"}'
    source = _source(tmp_path, _tar(files))
    with pytest.raises((ArtifactError, OSError)), ArchiveSnapshot(source, limits=limits):
        pytest.fail("Invalid graph entered")


@pytest.mark.parametrize("field", ["max_objects", "max_total_bytes"])
def test_archive_object_budgets_exact_and_plus_one(tmp_path: Path, field: str) -> None:
    """Distinct archive content reservations obey graph caps before any bulk copying."""
    files = _files()
    values = [raw for name, raw in files.items() if name.startswith("blobs/")]
    cap = len(values) if field == "max_objects" else sum(map(len, values))
    source = _source(tmp_path, _tar(files))
    with ArchiveSnapshot(source, limits=ArtifactLimits(**{field: cap})):
        pass
    with pytest.raises(ArtifactError, match=field), ArchiveSnapshot(
        source, limits=ArtifactLimits(**{field: cap - 1}),
    ):
        pytest.fail("Overflow entered")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_archive_source_must_be_safe_regular_file(tmp_path: Path, kind: str) -> None:
    """Input opens never follow source symlinks or block on special files."""
    source = tmp_path / "source"
    if kind == "symlink":
        source.symlink_to(_source(tmp_path, _tar(_files())))
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        source.mkdir()
    with pytest.raises((ArtifactError, OSError)), ArchiveSnapshot(source):
        pytest.fail("Nonregular archive entered")


def test_archive_mutation_and_compressed_read_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real-read cap remains binding after stat, and source mutation is detected even within budget."""
    source = _source(tmp_path, _tar(_files()))
    before = source.stat().st_size
    original = archive._Input.header

    def grow(incoming: archive._Input) -> None:
        """Append bytes after the initial size stat to exercise actual-read accounting."""
        with source.open("ab") as target:
            target.write(bytes(CHUNK_SIZE))
        original(incoming)

    monkeypatch.setattr(archive._Input, "header", grow)
    with pytest.raises(ArtifactError, match="max_archive_input_bytes"), ArchiveSnapshot(
        source, limits=ArtifactLimits(max_archive_input_bytes=before),
    ):
        pytest.fail("Growing source entered")


def _patch_link(monkeypatch: pytest.MonkeyPatch, replacement: Callable[..., None]) -> None:
    """Keep platform capability sets accurate while fault-injecting the supported link syscall."""
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {replacement})
    monkeypatch.setattr(os, "supports_follow_symlinks", os.supports_follow_symlinks | {replacement})
    monkeypatch.setattr(os, "link", replacement)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
def test_archive_exclusive_publication_race(tmp_path: Path, kind: str) -> None:
    """A destination created during acquisition wins unchanged; no success-shaped partial copy appears."""
    output = tmp_path / "output.gz"
    registry = Registry(document(children=[]))

    def race(path: str) -> None:
        """Create a competing final entry after preflight but before hard-link publication."""
        if kind == "file":
            output.write_bytes(b"winner")
        elif kind == "directory":
            output.mkdir()
        else:
            output.symlink_to(tmp_path / ("missing" if kind == "dangling" else "sentinel"))

    registry.after_get = race
    if kind == "symlink":
        (tmp_path / "sentinel").write_bytes(b"untouched")
    with pytest.raises(FileExistsError):
        pull_artifact(registry, "repo", "moving", output, output_format=ArtifactFormat.OCI_TAR_GZIP)
    if kind == "file":
        assert output.read_bytes() == b"winner"
    assert not list(tmp_path.glob(".regshape-*"))


@pytest.mark.parametrize("failure", [OSError(errno.ENOTSUP, "no hard links"), OSError(errno.ENOSPC, "full")])
def test_archive_publication_filesystem_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: OSError,
) -> None:
    """Real syscall failures never trigger replacement/copy fallback or leave partial final output."""
    def fail_link(*args, **kwargs) -> None:
        """Inject a filesystem capability/disk error at the final exclusive syscall."""
        raise failure

    _patch_link(monkeypatch, fail_link)
    with pytest.raises((ArtifactError, OSError)):
        _pull(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("after_link", [False, True])
def test_archive_cancel_at_publication_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_link: bool,
) -> None:
    """Cancellation before linking removes staging; after linking retains a completely valid artifact."""
    original = os.link

    def cancel(*args, **kwargs) -> None:
        """Raise the same cancellation on either side of the actual hard-link syscall."""
        if after_link:
            original(*args, **kwargs)
        raise KeyboardInterrupt("cancel boundary")

    _patch_link(monkeypatch, cancel)
    with pytest.raises(KeyboardInterrupt, match="cancel boundary"):
        _pull(tmp_path)
    assert not list(tmp_path.glob(".regshape-*"))
    if after_link:
        with ArchiveSnapshot(tmp_path / "output.gz"):
            pass
    else:
        assert not list(tmp_path.iterdir())


def test_archive_postpublication_unlink_failure_retains_complete_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later staging-unlink failure propagates without rolling back an already complete final link."""
    original = SafeDirectory.unlink
    failed = False

    def fail_once(directory: SafeDirectory, name: str) -> None:
        """Fail the first staging-name unlink only; normal cleanup then reclaims private storage."""
        nonlocal failed
        if name == "output.tar.gz" and not failed:
            failed = True
            raise OSError("unlink diagnostic")
        original(directory, name)

    monkeypatch.setattr(SafeDirectory, "unlink", fail_once)
    with pytest.raises(OSError, match="unlink diagnostic"):
        _pull(tmp_path)
    assert failed and not list(tmp_path.glob(".regshape-*"))
    with ArchiveSnapshot(tmp_path / "output.gz"):
        pass


@pytest.mark.parametrize("kind", ["target", "parent"])
def test_archive_publication_replacement_cannot_redirect_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    """After-link name/parent replacement is detected without adopting or deleting the substitute."""
    base = tmp_path / "base"
    base.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "sentinel").write_bytes(b"untouched")
    original = os.link

    def replace(*args, **kwargs) -> None:
        """Replace an exposed final name or parent after the complete file becomes visible."""
        original(*args, **kwargs)
        if kind == "target":
            (base / "output.gz").rename(base / "moved.gz")
            (base / "output.gz").symlink_to(other / "sentinel")
        else:
            base.rename(tmp_path / "moved-base")
            base.symlink_to(other, target_is_directory=True)

    _patch_link(monkeypatch, replace)
    with pytest.raises(ArtifactError, match="identity changed"):
        _pull(base)
    assert (other / "sentinel").read_bytes() == b"untouched"
    assert list(other.iterdir()) == [other / "sentinel"]
    moved = base / "moved.gz" if kind == "target" else tmp_path / "moved-base" / "output.gz"
    with ArchiveSnapshot(moved):
        pass


@pytest.mark.parametrize("operation", ["import", "export"])
def test_archive_disk_full_and_primary_preserving_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """Mid-stream disk failure removes owned partial data and preserves the exact primary exception."""
    primary = OSError(errno.ENOSPC, "synthetic disk full")
    source = _source(tmp_path, _tar(_files())) if operation == "import" else None
    monkeypatch.setattr(archive.tempfile, "gettempdir", lambda: str(tmp_path))

    def fail(target: BinaryIO, data: bytes) -> None:
        """Write a real partial block, then simulate storage exhaustion on the remaining write."""
        target.write(data[:3])
        raise primary

    monkeypatch.setattr(archive, "_write", fail)
    with pytest.raises(OSError) as caught:
        if source is not None:
            with ArchiveSnapshot(source):
                pytest.fail("Disk-full import entered")
        else:
            _pull(tmp_path)
    assert caught.value is primary
    assert sorted(tmp_path.iterdir()) == ([source] if source else [])


def test_archive_observer_cancellation_leaves_no_output(tmp_path: Path) -> None:
    """Archive pull reuses synchronous graph observers without swallowing cancellation or publishing early."""
    primary = KeyboardInterrupt("caller cancelled")
    registry, _, _ = nested_registry()

    def cancel(event) -> None:
        """Cancel after a verified object so cleanup must reclaim actual received files."""
        if event.phase == "stored":
            raise primary

    with pytest.raises(KeyboardInterrupt) as caught:
        pull_artifact(
            registry, "repo", "moving", tmp_path / "output.gz",
            output_format=ArtifactFormat.OCI_TAR_GZIP, progress_callback=cancel,
        )
    assert caught.value is primary and not list(tmp_path.iterdir())


def test_archive_inherited_permissions_and_untrusted_anchor(tmp_path: Path) -> None:
    """A permissive process umask does not expose private files; delegated bootstrap writes are rejected."""
    old = os.umask(0)
    try:
        output = _pull(tmp_path)
        assert output.stat().st_mode & 0o777 == 0o600
        with ArchiveSnapshot(output) as snapshot:
            assert os.fstat(snapshot.namespace.fd).st_mode & 0o777 == 0o700
            for directory in snapshot.store.algorithms.values():
                for identity in directory.files.values():
                    assert identity.st_mode & 0o777 == 0o600
    finally:
        os.umask(old)
    output.unlink()
    tmp_path.chmod(0o777)
    registry = Registry(document(children=[]))
    with pytest.raises(ArtifactError, match="untrusted writes"):
        pull_artifact(
            registry, "repo", "moving", tmp_path / "out",
            output_format=ArtifactFormat.OCI_TAR_GZIP,
        )
    assert not registry.calls and not list(tmp_path.iterdir())


def test_unsupported_platform_archive_guards_before_effects(tmp_path: Path) -> None:
    """Windows/missing handle support fails before source reads, namespace creation or registry access."""
    registry = Registry(document(children=[]))
    with patch.object(os, "supports_dir_fd", set()), patch.object(os, "mkdir") as mkdir:
        with pytest.raises(ArtifactError, match="unsupported"):
            pull_artifact(
                registry, "repo", "moving", tmp_path / "out",
                output_format=ArtifactFormat.OCI_TAR_GZIP,
            )
        with pytest.raises(ArtifactError, match="unsupported"), ArchiveSnapshot(tmp_path / "source"):
            pytest.fail("Unsupported archive entered")
        mkdir.assert_not_called()
    assert not registry.calls and not list(tmp_path.iterdir())


def test_unsupported_platform_hardlink_guard(tmp_path: Path) -> None:
    """Even a supported directory backend cannot pretend exclusive archive publication without links."""
    registry = Registry(document(children=[]))
    with patch.object(os, "supports_follow_symlinks", set()), pytest.raises(ArtifactError, match="unsupported"):
        pull_artifact(
            registry, "repo", "moving", tmp_path / "out",
            output_format=ArtifactFormat.OCI_TAR_GZIP,
        )
    assert not registry.calls and not list(tmp_path.iterdir())


def test_sha512_writer_pax_roundtrip_and_extension_accounting(tmp_path: Path) -> None:
    """SHA-512 paths use supported local extensions and those headers/bytes count in writer caps."""
    config = b"sha512 config"
    digest = address(config, "sha512")
    root = document(config=edge(config, digest=digest))
    registry = Registry(root, {digest: config})
    output = tmp_path / "sha512.gz"
    pull_artifact(
        registry, "repo", "moving", output, output_format=ArtifactFormat.OCI_TAR_GZIP,
    )
    with tarfile.open(output, "r:gz") as reader:
        members = reader.getmembers()
        item = next(item for item in members if item.name.startswith("blobs/sha512"))
        assert item.pax_headers == {"path": "blobs/" + digest.replace(":", "/")}
    with ArchiveSnapshot(output) as snapshot:
        assert snapshot.store.verify(digest, len(config), cap=100) == config
    metadata = len(_pax("path", item.name))
    output.unlink()
    for field, cap in [("max_archive_metadata_bytes", metadata), ("max_archive_entries", len(members) + 1)]:
        pull_artifact(
            Registry(root, {digest: config}), "repo", "moving", output,
            output_format=ArtifactFormat.OCI_TAR_GZIP, limits=ArtifactLimits(**{field: cap}),
        )
        output.unlink()
        with pytest.raises(ArtifactError, match=field):
            pull_artifact(
                Registry(root, {digest: config}), "repo", "moving", output,
                output_format=ArtifactFormat.OCI_TAR_GZIP, limits=ArtifactLimits(**{field: cap - 1}),
            )
        assert not output.exists()


def test_archive_header_output_cap_before_network(tmp_path: Path) -> None:
    """A policy too small for the fixed gzip header fails before storage or registry effects."""
    registry = Registry(document(children=[]))
    with pytest.raises(ArtifactError, match="max_gzip_header_bytes"):
        pull_artifact(
            registry, "repo", "moving", tmp_path / "out",
            output_format=ArtifactFormat.OCI_TAR_GZIP,
            limits=ArtifactLimits(max_gzip_header_bytes=9),
        )
    assert not registry.calls and not list(tmp_path.iterdir())


def test_archive_snapshot_is_single_use_without_invalidating_live_store(tmp_path: Path) -> None:
    """Accidental reentry cannot overwrite resource ownership or close the first live snapshot."""
    source = _source(tmp_path, _tar(_files()))
    context = ArchiveSnapshot(source)
    with context as snapshot:
        with pytest.raises(ArtifactError, match="single-use"):
            context.__enter__()
        item = snapshot.graph.objects[0]
        snapshot.store.verify(item.digest, item.size)
    with pytest.raises(ArtifactError, match="single-use"):
        context.__enter__()


def test_archive_packer_rehashes_private_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Observed post-verification corruption cannot be packed and published as a verified closure."""
    original = archive._GzipWriter.__init__
    modified = False

    def corrupt(writer: archive._GzipWriter, target: BinaryIO, limits: ArtifactLimits) -> None:
        """Corrupt a real private object after graph verification but before the packing read."""
        nonlocal modified
        path = next(tmp_path.glob(".regshape-private-*/.regshape-staging-*/blobs/sha256/*"))
        path.write_bytes(b"x" * path.stat().st_size)
        modified = True
        original(writer, target, limits)

    monkeypatch.setattr(archive._GzipWriter, "__init__", corrupt)
    with pytest.raises(ArtifactError, match="digest/size changed"):
        _pull(tmp_path)
    assert modified and not list(tmp_path.iterdir())


def test_archive_source_timestamp_mutation_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source that changes after decoding but before identity checks cannot enter the snapshot."""
    source = _source(tmp_path, _tar(_files()))
    original = archive._inflate

    def touch(incoming: BinaryIO, target: BinaryIO, limits: ArtifactLimits) -> int:
        """Alter actual filesystem mutation metadata without changing valid compressed bytes."""
        result = original(incoming, target, limits)
        timestamp = source.stat().st_mtime_ns + 1000000000
        os.utime(source, ns=(timestamp, timestamp))
        return result

    monkeypatch.setattr(archive, "_inflate", touch)
    with pytest.raises(ArtifactError, match="source changed"), ArchiveSnapshot(source):
        pytest.fail("Changed source entered")


def test_archive_primary_error_survives_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A secondary real cleanup refusal is reported but never replaces a decoding cancellation."""
    primary = KeyboardInterrupt("immutable primary")
    source = _source(tmp_path, _tar(_files()))
    monkeypatch.setattr(archive.tempfile, "gettempdir", lambda: str(tmp_path))

    def fail(source: BinaryIO, target: BinaryIO, limits: ArtifactLimits) -> int:
        """Create a real partial spool then cancel the decoder."""
        target.write(b"partial")
        monkeypatch.setattr(SafeDirectory, "cleanup", cleanup)
        raise primary

    def cleanup(directory: SafeDirectory) -> None:
        """Simulate permissions preventing owned namespace cleanup."""
        raise PermissionError("cleanup refused")

    monkeypatch.setattr(archive, "_inflate", fail)
    with pytest.raises(KeyboardInterrupt) as caught, ArchiveSnapshot(source):
        pytest.fail("Cancelled snapshot entered")
    assert caught.value is primary
    assert "retained" in caplog.text and "PermissionError" in caplog.text
    assert list(tmp_path.glob(".regshape-private-*"))


@pytest.mark.parametrize("operation", ["inflate", "pack"])
def test_raw_archive_short_writes_are_errors(operation: str) -> None:
    """Binary streams that report a short write cannot produce a success result."""
    class ShortWriter(io.BytesIO):
        """Synthetic disk stream accepting a real prefix but reporting insufficient progress."""

        def write(self, data: bytes) -> int:
            """Write only half the requested bytes, including short framing writes."""
            return super().write(data[:len(data) // 2])

    with pytest.raises(OSError, match="Short archive"):
        if operation == "inflate":
            archive._inflate(io.BytesIO(gzip.compress(2 * ZERO)), ShortWriter(), ArtifactLimits())
        else:
            archive._GzipWriter(ShortWriter(), ArtifactLimits())


def test_raw_gzip_all_reads_bounded_and_input_cap_counts_header() -> None:
    """No reader request is unbounded, and even a fixed header cannot bypass the compressed cap."""
    class BoundedSource(io.BytesIO):
        """Reject oversized/read-all requests on a real bytes source."""

        def read(self, size: int = -1) -> bytes:
            """Record the production guarantee that every request is between one byte and a chunk."""
            assert 0 < size <= CHUNK_SIZE
            return super().read(size)

    data = gzip.compress(4 * CHUNK_SIZE * b"a", mtime=0)
    archive._inflate(BoundedSource(data), io.BytesIO(), ArtifactLimits())
    with pytest.raises(ArtifactError, match="max_archive_input_bytes"):
        archive._inflate(BoundedSource(data), io.BytesIO(), ArtifactLimits(max_archive_input_bytes=9))


def test_raw_tar_effective_size_controls_padding_and_following_header() -> None:
    """A local size override, not the raw header value, locates both padding and the next member."""
    raw = (
        _extension(_pax("size", "3")) + _header(size=0) + b"abc" + bytes(509)
        + _header("index.json") + 2 * ZERO
    )
    members = archive._preflight(io.BytesIO(raw), len(raw), ArtifactLimits())
    assert members[0].size == 3 and members[1].offset == 2560
    corrupt = raw[:1539] + b"!" + raw[1540:]
    with pytest.raises(ArtifactError, match="padding"):
        archive._preflight(io.BytesIO(corrupt), len(corrupt), ArtifactLimits())


def test_empty_index_archive_is_complete(tmp_path: Path) -> None:
    """An empty root index remains a valid complete artifact, not an excuse to omit its bytes."""
    root = document(children=[])
    output = tmp_path / "empty.gz"
    pull_artifact(
        Registry(root), "repo", "moving", output, output_format=ArtifactFormat.OCI_TAR_GZIP,
    )
    with ArchiveSnapshot(output) as snapshot:
        assert len(snapshot.graph.objects) == 1
        assert snapshot.graph.objects[0].digest == address(root)

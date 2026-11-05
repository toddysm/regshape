"""Bounded single-member gzip/POSIX tar transport for verified artifact graphs.

Import never delegates extraction or extension decoding to tarfile: a bounded
seekable tar spool is preflighted before copying regular files into a private
snapshot. Export hashes the verified closure while packing, without a second
snapshot. Both operations inherit SafeDirectory's trusted-identity/ACL contract;
hostile same-user or privileged access to private storage is out of contract.
"""

import hashlib
import os
import re
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, final

from regshape.libs.artifacts.graph import VerifiedGraph, layout_roots, verify_graph
from regshape.libs.artifacts.models import (
    ArtifactLimits,
    ArtifactPhase,
    ArtifactPhaseCallback,
    digest_parts,
)
from regshape.libs.artifacts.storage import CHUNK_SIZE, ContentStore, SafeDirectory
from regshape.libs.errors import ArtifactError

_BLOCK = 512
_ZERO = bytes(_BLOCK)
_MARKER = b'{"imageLayoutVersion":"1.0.0"}'
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
_METADATA_TOTAL = 64 * 1024 * 1024
_OCTAL_MAX = 8**11 - 1
_MTIME = re.compile(rb"-?[0-9]+(?:\.[0-9]+)?")


def _write(target: BinaryIO, data: bytes) -> None:
    """Write all of one bounded block or fail explicitly on short writes."""
    if target.write(data) != len(data):
        raise OSError("Short archive write")


class _Input:
    """Borrow a source stream and charge every compressed byte, including lookahead."""

    def __init__(self, source: BinaryIO, limits: ArtifactLimits) -> None:
        """Initialize independent compressed-input and header counters without reading."""
        self.source = source
        self.limits = limits
        self.count = 0
        self.header_count = 0
        self.header_crc = 0

    def read(self, size: int) -> bytes:
        """Read at most one chunk/cap+1, rejecting budget overflow before returning bytes."""
        data = self.source.read(min(size, CHUNK_SIZE, self.limits.max_archive_input_bytes - self.count + 1))
        self.count += len(data)
        if self.count > self.limits.max_archive_input_bytes:
            raise ArtifactError("Archive exceeds max_archive_input_bytes")
        return data

    def exact(self, size: int, *, header: bool = False, checksum: bool = True) -> bytes:
        """Read a small exact field; reserve header bytes before buffering optional data."""
        if header:
            if size > self.limits.max_gzip_header_bytes - self.header_count:
                raise ArtifactError("Gzip header exceeds max_gzip_header_bytes")
            self.header_count += size
        result = bytearray()
        while len(result) < size:
            data = self.read(size - len(result))
            if not data:
                raise ArtifactError("Truncated gzip stream")
            result.extend(data)
        value = bytes(result)
        if header and checksum:
            self.header_crc = zlib.crc32(value, self.header_crc)
        return value

    def header(self) -> None:
        """Validate one bounded RFC 1952 header, including optional header CRC."""
        fixed = self.exact(10, header=True)
        if fixed[:3] != b"\x1f\x8b\x08" or fixed[3] & 0xE0:
            raise ArtifactError("Unsupported or malformed gzip header")
        flags = fixed[3]
        if flags & 4:
            length = int.from_bytes(self.exact(2, header=True), "little")
            self.exact(length, header=True)
        for flag in (8, 16):
            if flags & flag:
                while self.exact(1, header=True) != b"\0":
                    pass
        if flags & 2:
            expected = self.header_crc & 0xFFFF
            actual = int.from_bytes(self.exact(2, header=True, checksum=False), "little")
            if actual != expected:
                raise ArtifactError("Gzip header CRC mismatch")


def _inflate(source: BinaryIO, target: BinaryIO, limits: ArtifactLimits) -> int:
    """Spool exactly one raw-deflate member, validating CRC/size and rejecting all trailing bytes.

    Output allocation is chunk-bounded even for bombs. The returned size includes
    every expanded tar byte, not merely member data. Caller owns both streams.
    """
    incoming = _Input(source, limits)
    incoming.header()
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    pending = b""
    count = crc = 0
    try:
        while not decoder.eof:
            if not pending:
                pending = incoming.read(CHUNK_SIZE)
                if not pending:
                    raise ArtifactError("Truncated gzip deflate stream")
            data = decoder.decompress(
                pending, min(CHUNK_SIZE, limits.max_archive_bytes - count + 1),
            )
            pending = decoder.unconsumed_tail
            count += len(data)
            if count > limits.max_archive_bytes:
                raise ArtifactError("Archive exceeds max_archive_bytes")
            crc = zlib.crc32(data, crc)
            _write(target, data)
    except zlib.error as error:
        raise ArtifactError("Invalid gzip deflate stream") from error
    trailer = decoder.unused_data
    if len(trailer) < 8:
        trailer += incoming.exact(8 - len(trailer))
    expected_crc, expected_size = struct.unpack("<II", trailer[:8])
    if (expected_crc, expected_size) != (crc, count & 0xFFFFFFFF):
        raise ArtifactError("Gzip trailer CRC or size mismatch")
    if len(trailer) != 8 or incoming.read(1):
        raise ArtifactError("Trailing data or additional gzip member")
    return count


def _octal(raw: bytes) -> int:
    """Parse a fixed-width POSIX octal field, never GNU base-256 or signed numbers."""
    value = raw.strip(b" \0")
    if not value:
        return 0
    if any(char not in b"01234567" for char in value):
        raise ArtifactError("Unsupported tar numeric profile")
    return int(value, 8)


def _text(raw: bytes) -> str:
    """Decode a fixed-width UTF-8 field, rejecting data hidden after its first NUL."""
    value, _, tail = raw.partition(b"\0")
    if tail.strip(b"\0"):
        raise ArtifactError("Nonzero data after tar path terminator")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArtifactError("Invalid UTF-8 tar path") from error


def _decimal(raw: bytes) -> int:
    """Parse a bounded nonnegative POSIX decimal size without interpreter-size surprises."""
    if not raw or len(raw) > 20 or any(char not in b"0123456789" for char in raw):
        raise ArtifactError("Unsupported POSIX size value")
    return int(raw)


def _pax(body: bytes) -> dict[str, bytes]:
    """Decode already capped per-file POSIX records, allowing only path, size and mtime."""
    result: dict[str, bytes] = {}
    offset = 0
    while offset < len(body):
        space = body.find(b" ", offset, min(len(body), offset + 22))
        if space < 0:
            raise ArtifactError("Invalid POSIX record length")
        length = _decimal(body[offset:space])
        end = offset + length
        if length <= space - offset + 3 or end > len(body) or body[end - 1:end] != b"\n":
            raise ArtifactError("Invalid POSIX record bounds")
        key, separator, value = body[space + 1:end - 1].partition(b"=")
        if not separator or key not in (b"path", b"size", b"mtime"):
            raise ArtifactError("Unsupported POSIX extension attribute")
        name = key.decode("ascii")
        if name in result or b"\0" in value:
            raise ArtifactError("Duplicate or malformed POSIX extension attribute")
        if name == "size":
            _decimal(value)
        if name == "mtime" and (len(value) > 64 or _MTIME.fullmatch(value) is None):
            raise ArtifactError("Unsupported POSIX mtime value")
        result[name] = value
        offset = end
    if not result:
        raise ArtifactError("Empty POSIX extension")
    return result


def _path(name: str, directory: bool) -> str:
    """Require a literal OCI-root-relative path; normalization never hides unsafe components."""
    if directory and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or "\\" in name or "\0" in name or ":" in name
    ):
        raise ArtifactError("Unsafe archive path")
    if directory:
        allowed = name in ("blobs", "blobs/sha256", "blobs/sha512")
    elif name in ("oci-layout", "index.json"):
        allowed = True
    else:
        allowed = len(parts) == 3 and parts[0] == "blobs"
        if allowed:
            try:
                digest_parts(parts[1] + ":" + parts[2])
            except ValueError:
                allowed = False
    if not allowed:
        raise ArtifactError("Unsupported archive path/profile")
    return name


@dataclass(frozen=True)
class _Member:
    """Preflighted regular/directory member with an exact data range in the immutable private spool."""

    name: str
    size: int
    offset: int
    directory: bool = False


def _zero_padding(source: BinaryIO, count: int) -> None:
    """Consume only bounded zero padding, rejecting truncation and nonzero data."""
    while count:
        data = source.read(min(count, CHUNK_SIZE))
        if not data or data.strip(b"\0"):
            raise ArtifactError("Invalid or truncated tar padding")
        count -= len(data)


def _preflight(source: BinaryIO, total: int, limits: ArtifactLimits) -> tuple[_Member, ...]:
    """Scan raw headers and offsets before copying or allocating any uncapped extension.

    Counts include all raw extension/directory headers. Member data is skipped by
    seeking only after stream-bound checks; padding/end markers are read exactly.
    """
    source.seek(0)
    members: list[_Member] = []
    seen: set[str] = set()
    pending: dict[str, bytes] | None = None
    entries = metadata = content_bytes = objects = 0
    while True:
        raw = source.read(_BLOCK)
        if len(raw) != _BLOCK:
            raise ArtifactError("Truncated tar header/end markers")
        if raw == _ZERO:
            if pending is not None:
                raise ArtifactError("Dangling POSIX extension")
            remaining = total - source.tell()
            if remaining < _BLOCK or remaining % _BLOCK:
                raise ArtifactError("Invalid tar end markers/alignment")
            _zero_padding(source, remaining)
            return tuple(members)
        entries += 1
        if entries > limits.max_archive_entries:
            raise ArtifactError("Archive exceeds max_archive_entries")
        if _octal(raw[148:156]) != sum(raw[:148]) + 8 * 32 + sum(raw[156:]):
            raise ArtifactError("Tar header checksum mismatch")
        if raw[257:265] != b"ustar\0" + b"00" or raw[500:].strip(b"\0"):
            raise ArtifactError("Unsupported POSIX tar profile")
        kind = raw[156:157]
        if kind not in (b"\0", b"0", b"5", b"x") or raw[157:257].strip(b"\0"):
            raise ArtifactError("Unsupported tar member type/link/extension")
        for field in (raw[100:108], raw[108:116], raw[116:124], raw[136:148]):
            _octal(field)
        if _octal(raw[329:337]) or _octal(raw[337:345]):
            raise ArtifactError("Unsupported tar device metadata")
        size = _octal(raw[124:136])
        name = _text(raw[:100])
        prefix = _text(raw[345:500])
        if prefix:
            name = prefix + "/" + name
        if kind == b"x":
            if pending is not None:
                raise ArtifactError("Stacked POSIX extensions are unsupported")
            if size > limits.max_archive_metadata_bytes:
                raise ArtifactError("Archive exceeds max_archive_metadata_bytes")
            metadata += size
            if metadata > _METADATA_TOTAL:
                raise ArtifactError("Archive exceeds aggregate extension metadata cap")
        else:
            if pending is not None:
                if "path" in pending:
                    name = _text(pending["path"])
                if "size" in pending:
                    size = _decimal(pending["size"])
            pending = None
            directory = kind == b"5"
            name = _path(name, directory)
            if name in seen:
                raise ArtifactError("Duplicate archive path")
            seen.add(name)
            if directory and size:
                raise ArtifactError("Tar directories must have zero size")
            cap = (
                limits.max_layout_marker_bytes if name == "oci-layout"
                else limits.max_manifest_bytes if name == "index.json" else None
            )
            if cap is not None and size > cap:
                label = "max_layout_marker_bytes" if name == "oci-layout" else "max_manifest_bytes"
                raise ArtifactError(f"Archive metadata exceeds {label}")
            if not directory and name.startswith("blobs/"):
                objects += 1
                content_bytes += size
                if objects > limits.max_objects:
                    raise ArtifactError("Archive exceeds max_objects")
                if content_bytes > limits.max_total_bytes:
                    raise ArtifactError("Archive exceeds max_total_bytes")
            members.append(_Member(name, size, source.tell(), directory))
        padding = -size % _BLOCK
        if size + padding > total - source.tell():
            raise ArtifactError("Tar member exceeds spool bounds")
        if kind == b"x":
            pending = _pax(source.read(size))
        else:
            source.seek(size, os.SEEK_CUR)
        _zero_padding(source, padding)


def _copy_members(
    source: BinaryIO, members: tuple[_Member, ...], store: ContentStore,
) -> None:
    """Copy only preflighted regular data into exclusively created private files, never extractall."""
    for member in members:
        if member.directory:
            continue
        if member.name in ("oci-layout", "index.json"):
            directory, name = store.directory, member.name
        else:
            _, algorithm, name = member.name.split("/")
            directory = store.algorithms[algorithm]
        source.seek(member.offset)
        with directory.create_file(name) as target:
            remaining = member.size
            while remaining:
                data = source.read(min(remaining, CHUNK_SIZE))
                if not data:
                    raise ArtifactError("Truncated private archive spool")
                _write(target, data)
                remaining -= len(data)


@final
class ArchiveSnapshot:
    """Context-owned verified archive source for recursive validation and future push.

    ``store``, ``graph`` and original ``wrapper_bytes`` are valid only inside the
    entered context. Objects are dependency-first unique addresses; roots are
    normalized graph edges, NOT complete wrapper descriptors. No original source
    path is reopened or hard-linked. Peak temporary data is expanded tar T plus
    copied content U and bounded metadata; the spool is deleted before enter
    returns. SafeDirectory's namespace trust/platform requirements apply.
    """

    def __init__(self, path: str | Path, *, limits: ArtifactLimits | None = None) -> None:
        """Create a single-use context without effects; reject invalid policy types immediately."""
        self.path = Path(path).absolute()
        self.limits = limits if limits is not None else ArtifactLimits()
        if not isinstance(self.limits, ArtifactLimits):
            raise ValueError("limits must be ArtifactLimits")  # noqa: TRY004 - public contract
        self.parent: SafeDirectory | None = None
        self.namespace: SafeDirectory | None = None
        self.directory: SafeDirectory | None = None
        self.store: ContentStore
        self.graph: VerifiedGraph
        self.wrapper_bytes: bytes
        self._entered = False

    def __enter__(self) -> "ArchiveSnapshot":
        """Open a regular no-follow source, validate framing/profile, and verify its exact closure."""
        if self._entered:
            raise ArtifactError("ArchiveSnapshot contexts are single-use")
        self._entered = True
        try:
            with SafeDirectory.open(self.path.parent) as source_parent:
                with source_parent.read_file(self.path.name) as source:
                    before = os.fstat(source.fileno())
                    if before.st_size > self.limits.max_archive_input_bytes:
                        raise ArtifactError("Archive exceeds max_archive_input_bytes")
                    self.parent = SafeDirectory.open(Path(tempfile.gettempdir()).resolve())
                    self.namespace = SafeDirectory.private_namespace(self.parent)
                    self.directory = self.namespace.temporary()
                    self.store = ContentStore(self.directory, writable=True)
                    with self.namespace.create_file("archive.tar") as spool:
                        total = _inflate(source, spool, self.limits)
                    after = os.fstat(source.fileno())
                    entry = os.stat(self.path.name, dir_fd=source_parent.fd, follow_symlinks=False)
                    if (
                        not os.path.samestat(before, entry)
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    ):
                        raise ArtifactError("Archive source changed while reading")
                    source_parent.verify()
                with self.namespace.read_file("archive.tar") as spool:
                    members = _preflight(spool, total, self.limits)
                    _copy_members(spool, members, self.store)
                self.wrapper_bytes, roots = layout_roots(self.directory, self.limits)
                self.graph = verify_graph(roots, self.store, self.limits)
                expected = {"oci-layout", "index.json"} | {
                    "blobs/" + item.digest.replace(":", "/") for item in self.graph.objects
                }
                if {item.name for item in members if not item.directory} != expected:
                    raise ArtifactError("Unreferenced archive content")
                self.namespace.unlink("archive.tar")
            return self
        except BaseException as primary:
            self.__exit__(type(primary), primary, primary.__traceback__)
            raise

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Remove only owned private entries; cleanup never masks the active consumer exception."""
        primary = exc
        try:
            if self.namespace is not None:
                self.namespace.__exit__(exc_type, exc, traceback)
        except BaseException as error:
            primary = error
            raise
        finally:
            if self.parent is not None:
                self.parent.close(primary)
        return False


def _record(key: str, value: str) -> bytes:
    """Encode a POSIX record with a self-inclusive decimal byte length."""
    body = (key + "=" + value + "\n").encode("utf-8")
    length = len(body) + 2
    while True:
        result = str(length).encode("ascii") + b" " + body
        if len(result) == length:
            return result
        length = len(result)


def _header(name: str, size: int, kind: bytes = b"0") -> bytes:
    """Build normalized USTAR metadata from trusted bounded writer fields, not source ownership."""
    raw = bytearray(_BLOCK)
    encoded = name.encode("utf-8")
    if len(encoded) > 100 or not 0 <= size <= _OCTAL_MAX:
        raise ArtifactError("Internal tar header requires a POSIX extension")
    raw[:len(encoded)] = encoded
    raw[100:108] = b"0000644\0"
    raw[108:116] = raw[116:124] = b"0000000\0"
    raw[124:136] = f"{size:011o}\0".encode("ascii")
    raw[136:148] = b"00000000000\0"
    raw[148:156] = b"        "
    raw[156:157] = kind
    raw[257:265] = b"ustar\0" + b"00"
    raw[148:156] = f"{sum(raw):06o}\0 ".encode("ascii")
    return bytes(raw)


def _headers(name: str, size: int, limits: ArtifactLimits) -> tuple[bytes, int, int]:
    """Return bounded normalized headers plus raw-entry/extension-body charges for one file."""
    records = b""
    if len(name.encode("utf-8")) > 100:
        records += _record("path", name)
    if size > _OCTAL_MAX:
        records += _record("size", str(size))
    if len(records) > limits.max_archive_metadata_bytes:
        raise ArtifactError("Archive exceeds max_archive_metadata_bytes")
    prefix = b""
    if records:
        prefix = _header("PaxHeaders/entry", len(records), b"x") + records + bytes(-len(records) % _BLOCK)
    return (
        prefix + _header("pax-entry" if len(name.encode("utf-8")) > 100 else name, min(size, _OCTAL_MAX)),
        2 if records else 1, len(records),
    )


class _GzipWriter:
    """Borrow an output file; enforce both archive budgets while incrementally encoding one member."""

    def __init__(self, target: BinaryIO, limits: ArtifactLimits) -> None:
        """Write a timestamp-zero, filename-free gzip header under its independent cap."""
        self.target, self.limits = target, limits
        self.input_count = self.output_count = self.crc = 0
        self.encoder = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        if len(_GZIP_HEADER) > limits.max_gzip_header_bytes:
            raise ArtifactError("Gzip header exceeds max_gzip_header_bytes")
        self._compressed(_GZIP_HEADER)

    def _compressed(self, data: bytes) -> None:
        """Charge and write compressed bytes; an over-budget file can never be published."""
        self.output_count += len(data)
        if self.output_count > self.limits.max_archive_input_bytes:
            raise ArtifactError("Archive exceeds max_archive_input_bytes")
        _write(self.target, data)

    def write(self, data: bytes) -> None:
        """Charge expanded tar bytes and feed bounded chunks to the compressor."""
        self.input_count += len(data)
        if self.input_count > self.limits.max_archive_bytes:
            raise ArtifactError("Archive exceeds max_archive_bytes")
        for offset in range(0, len(data), CHUNK_SIZE):
            chunk = data[offset:offset + CHUNK_SIZE]
            self.crc = zlib.crc32(chunk, self.crc)
            self._compressed(self.encoder.compress(chunk))

    def finish(self) -> None:
        """Flush deflate and append its checked trailer; caller must close before publication."""
        self._compressed(self.encoder.flush())
        self._compressed(struct.pack("<II", self.crc, self.input_count & 0xFFFFFFFF))


def write_archive(
    store: ContentStore, graph: VerifiedGraph, wrapper: bytes,
    parent: SafeDirectory, name: str, limits: ArtifactLimits,
    *, phase_callback: ArtifactPhaseCallback | None = None,
) -> None:
    """Pack an already verified private closure and exclusively publish one complete regular file.

    This internal writer borrows all handles. It rehashes each object while
    streaming and checks exact sizes; original wrapper bytes preserve descriptor
    metadata. Temporary disk is existing content U plus compressed output C,
    not an additional tar/snapshot. A successful hard link is irreversible:
    later cleanup/diagnostic failures retain the complete output.
    The optional observer announces publication only after the packed file closes;
    its exception propagates before publication without claiming transfer bytes.
    """
    directory = store.directory
    members = [("oci-layout", len(_MARKER)), ("index.json", len(wrapper))]
    objects = {"blobs/" + item.digest.replace(":", "/"): item for item in graph.objects}
    members.extend((path, objects[path].size) for path in sorted(objects))
    headers: list[bytes] = []
    entries = metadata = 0
    expanded = 2 * _BLOCK
    for path, size in members:
        header, count, charge = _headers(path, size, limits)
        headers.append(header)
        entries += count
        metadata += charge
        expanded += len(header) + size + (-size % _BLOCK)
    if entries > limits.max_archive_entries:
        raise ArtifactError("Archive exceeds max_archive_entries")
    if metadata > _METADATA_TOTAL:
        raise ArtifactError("Archive exceeds aggregate extension metadata cap")
    if expanded > limits.max_archive_bytes:
        raise ArtifactError("Archive exceeds max_archive_bytes")
    with directory.create_file("output.tar.gz") as target:
        writer = _GzipWriter(target, limits)
        for (path, size), header in zip(members, headers):
            writer.write(header)
            if path in ("oci-layout", "index.json"):
                writer.write(_MARKER if path == "oci-layout" else wrapper)
            else:
                item = objects[path]
                algorithm, encoded = digest_parts(item.digest)
                with store.algorithms[algorithm].read_file(encoded) as source:
                    before = os.fstat(source.fileno())
                    if before.st_size != size:
                        raise ArtifactError("Archive object size changed before packing")
                    count = 0
                    hasher = hashlib.new(algorithm)
                    while chunk := source.read(CHUNK_SIZE):
                        count += len(chunk)
                        if count > size:
                            raise ArtifactError("Archive object grew while packing")
                        hasher.update(chunk)
                        writer.write(chunk)
                    after = os.fstat(source.fileno())
                    if (
                        count != size or f"{algorithm}:{hasher.hexdigest()}" != item.digest
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    ):
                        raise ArtifactError("Archive object digest/size changed while packing")
            writer.write(bytes(-size % _BLOCK))
        writer.write(2 * _ZERO)
        writer.finish()
    if phase_callback is not None:
        phase_callback(ArtifactPhase.PUBLISHING)
    directory.publish_file("output.tar.gz", parent, name)

"""Handle-relative artifact storage, ownership-aware cleanup and bounded copying.

New directories are acquired only inside an access-policy-checked private
namespace and installed with a native atomic no-replace rename. Public names are
never reopened to acquire ownership. The caller must exclusively control the
effective user identity and privileged actors during the namespace lifetime:
ordinary permissions do not isolate hostile same-identity processes.
"""

import hashlib
import os
import stat
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, cast, final
from uuid import uuid4

from regshape.libs._http import report_secondary_failure
from regshape.libs.artifacts.models import digest_parts
from regshape.libs.artifacts.native import NativeFilesystem
from regshape.libs.errors import ArtifactError

INCOMPLETE = ".regshape-incomplete"
CHUNK_SIZE = 65536


def require_safe_filesystem() -> None:
    """Reject platforms without no-follow, handle-relative file/directory operations."""
    required = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.rename)
    if (
        not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
        or not all(operation in os.supports_dir_fd for operation in required)
        or os.stat not in os.supports_follow_symlinks
    ):
        raise ArtifactError("Safe handle-relative artifact storage is unsupported on this platform")


def _component(name: str) -> None:
    """Require one literal local path component before passing it to dir_fd APIs."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ArtifactError("Unsafe local artifact path component")


def _close_after_failure(fd: int, primary: BaseException) -> None:
    """Release an acquired descriptor without replacing an immutable acquisition failure."""
    try:
        os.close(fd)
    except OSError as secondary:
        report_secondary_failure(primary, secondary, "Artifact handle acquisition cleanup")


class SafeFile:
    """Own a regular file handle and preserve the primary failure if close fails."""

    def __init__(self, fd: int, mode: str) -> None:
        """Take ownership of an already checked descriptor."""
        try:
            self.file = cast(BinaryIO, os.fdopen(fd, mode))
        except BaseException as primary:
            _close_after_failure(fd, primary)
            raise

    def __enter__(self) -> BinaryIO:
        """Borrow the stream until this context exits."""
        return self.file

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Propagate a sole close failure; never replace an active caller exception."""
        try:
            self.file.close()
        except BaseException as error:
            if exc is None:
                raise
            report_secondary_failure(exc, error, "Artifact file close")
        return False


@final
class SafeDirectory:
    """An opened directory with its parent association and explicit ownership ledger.

    Read-only directories close their handles. Owned directories remove only
    registered entries on exit unless retain() transfers the complete tree to
    the caller. Parent handles must outlive children.
    """

    def __init__(
        self, fd: int, name: str, parent: "SafeDirectory | None" = None,
        *, owned: bool = False, owns_parent: bool = False,
    ) -> None:
        """Take a directory handle and remember its identity without reopening its path."""
        self.fd = fd
        self.name = name
        self.parent = parent
        self.owned = owned
        self.owns_parent = owns_parent
        try:
            self.identity = os.fstat(fd)
        except BaseException as primary:
            _close_after_failure(fd, primary)
            raise
        self.files: dict[str, os.stat_result] = {}
        self.children: dict[str, SafeDirectory] = {}
        self.closed = False
        self.publication_marker: str | None = None
        self.namespace: SafeDirectory | None = None
        self.native: NativeFilesystem | None = None

    @classmethod
    def open(cls, path: str | Path) -> "SafeDirectory":
        """Open an existing absolute/relative path componentwise, rejecting symlinks."""
        require_safe_filesystem()
        if ".." in Path(path).parts:
            raise ArtifactError("Parent path components are not supported by safe artifact opens")
        path = Path(os.path.abspath(path))
        current = cls(os.open("/", os.O_RDONLY | os.O_DIRECTORY), "/")
        try:
            for name in path.parts[1:]:
                child = current.open_child(name)
                child.owns_parent = True
                current = child
            return current
        except BaseException as primary:
            current.close(primary)
            raise

    def __enter__(self) -> "SafeDirectory":
        """Expose the directory without changing ownership."""
        return self

    def verify(self) -> None:
        """Check associations and recheck the private root's access policy throughout its lifetime."""
        if self.closed:
            raise ArtifactError("Artifact directory handle is closed")
        if self.parent is not None:
            self.parent.verify()
            current = os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(current, self.identity):
                raise ArtifactError("Artifact directory identity changed; owned output retained")
        if self.namespace is self:
            if self.native is None or self.parent is None:
                raise ArtifactError("Private namespace lacks its native backend or bootstrap parent")
            identity = os.fstat(self.fd)
            if identity.st_uid != os.geteuid() or stat.S_IMODE(identity.st_mode) != 0o700:
                raise ArtifactError("Private namespace ownership or permissions changed")
            self.native.check_acl(self.fd)
            self.parent.require_trusted_anchor(self.native)
        elif self.namespace is not None and not self._inside_namespace():
            self.namespace.verify()

    def require_trusted_anchor(self, native: NativeFilesystem) -> None:
        """Require a root/effective-user-owned, nondelegated bootstrap path.

        Group/other writes are forbidden except sticky directories owned by root
        or this user, whose sticky rule protects this user's new entries. All
        ancestors and ACLs are inspected through retained handles. This enforces
        access against other unprivileged identities, NOT hostile same-user or
        privileged mutation; callers must exclude those actors.
        """
        self.verify()
        current: SafeDirectory | None = self
        while current is not None:
            identity = os.fstat(current.fd)
            if identity.st_uid not in (0, os.geteuid()):
                raise ArtifactError("Namespace bootstrap ancestor has an untrusted owner")
            if identity.st_mode & 0o022 and not identity.st_mode & stat.S_ISVTX:
                raise ArtifactError("Namespace bootstrap ancestor permits untrusted writes")
            native.check_acl(current.fd)
            current = current.parent

    @classmethod
    def private_namespace(cls, parent: "SafeDirectory") -> "SafeDirectory":
        """Create an invocation-private 0700 root under a checked trusted bootstrap parent.

        The parent path's access policy and native backend are checked before
        creation. The caller's exclusive-control precondition covers the initial
        mkdir/open sequence here; permission checks cannot detect a hostile
        same-user process. No caller-selected staging/output name is acquired
        that way. A private probe verifies filesystem no-replace behavior before
        registry access. Keep this context alive through installation/cleanup.
        """
        native = NativeFilesystem()
        parent.require_trusted_anchor(native)
        root = parent._mkdir_owned(".regshape-private-" + uuid4().hex)
        root.namespace = root
        root.native = native
        try:
            root.verify()
            root._probe_installation()
            return root
        except BaseException as primary:
            root.__exit__(type(primary), primary, primary.__traceback__)
            raise

    def _inside_namespace(self) -> bool:
        """Return whether this handle still has its private root as an actual retained ancestor."""
        if self.namespace is None:
            return False
        current: SafeDirectory | None = self
        while current is not None:
            if current is self.namespace:
                return True
            current = current.parent
        return False

    def _probe_installation(self) -> None:
        """Verify no-replace collision and successful installation using only owned scratch entries."""
        if self.native is None:
            raise ArtifactError("Private namespace has no native installation backend")
        with self.mkdir("probe-source") as source, self.mkdir("probe-occupied") as occupied:
            try:
                self.native.move(self.fd, source.name, self.fd, occupied.name)
            except FileExistsError:
                pass  # The primitive must reject this controlled collision.
            else:
                raise ArtifactError("Native filesystem probe did not enforce no-replace")
            source.verify()
            occupied.verify()
            self.install(source, "probe-installed")

    def description(self) -> str:
        """Return a bounded escaped original location for retained-output diagnostics, not reopening."""
        parts = []
        current: SafeDirectory | None = self
        while current is not None:
            parts.append(current.name)
            current = current.parent
        return repr("/".join(reversed(parts))[-512:])

    def exists(self, name: str) -> bool:
        """Check an entry without following it; dangling symlinks count as existing."""
        _component(name)
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def open_child(self, name: str) -> "SafeDirectory":
        """Open a no-follow child; the returned context borrows this parent handle."""
        _component(name)
        self.verify()
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        child = SafeDirectory(fd, name, self)
        child.namespace, child.native = self.namespace, self.native
        return child

    def mkdir(self, name: str) -> "SafeDirectory":
        """Create a child only within the still-live invocation-private namespace.

        Public/output parents cannot acquire ownership by mkdir followed by
        pathname inspection. Use install() on an already held private child.
        """
        if not self._inside_namespace():
            raise ArtifactError("Owned directory creation requires a trusted private namespace")
        return self._mkdir_owned(name)

    def _mkdir_owned(self, name: str) -> "SafeDirectory":
        """Acquire a new child in a trusted bootstrap/private namespace, never an exposed output."""
        _component(name)
        self.verify()
        os.mkdir(name, 0o700, dir_fd=self.fd)
        created = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        child = self.open_child(name)
        if not os.path.samestat(created, child.identity):
            child.close()
            raise ArtifactError("New artifact directory was replaced; retained without cleanup")
        if child.identity.st_uid != os.geteuid() or stat.S_IMODE(child.identity.st_mode) != 0o700:
            child.close()
            raise ArtifactError("New private directory has unexpected ownership/permissions; retained")
        child.owned = True
        self.children[name] = child
        return child

    def temporary(self) -> "SafeDirectory":
        """Create an owned staging child inside the private root, not in the public output parent."""
        return self.mkdir(".regshape-staging-" + uuid4().hex)

    def register(self, name: str) -> None:
        """Register a successful exclusive receive for later promotion or safe cleanup.

        Only call for a file just created by this invocation in this private
        directory. Arbitrary source files must never be registered as owned.
        """
        _component(name)
        self.verify()
        identity = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(identity.st_mode):
            raise ArtifactError("Artifact receive is not a regular file")
        self.files[name] = identity

    def create_file(self, name: str) -> SafeFile:
        """Exclusively create a registered regular file, returning an owning close context."""
        _component(name)
        self.verify()
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=self.fd,
        )
        try:
            self.files[name] = os.fstat(fd)
        except BaseException as primary:
            _close_after_failure(fd, primary)
            raise
        return SafeFile(fd, "wb")

    def read_file(self, name: str) -> SafeFile:
        """Open a regular file without following links or blocking on a FIFO."""
        _component(name)
        self.verify()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ArtifactError("Artifact input must be a regular file")
        except BaseException as primary:
            _close_after_failure(fd, primary)
            raise
        return SafeFile(fd, "rb")

    def read_bytes(self, name: str, cap: int) -> bytes:
        """Read at most cap+1 bytes, rejecting oversized metadata before JSON parsing."""
        with self.read_file(name) as source:
            content = bytearray()
            while chunk := source.read(min(CHUNK_SIZE, cap + 1 - len(content))):
                content.extend(chunk)
                if len(content) > cap:
                    raise ArtifactError(f"{name} exceeds its metadata byte cap")
            return bytes(content)

    def write_bytes(self, name: str, content: bytes) -> None:
        """Write and close a new owned file; a short write is an error."""
        with self.create_file(name) as target:
            if target.write(content) != len(content):
                raise OSError("Short artifact metadata write")

    def unlink(self, name: str) -> None:
        """Remove only a registered file whose directory entry still matches its identity."""
        self.verify()
        expected = self.files[name]
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not os.path.samestat(current, expected):
            raise ArtifactError("Artifact file replaced; replacement retained")
        os.unlink(name, dir_fd=self.fd)
        del self.files[name]

    def move_file(self, name: str, destination: "SafeDirectory", new_name: str) -> None:
        """Promote a registered file using native no-replace semantics, without pathname reopening."""
        _component(new_name)
        self.verify()
        destination.verify()
        expected = self.files[name]
        if not os.path.samestat(os.stat(name, dir_fd=self.fd, follow_symlinks=False), expected):
            raise ArtifactError("Artifact file identity changed before promotion")
        if not self._inside_namespace() or not destination._inside_namespace():
            raise ArtifactError("File promotion requires private namespace handles")
        if self.native is None:
            raise ArtifactError("File promotion has no native installation backend")
        self.native.move(self.fd, name, destination.fd, new_name)
        if not os.path.samestat(
            os.stat(new_name, dir_fd=destination.fd, follow_symlinks=False), expected,
        ):
            raise ArtifactError("Promoted file identity changed; replacement retained")
        destination.files[new_name] = self.files.pop(name)

    def move_child(self, name: str, destination: "SafeDirectory") -> None:
        """Transfer an owned subtree using the same no-replace installation and identity protocol."""
        destination.install(self.children[name], name)

    def install(self, child: "SafeDirectory", name: str) -> None:
        """Expose a pre-opened private child without replacing or acquiring any public entry.

        The source namespace must remain alive. The same held inode is checked
        after the native operation before changing ownership bookkeeping; an
        unexpected target is never adopted, written into, or deleted.
        Transfers between distinct private namespaces are rejected: the source
        root must retain authority/lifetime until the installed child is closed.
        """
        _component(name)
        if not child.owned or not child._inside_namespace() or child.namespace is child:
            raise ArtifactError("Installation requires an owned private child")
        if self._inside_namespace() and self.namespace is not child.namespace:
            raise ArtifactError("Cross-namespace directory installation is unsupported")
        child.verify()
        self.verify()
        if child.parent is None or child.native is None:
            raise ArtifactError("Installation source lacks private ownership context")
        if not self._inside_namespace():
            self.require_trusted_anchor(child.native)
        previous, previous_name = child.parent, child.name
        child.native.move(previous.fd, previous_name, self.fd, name)
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not os.path.samestat(current, child.identity):
            raise ArtifactError("Installed directory identity changed; replacement retained")
        del previous.children[previous_name]
        self.children[name] = child
        child.parent, child.name = self, name
        child.verify()

    def _reclaim(self) -> None:
        """Quarantine an installed entry before deletion; never delete a raced replacement.

        If the atomic move captured a replacement, restore it with no-replace
        semantics when possible. A competing name blocks restoration, so report
        the retained private location without deleting either object.
        """
        self.verify()
        if self.namespace is None or self.native is None or self.parent is None:
            raise ArtifactError("Cleanup lacks its retained private ownership context")
        root, previous, previous_name = self.namespace, self.parent, self.name
        root.verify()
        quarantine = "cleanup-" + uuid4().hex
        self.native.move(previous.fd, previous_name, root.fd, quarantine)
        current = os.stat(quarantine, dir_fd=root.fd, follow_symlinks=False)
        if not os.path.samestat(current, self.identity):
            primary = ArtifactError("Cleanup captured a replacement; no content deleted")
            try:
                self.native.move(root.fd, quarantine, previous.fd, previous_name)
            except BaseException as secondary:  # noqa: BLE001 - restoration must preserve the cleanup failure
                report_secondary_failure(
                    primary, secondary,
                    "Replacement restoration; retained at " + root.description() + "/" + quarantine,
                )
            raise primary
        del previous.children[previous_name]
        root.children[quarantine] = self
        self.parent, self.name = root, quarantine
        self.verify()

    def retain(self) -> None:
        """Transfer a complete owned tree to the caller; closing no longer removes it."""
        self.owned = False
        for child in self.children.values():
            child.retain()

    def complete(self, marker: str) -> None:
        """Remove the completion marker as an irreversible publication boundary.

        Exit checks marker absence too, covering cancellation between the unlink
        syscall and Python's subsequent ownership-state update.
        """
        self.publication_marker = marker
        self.unlink(marker)
        self.retain()

    def cleanup(self) -> None:
        """Reclaim exposed owned trees before deleting registered entries in the private namespace."""
        self.verify()
        if self.owned and self.namespace is not None and not self._inside_namespace():
            self._reclaim()
        for child in list(self.children.values()):
            if child.owned:
                child.cleanup()
        for name in list(self.files):
            self.unlink(name)
        if self.owned and self.parent is not None:
            self.verify()
            os.rmdir(self.name, dir_fd=self.parent.fd)
            self.owned = False

    def close(self, primary: BaseException | None = None) -> None:
        """Close all owned handles, preserving an active error and reporting secondary failures."""
        if self.closed:
            return
        failure = primary
        for child in list(self.children.values()):
            try:
                child.close(failure)
            except BaseException as error:  # noqa: BLE001 - finish closing sibling handles
                failure = error
        try:
            os.close(self.fd)
        except OSError as error:
            if failure is not None:
                report_secondary_failure(failure, error, "Artifact directory close")
            else:
                failure = error
        self.closed = True
        if self.owns_parent and self.parent is not None:
            try:
                self.parent.close(failure)
            except BaseException as error:  # noqa: BLE001 - propagate sole parent close failure
                failure = error
        if primary is None and failure is not None:
            raise failure

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Clean only this invocation's uncommitted entries, then close, preserving primary errors."""
        primary = exc
        try:
            if self.publication_marker is not None and not self.exists(self.publication_marker):
                self.retain()
            if self.owned:
                self.cleanup()
        except BaseException as error:  # noqa: BLE001 - cleanup must not mask cancellation/observer failure
            if primary is None:
                primary = error
            else:
                report_secondary_failure(
                    primary, error,
                    "Artifact cleanup; owned staging/output retained (original location "
                    + self.description() + "; current pathname may have changed)",
                )
        finally:
            self.close(primary)
        if exc is None and primary is not None:
            raise primary
        return False


class ContentStore:
    """Digest-addressed files underneath an opened layout directory.

    Writable stores own their blob directories. Read-only stores open each path
    without symlinks; no serialized descriptor is ever used as an unchecked path.
    """

    def __init__(self, directory: SafeDirectory, *, writable: bool = False) -> None:
        """Borrow the layout handle and optionally initialize private content directories."""
        self.directory = directory
        self.blobs: SafeDirectory | None = None
        self.algorithms: dict[str, SafeDirectory] = {}
        if writable:
            self.blobs = directory.mkdir("blobs")
            for algorithm in ("sha256", "sha512"):
                self.algorithms[algorithm] = self.blobs.mkdir(algorithm)

    def promote(self, filename: str, digest: str) -> None:
        """Move a completed temporary receive to a verified digest-derived path."""
        algorithm, encoded = digest_parts(digest)
        self.directory.move_file(filename, self.algorithms[algorithm], encoded)

    def put(self, digest: str, content: bytes) -> None:
        """Store already bounded embedded content in a new digest file."""
        algorithm, encoded = digest_parts(digest)
        self.algorithms[algorithm].write_bytes(encoded, content)

    def verify(self, digest: str, size: int, *, cap: int | None = None) -> bytes | None:
        """Hash a bounded stream and optionally retain capped document bytes.

        Enforce size before accepting each chunk and check the opened file's
        identity and mutation metadata after reading. Empty blobs are valid.
        """
        algorithm, encoded = digest_parts(digest)
        with self.directory.open_child("blobs") as blobs, blobs.open_child(algorithm) as directory:  # noqa: SIM117 - show handle lifetime
            with directory.read_file(encoded) as source:
                before = os.fstat(source.fileno())
                if before.st_size != size:
                    raise ArtifactError("Stored object size differs from descriptor")
                if cap is not None and size > cap:
                    raise ArtifactError("Document exceeds max_manifest_bytes")
                hasher = hashlib.new(algorithm)
                count = 0
                payload = bytearray() if cap is not None else None
                while chunk := source.read(CHUNK_SIZE):
                    count += len(chunk)
                    if count > size:
                        raise ArtifactError("Stored object grew beyond descriptor size")
                    hasher.update(chunk)
                    if payload is not None:
                        payload.extend(chunk)
                after = os.fstat(source.fileno())
                entry = os.stat(encoded, dir_fd=directory.fd, follow_symlinks=False)
                if (
                    not os.path.samestat(before, entry)
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                ):
                    raise ArtifactError("Source object changed while reading")
                directory.verify()
                if count != size or f"{algorithm}:{hasher.hexdigest()}" != digest:
                    raise ArtifactError("Stored object digest or size mismatch")
                return bytes(payload) if payload is not None else None

    def copy_from(self, source: "ContentStore", digest: str, size: int) -> None:
        """Copy one mutable source into private storage with bounded reads, never hard links.

        The graph verifies the copied bytes. A renamed or modified source file
        during copying is rejected even when its initial stat appeared valid.
        """
        algorithm, encoded = digest_parts(digest)
        with source.directory.open_child("blobs") as blobs, blobs.open_child(algorithm) as directory:  # noqa: SIM117 - show handle lifetime
            with directory.read_file(encoded) as incoming:
                before = os.fstat(incoming.fileno())
                if before.st_size != size:
                    raise ArtifactError("Source size differs from descriptor")
                with self.algorithms[algorithm].create_file(encoded) as outgoing:
                    count = 0
                    while chunk := incoming.read(CHUNK_SIZE):
                        count += len(chunk)
                        if count > size:
                            raise ArtifactError("Source grew beyond descriptor size")
                        if outgoing.write(chunk) != len(chunk):
                            raise OSError("Short snapshot write")
                    after = os.fstat(incoming.fileno())
                    entry = os.stat(encoded, dir_fd=directory.fd, follow_symlinks=False)
                    if (
                        count != size or not os.path.samestat(before, entry)
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    ):
                        raise ArtifactError("Source changed during snapshot")
                    directory.verify()

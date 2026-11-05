"""Fail-closed native no-replace renames and namespace access-policy checks.

Linux and Darwin are the only supported publication backends. Permission/ACL
checks exclude other unprivileged identities, not hostile processes sharing the
effective user identity or administrative privilege. Those principals must be
trusted for the entire private-namespace lifetime.
"""

import ctypes
import errno
import os
import sys
from typing import final

from regshape.libs._http import report_secondary_failure
from regshape.libs.errors import ArtifactError


@final
class NativeFilesystem:
    """Checked platform bindings; no rename/replace or pathname fallback is permitted."""

    def __init__(self) -> None:
        """Resolve required native entry points before any storage creation.

        Filesystem support still requires an invocation-private runtime probe.
        No raw syscall numbers or assumptions about processor architecture are used.
        """
        self.platform = sys.platform
        if self.platform == "darwin":
            symbol, self.flag = "renameatx_np", 0x00000004  # RENAME_EXCL
        elif self.platform.startswith("linux"):
            symbol, self.flag = "renameat2", 1  # RENAME_NOREPLACE
        else:
            raise ArtifactError("Native no-replace artifact publication is unsupported on this platform")
        self.library = ctypes.CDLL(None, use_errno=True)
        if not hasattr(self.library, symbol):
            raise ArtifactError("Required native no-replace rename entry point is unavailable")
        self.rename = getattr(self.library, symbol)
        self.rename.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        ]
        self.rename.restype = ctypes.c_int
        if self.platform == "darwin":
            self._configure_darwin_acl()
        elif not hasattr(os, "getxattr"):
            raise ArtifactError("Handle-relative namespace ACL inspection is unavailable")

    def _configure_darwin_acl(self) -> None:
        """Bind Darwin's extended ACL functions with their actual C pointer types."""
        for symbol in ("acl_get_fd_np", "acl_get_entry", "acl_get_tag_type", "acl_free"):
            if not hasattr(self.library, symbol):
                raise ArtifactError("Darwin namespace ACL inspection is unavailable")
        self.library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        self.library.acl_get_fd_np.restype = ctypes.c_void_p
        self.library.acl_get_entry.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
        ]
        self.library.acl_get_entry.restype = ctypes.c_int
        self.library.acl_get_tag_type.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ]
        self.library.acl_get_tag_type.restype = ctypes.c_int
        self.library.acl_free.argtypes = [ctypes.c_void_p]
        self.library.acl_free.restype = ctypes.c_int

    def move(self, source_fd: int, source: str, target_fd: int, target: str) -> None:
        """Atomically rename one validated relative component, refusing any existing target.

        Callers retain the source handle and check its identity after installation.
        Unsupported filesystems, cross-device moves and permission failures propagate;
        there is no overwrite, copy or check-then-rename fallback.
        """
        ctypes.set_errno(0)
        result = self.rename(
            source_fd, os.fsencode(source), target_fd, os.fsencode(target), self.flag,
        )
        if result != 0:
            code = ctypes.get_errno()
            if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV):
                raise ArtifactError("Filesystem does not support required atomic no-replace installation")
            raise OSError(code, os.strerror(code))

    def check_acl(self, fd: int) -> None:
        """Reject ACL grants outside the conservative namespace policy, using the opened handle.

        Linux extended access/default ACLs are rejected. Darwin deny-only ACLs
        (including the standard home-directory delete denial) are accepted;
        any allow entry is rejected, even a harmless one. Inspection errors fail
        closed rather than treating an unreadable ACL as absent.
        """
        if self.platform == "darwin":
            self._check_darwin_acl(fd)
            return
        for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                os.getxattr(fd, attribute)  # type: ignore[attr-defined]  # Linux-only API
            except OSError as error:
                if error.errno == errno.ENODATA:
                    continue
                raise ArtifactError("Cannot establish trusted namespace ACL policy") from error
            raise ArtifactError("Extended ACLs are unsupported for trusted namespace bootstrap")

    def _check_darwin_acl(self, fd: int) -> None:
        """Inspect/free one Darwin extended ACL; preserve immutable primary exceptions.

        Darwin returns ENOENT for an absent extended ACL on a valid open handle.
        Its acl_get_entry API reports EINVAL at the end of a valid copied ACL.
        The copy is private and cannot be mutated by another filesystem writer.
        """
        ctypes.set_errno(0)
        acl = self.library.acl_get_fd_np(fd, 0x00000100)  # ACL_TYPE_EXTENDED
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:
                return
            raise ArtifactError("Cannot inspect Darwin namespace ACL")
        primary: BaseException | None = None
        try:
            entry_id = 0  # ACL_FIRST_ENTRY
            while True:
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                result = self.library.acl_get_entry(acl, entry_id, ctypes.byref(entry))
                if result != 0:
                    if ctypes.get_errno() == errno.EINVAL:
                        break
                    raise ArtifactError("Cannot enumerate Darwin namespace ACL")
                tag = ctypes.c_int()
                if self.library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                    raise ArtifactError("Cannot interpret Darwin namespace ACL entry")
                if tag.value != 2:  # ACL_EXTENDED_DENY
                    raise ArtifactError("Allow ACL entries are unsupported for trusted namespace bootstrap")
                entry_id = -1  # ACL_NEXT_ENTRY
        except BaseException as error:
            primary = error
            raise
        finally:
            if self.library.acl_free(acl) != 0:
                failure = ArtifactError("Cannot free namespace ACL inspection data")
                if primary is None:
                    raise failure
                report_secondary_failure(primary, failure, "Namespace ACL cleanup")

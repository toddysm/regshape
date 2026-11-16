"""Private-namespace trust, native no-replace and ownership-race regressions.

The private namespace assumes exclusive caller control over its effective user
identity. Fault injection inside that boundary tests observed identity drift,
not isolation from arbitrary hostile same-user access. Public installation and
cleanup races must never adopt or delete a substituted object.
"""

import ctypes
import errno
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from regshape.libs.artifacts import ArtifactError, pull_artifact
from regshape.libs.artifacts.native import NativeFilesystem
from regshape.libs.artifacts.storage import (
    INCOMPLETE,
    SafeDirectory,
    require_safe_filesystem,
)
from regshape.tests.artifact_fixtures import Registry, document


@pytest.fixture(autouse=True)
def supported_namespace(request: pytest.FixtureRequest) -> None:
    """Run unsupported-backend guards everywhere; skip real POSIX operations where unavailable."""
    if "unsupported_platform" not in request.node.name:
        try:
            require_safe_filesystem()
            NativeFilesystem()
        except ArtifactError:
            pytest.skip("No supported native artifact namespace backend")


@pytest.mark.parametrize("platform", ["win32", "freebsd14", "unknown"])
def test_unsupported_platform_native_backend(platform: str) -> None:
    """Unsupported native platforms are rejected without loading a library or creating a path."""
    with patch("regshape.libs.artifacts.native.sys.platform", platform), patch(
        "regshape.libs.artifacts.native.ctypes.CDLL",
    ) as load, pytest.raises(ArtifactError, match="unsupported"):
        NativeFilesystem()
    load.assert_not_called()


def test_missing_native_symbol_fails_before_creation(tmp_path: Path) -> None:
    """Absent no-replace bindings cannot fall back to ordinary rename or mkdir reservation."""
    registry = Registry(document(children=[]))
    with patch(
        "regshape.libs.artifacts.native.ctypes.CDLL", return_value=SimpleNamespace(),
    ), pytest.raises(ArtifactError, match="entry point"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert not registry.calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o773])
def test_untrusted_anchor_fails_before_creation(tmp_path: Path, mode: int) -> None:
    """Nonsticky group/world-writable bootstrap parents fail before staging/network effects."""
    tmp_path.chmod(mode)
    registry = Registry(document(children=[]))
    with pytest.raises(ArtifactError, match="untrusted writes"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert not registry.calls and not list(tmp_path.iterdir())


def test_unknown_acl_fails_before_creation(tmp_path: Path) -> None:
    """An uninspectable/delegated access policy is never interpreted as trusted by default."""
    registry = Registry(document(children=[]))
    with patch.object(
        NativeFilesystem, "check_acl", side_effect=ArtifactError("untrusted ACL"),
    ), pytest.raises(ArtifactError, match="untrusted ACL"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert not registry.calls and not list(tmp_path.iterdir())


def test_untrusted_owner_is_not_a_namespace_anchor(tmp_path: Path) -> None:
    """A retained path with another unprivileged owner cannot bootstrap a private namespace."""
    with SafeDirectory.open(tmp_path) as parent:
        original = os.fstat

        def foreign_owner(fd: int):
            """Substitute only the parent's ownership metadata, leaving open-object identity stable."""
            info = original(fd)
            if fd != parent.fd:
                return info
            values = list(info)
            values[4] = os.geteuid() + 10000
            return os.stat_result(values)

        with patch.object(os, "fstat", foreign_owner), pytest.raises(ArtifactError, match="untrusted owner"):
            SafeDirectory.private_namespace(parent)
    assert not list(tmp_path.iterdir())


def test_public_mkdir_cannot_acquire_ownership(tmp_path: Path) -> None:
    """The original vulnerable public mkdir/stat/open acquisition route is no longer available."""
    with SafeDirectory.open(tmp_path) as parent, pytest.raises(ArtifactError, match="private namespace"):
        parent.mkdir("visible-reservation")
    assert not list(tmp_path.iterdir())


def test_private_namespace_policy_lifetime_and_owned_handle_cleanup(tmp_path: Path) -> None:
    """Root permissions are rechecked while live and all private handles close on successful exit."""
    with SafeDirectory.open(tmp_path) as parent, SafeDirectory.private_namespace(parent) as namespace:
        root_fd = namespace.fd
        child = namespace.temporary()
        child_fd = child.fd
        os.fchmod(root_fd, 0o777)
        try:
            with pytest.raises(ArtifactError, match="permissions changed"):
                child.write_bytes("must-not-exist", b"data")
        finally:
            os.fchmod(root_fd, 0o700)
        assert not child.exists("must-not-exist")
    assert not list(tmp_path.iterdir())
    for fd in (root_fd, child_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_directory_transfer_between_private_namespaces_is_rejected(tmp_path: Path) -> None:
    """A child cannot acquire a destination lifetime shorter than its retained source namespace."""
    with SafeDirectory.open(tmp_path) as parent, SafeDirectory.private_namespace(
        parent,
    ) as first, SafeDirectory.private_namespace(parent) as second, first.temporary() as child:
        with pytest.raises(ArtifactError, match="Cross-namespace"):
            second.install(child, "moved")
        child.verify()
        assert not second.exists("moved")
    assert not list(tmp_path.iterdir())


def test_filesystem_probe_fails_before_registry_access(tmp_path: Path) -> None:
    """Unavailable filesystem no-replace support fails in owned probe storage, not a public path."""
    registry = Registry(document(children=[]))
    with patch.object(
        NativeFilesystem, "move", side_effect=ArtifactError("unsupported filesystem"),
    ), pytest.raises(ArtifactError, match="unsupported filesystem"):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    assert not registry.calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_substitution_before_public_acquisition_cannot_be_adopted(tmp_path: Path, kind: str) -> None:
    """A preexisting object installed at the output name wins; its identity/data remain unchanged."""
    victim = tmp_path / "preexisting"
    if kind == "directory":
        victim.mkdir()
    elif kind == "file":
        victim.write_bytes(b"untouched")
    else:
        victim.symlink_to(tmp_path / "missing")
    identity = victim.lstat()
    original_install, original_open = SafeDirectory.install, SafeDirectory.open_child

    def race(parent: SafeDirectory, child: SafeDirectory, name: str) -> None:
        """Substitute the object immediately before installation, the old acquisition boundary."""
        if name == "out":
            victim.rename(tmp_path / "out")
        original_install(parent, child, name)

    def reject_output_open(parent: SafeDirectory, name: str) -> SafeDirectory:
        """Prove ownership never comes from opening the public output pathname."""
        assert name != "out"
        return original_open(parent, name)

    with patch.object(SafeDirectory, "install", race), patch.object(
        SafeDirectory, "open_child", reject_output_open,
    ), pytest.raises(FileExistsError):
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    assert os.path.samestat((tmp_path / "out").lstat(), identity)
    assert [entry.name for entry in tmp_path.iterdir()] == ["out"]
    if kind == "directory":
        assert not list((tmp_path / "out").iterdir())
    elif kind == "file":
        assert (tmp_path / "out").read_bytes() == b"untouched"


@pytest.mark.parametrize("when", ["before-native-move", "after-native-move"])
def test_installation_identity_drift_never_adopts_replacement(tmp_path: Path, when: str, caplog) -> None:
    """Native installation is followed by held-inode checks, not acquisition of whatever now exists."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "sentinel").write_bytes(b"untouched")
    identity = victim.stat()
    original = NativeFilesystem.move

    def substitute(native: NativeFilesystem, source_fd: int, source: str, target_fd: int, target: str) -> None:
        """Inject either a private-boundary breach or a public post-install substitution."""
        if target != "out":
            original(native, source_fd, source, target_fd, target)
            return
        if when == "before-native-move":
            os.rename(source, "held-original", src_dir_fd=source_fd, dst_dir_fd=source_fd)
            os.rename(victim, source, dst_dir_fd=source_fd)
            original(native, source_fd, source, target_fd, target)
        else:
            original(native, source_fd, source, target_fd, target)
            os.rename(target, "held-original", src_dir_fd=target_fd, dst_dir_fd=target_fd)
            os.rename(victim, target, dst_dir_fd=target_fd)

    with patch.object(NativeFilesystem, "move", substitute), pytest.raises(
        ArtifactError, match="Installed directory identity",
    ):
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    assert os.path.samestat((tmp_path / "out").stat(), identity)
    assert sorted(entry.name for entry in (tmp_path / "out").iterdir()) == ["sentinel"]
    assert (tmp_path / "out" / "sentinel").read_bytes() == b"untouched"
    assert "retained" in caplog.text


def test_failed_installed_output_is_reclaimed_before_cleanup(tmp_path: Path) -> None:
    """An ordinary precommit failure cleans the whole owned output through private quarantine."""
    with patch.object(SafeDirectory, "complete", side_effect=OSError("before commit")), pytest.raises(
        OSError, match="before commit",
    ):
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("restore_collision", [False, True])
def test_cleanup_substitution_never_deletes_replacement(tmp_path: Path, restore_collision: bool, caplog) -> None:
    """A raced cleanup move checks identity before deleting; restoration cannot overwrite a new winner."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "sentinel").write_bytes(b"untouched")
    original = NativeFilesystem.move
    primary = OSError("before commit")

    def substitute(native: NativeFilesystem, source_fd: int, source: str, target_fd: int, target: str) -> None:
        """Race exactly after cleanup's identity check but before its atomic quarantine move."""
        if source == "out" and target.startswith("cleanup-"):
            os.rename(source, "held-original", src_dir_fd=source_fd, dst_dir_fd=source_fd)
            os.rename(victim, source, dst_dir_fd=source_fd)
            original(native, source_fd, source, target_fd, target)
            if restore_collision:
                (tmp_path / "out").mkdir()
                (tmp_path / "out" / "winner").write_bytes(b"new winner")
        else:
            original(native, source_fd, source, target_fd, target)

    with patch.object(SafeDirectory, "complete", side_effect=primary), patch.object(
        NativeFilesystem, "move", substitute,
    ), pytest.raises(OSError) as caught:
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    assert caught.value is primary
    assert (tmp_path / "held-original" / INCOMPLETE).exists()
    if restore_collision:
        retained = list(tmp_path.glob(".regshape-private-*/cleanup-*/sentinel"))
        assert len(retained) == 1 and retained[0].read_bytes() == b"untouched"
        assert (tmp_path / "out" / "winner").read_bytes() == b"new winner"
    else:
        assert (tmp_path / "out" / "sentinel").read_bytes() == b"untouched"
        assert not list(tmp_path.glob(".regshape-private-*"))
    assert "retained" in caplog.text


@pytest.mark.parametrize("platform,flag", [("linux", 1), ("darwin", 4)])
@pytest.mark.parametrize("code", [
    errno.EEXIST, errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV, errno.EACCES,
])
def test_native_flags_and_fail_closed_errors(platform: str, flag: int, code: int) -> None:
    """Native flags are exact and unsupported/collision errors never trigger a replacement fallback."""
    rename = Mock(side_effect=lambda *args: (ctypes.set_errno(code), -1)[1])
    library = SimpleNamespace(
        renameat2=rename, renameatx_np=rename, acl_get_fd_np=Mock(),
        acl_get_entry=Mock(), acl_get_tag_type=Mock(), acl_free=Mock(),
    )
    with patch("regshape.libs.artifacts.native.sys.platform", platform), patch(
        "regshape.libs.artifacts.native.ctypes.CDLL", return_value=library,
    ), patch.object(
        os, "getxattr", create=True,
    ):
        native = NativeFilesystem()
        ctypes.set_errno(code)
        error = FileExistsError if code == errno.EEXIST else (
            OSError if code == errno.EACCES else ArtifactError
        )
        with pytest.raises(error):
            native.move(10, "source", 11, "target")
    rename.assert_called_once_with(10, b"source", 11, b"target", flag)


@pytest.mark.parametrize("tag", [1, 2])
def test_darwin_acl_allow_rejected_and_deny_freed(tag: int) -> None:
    """Darwin ACL entries are checked rather than inferring safety from ordinary mode bits."""
    entries = 0

    def get_entry(acl, entry_id, result) -> int:
        """Return one valid copied ACL entry, then Darwin's documented end-of-list result."""
        nonlocal entries
        entries += 1
        if entries == 1:
            result._obj.value = 123
            return 0
        ctypes.set_errno(errno.EINVAL)
        return -1

    def get_tag(entry, result) -> int:
        """Populate the native output parameter with the selected allow/deny tag."""
        result._obj.value = tag
        return 0

    library = SimpleNamespace(
        renameatx_np=Mock(), acl_get_fd_np=Mock(return_value=123),
        acl_get_entry=Mock(side_effect=get_entry), acl_get_tag_type=Mock(side_effect=get_tag),
        acl_free=Mock(return_value=0),
    )
    with patch("regshape.libs.artifacts.native.sys.platform", "darwin"), patch(
        "regshape.libs.artifacts.native.ctypes.CDLL", return_value=library,
    ):
        native = NativeFilesystem()
        if tag == 2:
            native.check_acl(10)
        else:
            with pytest.raises(ArtifactError, match="Allow ACL"):
                native.check_acl(10)
    library.acl_free.assert_called_once_with(123)


@pytest.mark.parametrize("code", [errno.ENOENT, errno.EACCES, errno.ENOTSUP])
def test_darwin_acl_absence_is_distinct_from_inspection_failure(code: int) -> None:
    """Only native absence permits bootstrap; permission/unsupported inspection errors fail closed."""
    def absent(fd, kind) -> None:
        """Model a NULL ACL result carrying the selected native errno."""
        ctypes.set_errno(code)

    library = SimpleNamespace(
        renameatx_np=Mock(), acl_get_fd_np=Mock(side_effect=absent),
        acl_get_entry=Mock(), acl_get_tag_type=Mock(), acl_free=Mock(),
    )
    with patch("regshape.libs.artifacts.native.sys.platform", "darwin"), patch(
        "regshape.libs.artifacts.native.ctypes.CDLL", return_value=library,
    ):
        native = NativeFilesystem()
        if code == errno.ENOENT:
            native.check_acl(10)
        else:
            with pytest.raises(ArtifactError, match="inspect"):
                native.check_acl(10)
    library.acl_free.assert_not_called()


@pytest.mark.parametrize("state", ["absent", "present", "unreadable"])
def test_linux_acl_policy_rejects_delegation_and_unknown_access(state: str) -> None:
    """Linux extended/default ACL absence is required; unreadable policy is not assumed safe."""
    def xattr(fd, attribute):
        """Return or fail the Linux-only fd xattr call without reading a platform-specific fixture."""
        if state == "present":
            return b"extended ACL"
        raise OSError(errno.ENODATA if state == "absent" else errno.EACCES, "ACL probe")

    with patch("regshape.libs.artifacts.native.sys.platform", "linux"), patch(
        "regshape.libs.artifacts.native.ctypes.CDLL",
        return_value=SimpleNamespace(renameat2=Mock()),
    ), patch.object(os, "getxattr", side_effect=xattr, create=True):
        native = NativeFilesystem()
        if state == "absent":
            native.check_acl(10)
        else:
            with pytest.raises(ArtifactError, match="ACL"):
                native.check_acl(10)

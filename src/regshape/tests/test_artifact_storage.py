"""Safe-directory publication, snapshot, cancellation and bounded-receive regressions.

Supported-platform tests exercise real local handles. The unsupported-platform
guard test runs everywhere and proves rejection before filesystem/network writes.
"""

import dataclasses
import gzip
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from regshape.libs.artifacts import (
    ArtifactError,
    ArtifactLimits,
    DirectorySnapshot,
    pull_artifact,
)
from regshape.libs.artifacts.storage import (
    CHUNK_SIZE,
    INCOMPLETE,
    SafeDirectory,
    SafeFile,
    require_safe_filesystem,
)
from regshape.libs.blobs import get_blob
from regshape.libs.blobs.operations import _bounded_blob_redirect
from regshape.libs.errors import BlobError
from regshape.libs.layout import validate_layout
from regshape.tests.artifact_fixtures import (
    Registry,
    StreamResponse,
    address,
    document,
    edge,
    nested_registry,
    write_layout,
)


@pytest.fixture(autouse=True)
def supported_storage(request: pytest.FixtureRequest) -> None:
    """Skip actual no-follow operations where unsupported, never skip the guard regression."""
    if "unsupported_platform" not in request.node.name:
        try:
            require_safe_filesystem()
        except ArtifactError:
            pytest.skip("This platform deliberately rejects safe artifact filesystem operations")


def test_unsupported_platform_fails_before_effects(tmp_path: Path) -> None:
    """Missing handle primitives abort pull and snapshot before creating anything or making GETs."""
    registry = Registry(document(children=[]))
    with patch.object(os, "supports_dir_fd", set()), patch.object(os, "mkdir") as mkdir:
        with pytest.raises(ArtifactError, match="unsupported"):
            pull_artifact(registry, "repo", "moving", tmp_path / "out")
        with pytest.raises(ArtifactError, match="unsupported"), DirectorySnapshot(tmp_path):
            pytest.fail("Unsupported snapshot entered")
        mkdir.assert_not_called()
    assert not registry.calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
def test_existing_destination_rejected_pre_network(tmp_path: Path, kind: str) -> None:
    """Existing output of any kind remains untouched, including dangling links."""
    output = tmp_path / "out"
    if kind == "file":
        output.write_bytes(b"existing")
    elif kind == "directory":
        output.mkdir()
    else:
        output.symlink_to(tmp_path / ("missing" if kind == "dangling" else "target"))
    registry = Registry(document(children=[]))
    with pytest.raises(ArtifactError, match="already exists"):
        pull_artifact(registry, "repo", "moving", output)
    assert not registry.calls
    assert len(list(tmp_path.iterdir())) == 1


def test_destination_created_during_download_is_not_overwritten(tmp_path: Path) -> None:
    """Exclusive final reservation loses a race without modifying the winner's directory."""
    output = tmp_path / "out"
    registry = Registry(document(children=[]))
    registry.after_get = lambda path: output.mkdir()
    with pytest.raises(FileExistsError):
        pull_artifact(registry, "repo", "moving", output)
    assert list(output.iterdir()) == []
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("which", ["destination", "staging", "parent"])
def test_renamed_parent_or_reservation_never_redirects_writes(tmp_path: Path, which: str, caplog) -> None:
    """Rename/symlink replacements cannot redirect publication or cleanup to an attacker tree."""
    base = tmp_path / "base"
    base.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "sentinel").write_bytes(b"untouched")
    original_write = SafeDirectory.write_bytes
    original_install = SafeDirectory.install
    replaced = False

    def replace_stage(directory: SafeDirectory, name: str, data: bytes) -> None:
        """Inject a private-namespace breach after acquisition; observed replacement must still abort."""
        nonlocal replaced
        original_write(directory, name, data)
        if name != INCOMPLETE or replaced or which != "staging":
            return
        replaced = True
        staging = next(base.glob(".regshape-private-*/.regshape-staging-*"))
        staging.rename(base / "moved-stage")
        staging.symlink_to(other, target_is_directory=True)

    def replace_after_install(parent: SafeDirectory, child: SafeDirectory, name: str) -> None:
        """Replace the installed public entry/ancestor before marker removal, never acquiring it again."""
        nonlocal replaced
        original_install(parent, child, name)
        if name != "out" or replaced or which == "staging":
            return
        replaced = True
        if which == "destination":
            (base / "out").rename(base / "moved")
            (base / "out").symlink_to(other, target_is_directory=True)
        else:
            base.rename(tmp_path / "moved-base")
            base.symlink_to(other, target_is_directory=True)

    with patch.object(SafeDirectory, "write_bytes", replace_stage), patch.object(
        SafeDirectory, "install", replace_after_install,
    ), pytest.raises(
        ArtifactError, match="identity changed",
    ):
        pull_artifact(Registry(document(children=[])), "repo", "moving", base / "out")
    assert replaced
    assert sorted(item.name for item in other.iterdir()) == ["sentinel"]
    assert (other / "sentinel").read_bytes() == b"untouched"
    assert "retained" in caplog.text
    if which == "destination":
        assert (base / "out").is_symlink()
        assert (base / "moved" / INCOMPLETE).exists()
    elif which == "parent":
        assert base.is_symlink()
        assert (tmp_path / "moved-base" / "out" / INCOMPLETE).exists()


def test_publication_order_and_marker_visibility(tmp_path: Path) -> None:
    """The incomplete marker spans all publication writes; index is last and completion is explicit."""
    output = tmp_path / "out"
    names = []
    original = SafeDirectory.write_bytes
    original_install = SafeDirectory.install

    def record(directory: SafeDirectory, name: str, content: bytes) -> None:
        """Require private metadata writes before the output name becomes visible."""
        if name in (INCOMPLETE, "oci-layout", ".index-pending"):
            names.append(name)
            assert not output.exists()
            if name != INCOMPLETE:
                assert directory.exists(INCOMPLETE)
        original(directory, name, content)

    def inspect_installed(parent: SafeDirectory, child: SafeDirectory, name: str) -> None:
        """The installed graph remains rejected while its completion marker is still present."""
        original_install(parent, child, name)
        if name == "out":
            assert (output / "index.json").exists()
            with pytest.raises(ArtifactError, match="Incomplete"):
                validate_layout(output, recursive=True)

    with patch.object(SafeDirectory, "write_bytes", record), patch.object(
        SafeDirectory, "install", inspect_installed,
    ):
        pull_artifact(Registry(document(children=[])), "repo", "moving", output)
    assert names == [INCOMPLETE, "oci-layout", ".index-pending"]
    assert (output / "index.json").exists() and not (output / INCOMPLETE).exists()


def test_cancel_immediately_after_marker_removal_retains_publication(tmp_path: Path) -> None:
    """Cancellation at the unlink-to-retain gap cannot delete an already readable complete layout."""
    original = SafeDirectory.unlink
    cancellation = KeyboardInterrupt()

    def interrupt(directory: SafeDirectory, name: str) -> None:
        """Raise immediately after the successful completion-marker removal syscall."""
        original(directory, name)
        if name == INCOMPLETE:
            raise cancellation

    with patch.object(SafeDirectory, "unlink", interrupt), pytest.raises(KeyboardInterrupt) as caught:
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    assert caught.value is cancellation
    assert not (tmp_path / "out" / INCOMPLETE).exists()
    validate_layout(tmp_path / "out", recursive=True)


@pytest.mark.parametrize("operation", ["pull", "validate", "snapshot"])
def test_parent_components_are_rejected_before_normalization(tmp_path: Path, operation: str) -> None:
    """A symlink followed by dot-dot must not vanish during lexical path normalization."""
    registry = Registry(document(children=[]))
    local = tmp_path / "local"
    local.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (local / "jump").symlink_to(other, target_is_directory=True)
    path = local / "jump" / ".." / "selected"
    with pytest.raises(ArtifactError, match="Parent path components"):
        if operation == "pull":
            pull_artifact(registry, "repo", "moving", path)
        elif operation == "validate":
            validate_layout(path, recursive=True)
        else:
            with DirectorySnapshot(path):
                pytest.fail("Unsafe source accepted")
    assert not registry.calls and not list(other.iterdir())
    assert [item.name for item in local.iterdir()] == ["jump"]


@pytest.mark.parametrize("filename", ["oci-layout", ".index-pending"])
def test_publication_write_failure_removes_owned_output(tmp_path: Path, filename: str) -> None:
    """A failed metadata write has no successful output/result and cleans only its reservation."""
    original = SafeDirectory.write_bytes

    def fail(directory: SafeDirectory, name: str, content: bytes) -> None:
        """Model disk-full only at the selected publication boundary."""
        if name == filename:
            raise OSError("disk full")
        original(directory, name, content)

    with patch.object(SafeDirectory, "write_bytes", fail), pytest.raises(OSError, match="disk full"):
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    assert not list(tmp_path.iterdir())


def test_post_publication_failure_retains_complete_output(tmp_path: Path) -> None:
    """A sole close failure after commit reports failure but cannot roll back a readable artifact."""
    original = SafeDirectory.close
    raised = False

    def fail_after_close(directory: SafeDirectory, primary=None) -> None:
        """Inject a one-shot independent output close diagnostic after handles are actually closed."""
        nonlocal raised
        original(directory, primary)
        if directory.name == "out" and not directory.owned and not raised:
            raised = True
            raise OSError("post-publication close")

    with patch.object(SafeDirectory, "close", fail_after_close), pytest.raises(OSError, match="post-publication"):
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out")
    validate_layout(tmp_path / "out", recursive=True)
    assert not (tmp_path / "out" / INCOMPLETE).exists()


@dataclasses.dataclass(frozen=True)
class FrozenFailure(Exception):
    """Immutable caller exception that rejects manual traceback/context attribute assignment."""

    message: str


@pytest.mark.parametrize("phase", ["preparing", "transferring", "received", "verifying", "stored"])
@pytest.mark.parametrize("primary", [FrozenFailure("immutable"), KeyboardInterrupt()])
def test_callback_failure_and_cancel_keep_exact_primary(tmp_path: Path, phase: str, primary: BaseException) -> None:
    """All observer boundaries preserve the exact exception and stop recursive notification/cleanup."""
    registry = Registry(document(children=[]))
    seen = []

    def observer(event) -> None:
        """Raise the sentinel at one phase and reject any later notification."""
        seen.append(event.phase)
        if event.phase == phase:
            raise primary

    with pytest.raises(type(primary)) as caught:
        pull_artifact(registry, "repo", "moving", tmp_path / "out", progress_callback=observer)
    assert caught.value is primary
    assert seen[-1] == phase
    assert all(response.close_calls == 1 for response in registry.responses)
    assert not list(tmp_path.iterdir())


def test_secondary_cleanup_and_logging_cannot_replace_immutable_error(tmp_path: Path, caplog) -> None:
    """Owned cleanup/logging failures never assign attributes on or replace an immutable error."""
    primary = FrozenFailure("immutable")

    def observer(event) -> None:
        """Fail once the completed raw receive has been registered as owned staging."""
        if event.phase == "verifying":
            raise primary

    with (
        patch.object(SafeDirectory, "unlink", side_effect=OSError("cleanup")),
        patch("regshape.libs._http.logger.error", side_effect=RuntimeError("logging")),
        pytest.raises(FrozenFailure) as caught,
    ):
        pull_artifact(Registry(document(children=[])), "repo", "moving", tmp_path / "out", progress_callback=observer)
    assert caught.value is primary
    assert not (tmp_path / "out").exists()
    assert list(tmp_path.glob(".regshape-private-*/.regshape-staging-*"))


def test_failure_observer_cannot_replace_primary(tmp_path: Path) -> None:
    """A second observer exception while reporting malformed JSON preserves the parsing error."""
    secondary = FrozenFailure("observer")

    def observer(event) -> None:
        """Fail only on the once-only failure notification."""
        if event.phase == "failed":
            raise secondary

    with pytest.raises(ArtifactError, match="JSON"):
        pull_artifact(Registry(b"invalid"), "repo", "moving", tmp_path / "out", progress_callback=observer)
    assert not list(tmp_path.iterdir())


def test_snapshot_is_independent_and_ignores_authoring_extras(tmp_path: Path) -> None:
    """Snapshot bytes survive original-source edits; extras are neither copied nor uploaded."""
    _, root, objects = nested_registry()
    source = tmp_path / "source"
    write_layout(source, root, objects)
    (source / ".regshape-stage.json").write_text("authoring state")
    (source / "unreferenced").write_bytes(b"ignored")
    with DirectorySnapshot(source) as snapshot:
        fd = snapshot.directory.fd
        assert len(snapshot.graph.objects) == len(objects)
        for item in snapshot.graph.objects:
            (source / "blobs" / item.digest.replace(":", "/")).write_bytes(b"changed")
            assert snapshot.store.verify(item.digest, item.size, cap=max(1, item.size)) == objects[item.digest]
        assert not snapshot.directory.exists(".regshape-stage.json")
        assert not snapshot.directory.exists("unreferenced")
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("which", ["blob", "algorithm", "blobs", "marker", "wrapper", "source"])
def test_source_symlinks_are_rejected(tmp_path: Path, which: str) -> None:
    """No required source path may be a symlink, including directory and metadata components."""
    root = document(children=[])
    source = tmp_path / "source"
    write_layout(source, root, {})
    paths = {
        "source": source, "blobs": source / "blobs", "algorithm": source / "blobs" / "sha256",
        "blob": source / "blobs" / address(root).replace(":", "/"),
        "marker": source / "oci-layout", "wrapper": source / "index.json",
    }
    target = paths[which]
    moved = target.with_name(target.name + "-original")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(OSError):
        validate_layout(source, recursive=True)
    with pytest.raises(OSError), DirectorySnapshot(source):
        pytest.fail("Unsafe source snapshot entered")


def test_nonregular_source_does_not_block(tmp_path: Path) -> None:
    """Opening FIFO content is nonblocking and rejected before any attempted read."""
    root = document(children=[])
    source = tmp_path / "source"
    write_layout(source, root, {})
    blob = source / "blobs" / address(root).replace(":", "/")
    blob.unlink()
    os.mkfifo(blob)
    with pytest.raises(ArtifactError, match="regular"):
        validate_layout(source, recursive=True)


@pytest.mark.parametrize("snapshot", [False, True])
def test_marker_cap_including_whitespace_boundary(tmp_path: Path, snapshot: bool) -> None:
    """Both validation and snapshot cap the small marker before buffering/parsing whitespace."""
    root = document(children=[])
    source = tmp_path / "source"
    write_layout(source, root, {})
    marker = source / "oci-layout"
    content = marker.read_bytes() + b" " * 50
    marker.write_bytes(content)
    limits = ArtifactLimits(max_layout_marker_bytes=len(content))
    if snapshot:
        with DirectorySnapshot(source, limits=limits):
            pass
        with pytest.raises(ArtifactError, match="byte cap"), DirectorySnapshot(
            source, limits=dataclasses.replace(limits, max_layout_marker_bytes=len(content) - 1),
        ):
            pytest.fail("Oversize marker accepted")
    else:
        validate_layout(source, recursive=True, limits=limits)
        with pytest.raises(ArtifactError, match="byte cap"):
            validate_layout(
                source, recursive=True,
                limits=dataclasses.replace(limits, max_layout_marker_bytes=len(content) - 1),
            )


def test_incomplete_and_corrupt_content_never_snapshot(tmp_path: Path) -> None:
    """A marker or a corrupted blob fails before a snapshot can be used as a push source."""
    root = document(children=[])
    source = tmp_path / "source"
    write_layout(source, root, {})
    (source / INCOMPLETE).write_bytes(b"")
    with pytest.raises(ArtifactError, match="Incomplete"), DirectorySnapshot(source):
        pytest.fail("Incomplete layout accepted")
    (source / INCOMPLETE).unlink()
    (source / "blobs" / address(root).replace(":", "/")).write_bytes(b"x" * len(root))
    with pytest.raises(ArtifactError, match="digest"), DirectorySnapshot(source):
        pytest.fail("Corrupt snapshot accepted")


def test_snapshot_source_replacement_during_copy(tmp_path: Path) -> None:
    """An opened source renamed/replaced during copying cannot pass final association checks."""
    root = document(children=[])
    source = tmp_path / "source"
    write_layout(source, root, {})
    target = source / "blobs" / address(root).replace(":", "/")
    original = SafeDirectory.create_file
    replaced = False

    def swap(directory: SafeDirectory, name: str) -> SafeFile:
        """Replace source just after its handle was opened and before copying begins."""
        nonlocal replaced
        if name == target.name and not replaced:
            replaced = True
            target.rename(target.with_name("old"))
            target.write_bytes(root)
        return original(directory, name)

    with patch.object(SafeDirectory, "create_file", swap), pytest.raises(
        ArtifactError, match="changed",
    ), DirectorySnapshot(source):
        pytest.fail("Replaced source accepted")


@pytest.mark.parametrize("actual,expected", [(b"abcdef", 5), (b"abc", 5)])
def test_blob_trusted_size_checked_before_writes(tmp_path: Path, actual: bytes, expected: int) -> None:
    """Oversize chunks are rejected before disk writes; short bodies fail and clean their file."""
    registry = Registry(b"", {address(actual): actual})
    writes = []
    from regshape.libs.manifests.download import _DownloadFile
    original = _DownloadFile.write

    def record(output, chunk: bytes) -> None:
        """Record accepted writes, not attempted network receipt."""
        writes.append(chunk)
        original(output, chunk)

    with SafeDirectory.open(tmp_path) as directory, patch.object(
        _DownloadFile, "write", record,
    ), pytest.raises(BlobError):
        get_blob(
            registry, "repo", address(actual), "receive", total_size=expected,
            max_bytes=expected, output_dir_fd=directory.fd,
        )
    assert b"".join(writes) == (actual if len(actual) < expected else b"")
    assert not list(tmp_path.iterdir())
    assert registry.responses[0].close_calls == 1


def test_large_blob_is_streamed_with_bounded_heap(tmp_path: Path) -> None:
    """Payload size does not determine response or verification buffering allocations."""
    import tracemalloc

    config = b"a" * (12 * 1024 * 1024)
    root = document(config=edge(config))
    registry = Registry(root, {address(config): config})
    tracemalloc.start()
    try:
        result = pull_artifact(registry, "repo", "moving", tmp_path / "out")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.bytes_stored == len(config) + len(root)
    assert peak < 3 * 1024 * 1024


def test_blob_decoding_and_error_bodies_are_bounded(tmp_path: Path) -> None:
    """Compressed entity expansion and error payloads cannot bypass receive caps."""
    content = b"x" * (4 * CHUNK_SIZE)
    response = StreamResponse(gzip.compress(content), headers={"Content-Encoding": "gzip"})
    registry = Registry(b"")
    with SafeDirectory.open(tmp_path) as directory, patch.object(
        registry, "get", return_value=response,
    ), pytest.raises(BlobError, match="max_bytes"):
        get_blob(registry, "repo", address(content), "out", max_bytes=10, output_dir_fd=directory.fd)
    assert response.close_calls == 1 and not list(tmp_path.iterdir())
    response = StreamResponse(content, status=404)
    with SafeDirectory.open(tmp_path) as directory, patch.object(
        registry, "get", return_value=response,
    ), pytest.raises(BlobError, match="error body"):
        get_blob(registry, "repo", address(content), "out", max_bytes=10, output_dir_fd=directory.fd)
    assert response.close_calls == 1 and not list(tmp_path.iterdir())


def test_blob_redirect_bodies_are_bounded_without_disabling_signed_redirects() -> None:
    """The redirect hook drains at most its cap, preserving Requests redirect/auth policy."""
    response = StreamResponse(b"x" * 65536, status=307, headers={"Location": "https://cdn.example/signed"})
    assert _bounded_blob_redirect(response) is response
    assert response.content == b"" and response.close_calls == 1
    oversize = StreamResponse(b"x" * 65537, status=307)
    with pytest.raises(BlobError, match="redirect body"):
        _bounded_blob_redirect(oversize)
    assert oversize.close_calls == 1


def test_close_failure_after_blob_receipt_prevents_success(tmp_path: Path) -> None:
    """A response close failure prevents completion, removes owned bytes and stays visible."""
    raw = b"blob"
    registry = Registry(b"", {address(raw): raw})
    response = StreamResponse(raw)
    with (
        SafeDirectory.open(tmp_path) as directory,
        patch.object(registry, "get", return_value=response),
        patch.object(response, "close", side_effect=OSError("close failed")),
        pytest.raises(OSError, match="close failed"),
    ):
        get_blob(registry, "repo", address(raw), "out", max_bytes=10, output_dir_fd=directory.fd)
    assert not list(tmp_path.iterdir())


def test_invalid_blob_metadata_cleans_before_completion(tmp_path: Path) -> None:
    """Malformed response metadata fails within ownership contexts, never after completed output."""
    raw = b"blob"
    registry = Registry(b"", {address(raw): raw})
    registry.headers_by_reference[address(raw)] = {"Content-Type": ""}
    events = []
    with SafeDirectory.open(tmp_path) as directory, pytest.raises(ValueError):
        get_blob(
            registry, "repo", address(raw), "out", max_bytes=10, output_dir_fd=directory.fd,
            progress_callback=events.append,
        )
    assert not list(tmp_path.iterdir())
    assert all(event.phase.value != "completed" for event in events)
    assert registry.responses[0].close_calls == 1


@pytest.mark.parametrize("headers,total", [
    ({"Content-Length": "4"}, 4),
    ({"Content-Length": "invalid"}, None),
    ({"Content-Length": "4", "Transfer-Encoding": "chunked"}, None),
])
def test_bounded_blob_progress_uses_comparable_header_total(
    tmp_path: Path, headers: dict, total: int | None,
) -> None:
    """Without a declared size, bounded progress retains the existing comparable-header contract."""
    raw = b"blob"
    registry = Registry(b"", {address(raw): raw})
    registry.headers_by_reference[address(raw)] = headers
    events = []
    with SafeDirectory.open(tmp_path) as directory:
        get_blob(
            registry, "repo", address(raw), "out", max_bytes=10, output_dir_fd=directory.fd,
            progress_callback=events.append,
        )
    assert events[-1].phase.value == "completed"
    assert all(event.total_bytes == total for event in events if event.phase.value != "preparing")
    assert (tmp_path / "out").read_bytes() == raw


def test_blob_observer_and_secondary_close_preserve_frozen_failure(tmp_path: Path) -> None:
    """The bounded blob adapter preserves immutable observer identity even when response close fails."""
    config = b"blob body"
    registry = Registry(document(config=edge(config)), {address(config): config})
    primary = FrozenFailure("blob observer")

    def observer(event) -> None:
        """Fail only during blob transfer, after the root has been safely stored."""
        if event.kind == "blob" and event.phase == "transferring":
            raise primary

    original = StreamResponse.close

    def fail_blob_close(response: StreamResponse) -> None:
        """Close the real stream and fail only the blob response cleanup."""
        original(response)
        if len(registry.responses) == 2:
            raise OSError("secondary close")

    with patch.object(StreamResponse, "close", fail_blob_close), pytest.raises(FrozenFailure) as caught:
        pull_artifact(registry, "repo", "moving", tmp_path / "out", progress_callback=observer)
    assert caught.value is primary
    assert not list(tmp_path.iterdir())


def test_signed_redirect_strips_authorization_and_uses_bounded_cache() -> None:
    """Requests can follow a drained signed redirect without forwarding registry authorization."""
    response = StreamResponse(b"redirect metadata", status=307, headers={"Location": "https://cdn.example/signed"})
    response.url = "https://registry.example/v2/repo/blobs/sha256:digest"
    request = requests.Request("GET", response.url, headers={"Authorization": "Bearer synthetic"}).prepare()
    response.request = request
    _bounded_blob_redirect(response)
    final = StreamResponse(b"content")
    with requests.Session() as session, patch.object(session, "send", return_value=final) as send:
        assert list(session.resolve_redirects(response, request, stream=True)) == [final]
        outgoing = send.call_args.args[0]
        assert outgoing.url == "https://cdn.example/signed"
        assert "Authorization" not in outgoing.headers
    final.close()


def test_file_wrapper_acquisition_failure_closes_descriptor(tmp_path: Path) -> None:
    """A failed fdopen does not leak an already acquired handle or replace the acquisition error."""
    descriptor = os.open(tmp_path / "file", os.O_CREAT | os.O_WRONLY, 0o600)
    primary = FrozenFailure("fdopen")
    with patch.object(os, "fdopen", side_effect=primary), pytest.raises(FrozenFailure) as caught:
        SafeFile(descriptor, "wb")
    assert caught.value is primary
    with pytest.raises(OSError):
        os.fstat(descriptor)

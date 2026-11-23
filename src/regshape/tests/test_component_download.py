"""Exercise pinned component selection, exact receipt and native ownership boundaries.

Synthetic streams/ranges inject deterministic faults without external registries
or credentials. Native success cases run on Linux/macOS; Windows fails closed.
"""

import base64
import errno
import json
import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from regshape.libs.artifacts import ArtifactLimits, pull_artifact
from regshape.libs.artifacts.graph import DOCKER_MANIFEST, OCI_INDEX, OCI_MANIFEST
from regshape.libs.artifacts.storage import SafeDirectory
from regshape.libs.blobs._download_worker import _BlobOutput
from regshape.libs.blobs._ranges import _RangedBlob
from regshape.libs.blobs.download import DownloadOptions
from regshape.libs.blobs.operations import _get_blob_with_context
from regshape.libs.components import (
    download_config, download_layer, preflight_component_download,
)
from regshape.libs.errors import ArtifactError, BlobError, ComponentError, ManifestError
from regshape.libs.progress import ManifestPhase, TransferPhase, TransferProgress
from regshape.libs.transport import RegistryClient
from regshape.libs.transport.download import DownloadTransport
from regshape.tests.artifact_fixtures import Registry, StreamResponse, address, document, edge
from regshape.tests.test_blob_batch import _client
from regshape.tests.test_blob_ranges import PAYLOAD, R, _registry as range_registry

native = pytest.mark.skipif(os.name == "nt", reason="Native component publication requires POSIX")


def _registry(config=b"opaque config", layers=(), *, subject=None, media_type=OCI_MANIFEST):
    """Build an artifact-neutral manifest whose exact bytes and request log stay observable."""
    root = document(
        config=edge(config, "application/example.config"),
        layers=[edge(raw, "application/example.layer") for raw in layers],
        subject=subject, media_type=media_type,
    )
    return Registry(root, {address(raw): raw for raw in (config, *layers)})


def _pooled_get(registry, payload):
    """Route manifests to synthetic storage and blobs through real ordered range primitives."""
    ranged, calls, responses = range_registry(payload)

    def get(client, path, **kwargs):
        """Keep one pinned manifest and expose full/ranged blob protocol behavior."""
        if "/manifests/" in path:
            return registry.get(path, **kwargs)
        return ranged(client, path, **kwargs)

    return get, calls, responses


@pytest.mark.parametrize("index", [-1, True, False, 1.0, "0", None])
def test_invalid_layer_index_precedes_all_effects(tmp_path, index):
    """SDK/preflight reject non-integer/negative selectors without silently selecting config."""
    with patch.object(SafeDirectory, "open") as opened:
        with pytest.raises(ComponentError, match="layer_index"):
            preflight_component_download(
                "registry.example", "repo", "moving", tmp_path / "out",
                component="layer", layer_index=index,
            )
        registry = _registry()
        with pytest.raises(ComponentError, match="layer_index"):
            download_layer(registry, "repo", "moving", tmp_path / "out", layer_index=index)
    opened.assert_not_called()
    assert registry.calls == [] and not list(tmp_path.iterdir())


@pytest.mark.parametrize("fields", [
    {"registry": "https://registry.example"}, {"registry": "user:secret@registry.example"},
    {"registry": "registry.example\u202e"}, {"repo": "../other"},
    {"repo": "Upper/Repo"}, {"reference": "sha256:bad"}, {"reference": "tag?secret"},
    {"component": "other"}, {"layer_index": 0},
    {"output_path": "-"}, {"output_path": ""}, {"output_path": None},
])
def test_invalid_source_selector_and_output_policy_have_no_effects(tmp_path, fields):
    """Unsafe sources and file-only policy errors stay component errors before parent/network opens."""
    arguments = {
        "registry": "registry.example", "repo": "repo", "reference": "moving",
        "output_path": tmp_path / "out", "component": "config",
    }
    arguments.update(fields)
    with patch.object(SafeDirectory, "open") as opened, pytest.raises(ComponentError):
        preflight_component_download(**arguments)
    opened.assert_not_called()


@pytest.mark.parametrize("options", [
    {"chunk_size": False}, {"chunk_size": 0}, {"chunk_size": 1.5},
    {"limits": {}}, {"download_options": {}},
    {"progress_callback": 1}, {"manifest_progress_callback": 1},
    {"phase_callback": 1}, {"download_status_callback": 1},
    {"download_options": DownloadOptions.legacy(), "download_status_callback": lambda event: None},
])
def test_options_callbacks_and_policy_are_validated_before_effects(tmp_path, options):
    """Invalid policy stays ValueError/TypeError and cannot allocate parent/pool/output state."""
    with (
        patch.object(SafeDirectory, "open") as opened,
        patch("regshape.libs.transport.download._DownloadAdapter") as adapter,
        pytest.raises((ValueError, TypeError)),
    ):
        download_config(_registry(), "repo", "moving", tmp_path / "out", **options)
    opened.assert_not_called()
    adapter.assert_not_called()
    assert not list(tmp_path.iterdir())


@native
@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "symlink"])
def test_existing_entries_are_never_replaced(tmp_path, kind):
    """Both preflight and SDK reject existing no-follow entries with the specific recovery error."""
    output = tmp_path / "out"
    if kind == "directory":
        output.mkdir()
    elif kind in ("dangling", "symlink"):
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_bytes(b"other owner")
        output.symlink_to(target)
    else:
        output.write_bytes(b"other owner")
    identity = output.lstat()
    registry = _registry()
    for action in (
        lambda: preflight_component_download(
            "registry.example", "repo", "moving", output, component="config",
        ),
        lambda: download_config(registry, "repo", "moving", output),
    ):
        with pytest.raises(ComponentError, match="already exists"):
            action()
        assert os.path.samestat(identity, output.lstat())
    assert not registry.calls and not list(tmp_path.glob(".regshape-private-*"))
    if kind == "file":
        assert output.read_bytes() == b"other owner"


@native
def test_successful_preflight_leaves_no_output_credentials_or_private_probe(tmp_path):
    """A supported absent destination is checked without reserving names or acquiring credentials."""
    with (
        patch("regshape.libs.transport.client.resolve_credentials") as credentials,
        patch("regshape.libs.transport.download._DownloadAdapter") as adapter,
    ):
        preflight_component_download(
            "registry.example", "repo", "moving", tmp_path / "out", component="config",
        )
    credentials.assert_not_called()
    adapter.assert_not_called()
    assert not list(tmp_path.iterdir())


@native
@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_output_parent_policy_fails_before_manifest_receipt(tmp_path, kind):
    """Missing/non-directory parents retain I/O causes; symlink parents retain native policy errors."""
    parent = tmp_path / "parent"
    if kind == "file":
        parent.write_bytes(b"another owner")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
    registry = _registry()
    expected = ArtifactError if kind == "symlink" else ComponentError
    for action in (
        lambda: preflight_component_download(
            "registry.example", "repo", "moving", parent / "out", component="config",
        ),
        lambda: download_config(registry, "repo", "moving", parent / "out"),
    ):
        with pytest.raises(expected) as caught:
            action()
        if kind != "symlink":
            assert isinstance(caught.value.__cause__, OSError)
    assert not registry.calls and not list(tmp_path.rglob(".regshape-private-*"))
    if kind == "file":
        assert parent.read_bytes() == b"another owner"
    elif kind == "symlink":
        assert parent.is_symlink() and not list(target.iterdir())


def test_unsupported_platform_error_is_not_normalized(tmp_path):
    """Native support failures retain the exact established ArtifactError, not ComponentError."""
    primary = ArtifactError("synthetic unsupported native filesystem")
    with patch(
        "regshape.libs.components.require_archive_publication", side_effect=primary,
    ), pytest.raises(ArtifactError) as caught:
        preflight_component_download(
            "registry.example", "repo", "moving", tmp_path / "out", component="config",
        )
    assert caught.value is primary


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows fail-closed contract")
@pytest.mark.parametrize("path_type", ["path", "string"])
def test_windows_explicitly_fails_closed(tmp_path, path_type):
    """A valid SDK call on Windows cannot create staging, credentials or registry requests."""
    registry = _registry()
    output = tmp_path / "out" if path_type == "path" else str(tmp_path / "out")
    for action in (
        lambda: preflight_component_download(
            "registry.example", "repo", "moving", output, component="config",
        ),
        lambda: download_config(registry, "repo", "moving", output),
    ):
        with pytest.raises(ArtifactError, match="unsupported") as caught:
            action()
        assert type(caught.value) is ArtifactError
    assert not registry.calls and not list(tmp_path.iterdir())


@native
@pytest.mark.parametrize("reference", ["moving", "digest"])
@pytest.mark.parametrize("media_type", [OCI_MANIFEST, DOCKER_MANIFEST])
@pytest.mark.parametrize("payload", [b"", b"\x00opaque config\xff", b"\x1f\x8bcompressed layer"])
def test_exact_config_tag_digest_nonimage_and_frozen_result(tmp_path, reference, media_type, payload):
    """One verified manifest selects only its config; immutable result has exactly eight safe fields."""
    registry = _registry(payload, [b"unselected"], media_type=media_type)
    token = address(registry.root) if reference == "digest" else reference
    output = tmp_path / "out"
    result = download_config(registry, "repo", token, output, chunk_size=1)
    assert output.read_bytes() == payload
    assert registry.calls == [
        f"/v2/repo/manifests/{token}", f"/v2/repo/blobs/{address(payload)}",
    ]
    assert asdict(result) == {
        "source": f"registry.example/repo{'@' if reference == 'digest' else ':'}{token}",
        "resolved_reference": f"registry.example/repo@{address(registry.root)}",
        "component": "config", "layer_index": None, "digest": address(payload),
        "size": len(payload), "media_type": "application/example.config",
        "output_path": str(output.absolute()),
    }
    with pytest.raises(FrozenInstanceError):
        result.size = -1
    assert all(response.close_calls == 1 for response in registry.responses)
    assert set(tmp_path.iterdir()) == {output}


@native
@pytest.mark.parametrize("layers,index", [([b"only"], 0), ([b"first", b"middle", b"last"], 0),
                                        ([b"first", b"middle", b"last"], 2)])
def test_layer_order_excludes_subjects_and_moving_tag_is_not_reread(tmp_path, layers, index):
    """Layer zero/last select the received descriptor, never a subject or subsequently moved tag."""
    registry = _registry(layers=layers, subject=edge(document(children=[]), OCI_INDEX))
    pinned = registry.root

    def moved(path):
        """Move the tag after its first response bytes are captured."""
        if "/manifests/" in path:
            registry.root = document(config=edge(b"new config"), layers=[edge(b"new layer")])

    registry.after_get = moved
    result = download_layer(registry, "repo", "moving", tmp_path / "out", layer_index=index)
    assert (tmp_path / "out").read_bytes() == layers[index]
    assert result.resolved_reference.endswith("@" + address(pinned))
    assert result.layer_index == index and result.component == "layer"
    assert registry.calls == ["/v2/repo/manifests/moving", f"/v2/repo/blobs/{address(layers[index])}"]


@native
@pytest.mark.parametrize("layers,index", [([], 0), ([b"only"], 1), ([b"a", b"b"], 2)])
def test_empty_and_out_of_range_layers_do_not_request_blobs(tmp_path, layers, index):
    """Manifest-known selection errors clean its owned file before any blob request."""
    registry = _registry(layers=layers)
    with pytest.raises(ComponentError, match="out of range"):
        download_layer(registry, "repo", "moving", tmp_path / "out", layer_index=index)
    assert registry.calls == ["/v2/repo/manifests/moving"]
    assert not list(tmp_path.iterdir())


@native
def test_indexes_require_explicit_child_reference(tmp_path):
    """An index is rejected even when it contains one apparently suitable child."""
    registry = Registry(document(children=[edge(document(config=edge(b"x")), OCI_MANIFEST)]))
    with pytest.raises(ComponentError, match="explicit child"):
        download_config(registry, "repo", "moving", tmp_path / "out")
    assert registry.calls == ["/v2/repo/manifests/moving"] and not list(tmp_path.iterdir())


@native
def test_header_only_index_is_still_rejected_explicitly(tmp_path):
    """Absent JSON mediaType does not turn a header-declared index into a missing-config error."""
    root = b'{"schemaVersion":2,"manifests":[]}'
    registry = Registry(root)
    registry.root_headers = {"Content-Type": OCI_INDEX}
    with pytest.raises(ComponentError, match="explicit child"):
        download_config(registry, "repo", "moving", tmp_path / "out")
    assert not list(tmp_path.iterdir()) and len(registry.calls) == 1


@native
def test_missing_config_has_a_component_selection_error(tmp_path):
    """A declared simple manifest without config fails before any component blob receipt."""
    registry = Registry(json.dumps({
        "schemaVersion": 2, "mediaType": OCI_MANIFEST, "layers": [],
    }).encode())
    with pytest.raises(ComponentError, match="no config descriptor"):
        download_config(registry, "repo", "moving", tmp_path / "out")
    assert registry.calls == ["/v2/repo/manifests/moving"] and not list(tmp_path.iterdir())


@native
def test_sha512_manifest_and_selected_descriptor_are_verified_without_normalization(tmp_path):
    """Reported SHA-512 pins the received manifest, and the selected blob keeps its SHA-512 address."""
    payload = b"sha512 config"
    digest = address(payload, "sha512")
    registry = Registry(document(config=edge(payload, digest=digest)), {digest: payload})
    registry.root_headers = {"Docker-Content-Digest": address(registry.root, "sha512")}
    result = download_config(registry, "repo", "moving", tmp_path / "out")
    assert result.digest == digest
    assert result.resolved_reference.endswith("@" + address(registry.root, "sha512"))
    assert (tmp_path / "out").read_bytes() == payload
    assert registry.calls[-1] == "/v2/repo/blobs/" + digest


@native
@pytest.mark.parametrize("changes", [
    {"digest": "md5:bad"}, {"size": True}, {"size": -1}, {"size": "1"},
    {"size": None}, {"mediaType": ""}, {"urls": [1]},
])
def test_strict_descriptor_errors_keep_artifact_taxonomy(tmp_path, changes):
    """Malformed descriptors fail strict parsing without coercion, blob requests or publication."""
    registry = Registry(document(config=edge(b"x", **changes)))
    with pytest.raises(ArtifactError) as caught:
        download_config(registry, "repo", "moving", tmp_path / "out")
    assert type(caught.value) is ArtifactError
    assert registry.calls == ["/v2/repo/manifests/moving"] and not list(tmp_path.iterdir())


@native
@pytest.mark.parametrize("actual", [None, b"corrupt", b"shor", b"longer bytes"])
@pytest.mark.parametrize("pooled", [False, True])
def test_missing_corrupt_short_long_content_never_publishes(tmp_path, actual, pooled):
    """Both receiver backends enforce descriptor integrity and clean only their owned partial bytes."""
    payload = b"expected"
    registry = _registry(payload)
    registry.objects.pop(address(payload))
    if actual is not None:
        registry.objects[address(payload)] = actual

    def get(client, path, **kwargs):
        """Serve the same faulty bytes on borrowed serial or forked pooled clients."""
        return registry.get(path, **kwargs)

    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(BlobError):
        download_config(
            _client() if pooled else registry, "repo", "moving", tmp_path / "out",
            download_options=DownloadOptions.sequential() if pooled else DownloadOptions.legacy(),
        )
    assert not list(tmp_path.iterdir())
    assert all(response.close_calls == 1 for response in registry.responses)


@native
@pytest.mark.parametrize("delta", [0, -1])
@pytest.mark.parametrize("payload", [b"", b"component bytes"])
def test_manifest_plus_selected_blob_budget_equality_and_overflow(tmp_path, delta, payload):
    """The exact unique total passes; one byte less fails before blob reservation/receipt."""
    registry = _registry(payload)
    total = len(registry.root) + len(payload)
    limits = ArtifactLimits(max_total_bytes=total + delta)
    if delta:
        with pytest.raises(ComponentError if payload else ManifestError):
            download_config(registry, "repo", "moving", tmp_path / "out", limits=limits)
        assert registry.calls == ["/v2/repo/manifests/moving"]
        assert not list(tmp_path.iterdir())
    else:
        result = download_config(registry, "repo", "moving", tmp_path / "out", limits=limits)
        assert result.size == len(payload) and (tmp_path / "out").read_bytes() == payload


@native
def test_zero_descriptor_size_refuses_even_one_byte(tmp_path):
    """A positive receiver cap for verified empty content cannot permit a byte beyond strict size zero."""
    registry = _registry(b"")
    registry.objects[address(b"")] = b"x"
    with pytest.raises(BlobError):
        download_config(registry, "repo", "moving", tmp_path / "out")
    assert not list(tmp_path.iterdir())


@native
@pytest.mark.parametrize("limit", ["manifest", "json", "edges"])
def test_manifest_and_structure_caps_preserve_receiver_parser_errors(tmp_path, limit):
    """Manifest caps retain ManifestError; structural caps retain ArtifactError before selected receipt."""
    registry = _registry(layers=[b"x", b"y"])
    limits = {
        "manifest": ArtifactLimits(max_manifest_bytes=len(registry.root) - 1),
        "json": ArtifactLimits(max_json_depth=1), "edges": ArtifactLimits(max_edges=2),
    }[limit]
    with pytest.raises(ManifestError if limit == "manifest" else ArtifactError):
        download_config(registry, "repo", "moving", tmp_path / "out", limits=limits)
    assert registry.calls == ["/v2/repo/manifests/moving"] and not list(tmp_path.iterdir())


@native
def test_embedded_missing_component_has_no_fallback_but_artifact_pull_keeps_it(tmp_path):
    """Registry-only component receipt intentionally differs from established complete-artifact fallback."""
    payload = b"embedded config"
    root = document(config=edge(
        payload, data=base64.b64encode(payload).decode("ascii"), urls=["https://unused.invalid/data"],
    ))
    registry = Registry(root)
    with pytest.raises(BlobError):
        download_config(registry, "repo", "moving", tmp_path / "out")
    assert registry.calls == ["/v2/repo/manifests/moving", f"/v2/repo/blobs/{address(payload)}"]
    assert not list(tmp_path.iterdir())
    result = pull_artifact(registry, "repo", "moving", tmp_path / "layout")
    assert result.embedded_objects == 1
    assert (tmp_path / "layout" / "blobs" / "sha256" / address(payload).split(":")[1]).read_bytes() == payload


@native
@pytest.mark.parametrize("ranged", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_pooled_progress_scratch_and_terminal_order(tmp_path, ranged, override):
    """Frozen caller-thread events correlate; terminal success follows publication and all cleanup."""
    payload = PAYLOAD if ranged else b"small config"
    registry = _registry(payload)
    get, calls, responses = _pooled_get(registry, payload)
    output, scratch = tmp_path / "out", tmp_path / "scratch"
    scratch.mkdir()
    progress, manifests, statuses, phases, threads = [], [], [], [], []

    def observe(event):
        """Require no public output before COMMITTING and complete output before COMPLETED."""
        threads.append(threading.get_ident())
        progress.append(event)
        if event.phase == TransferPhase.COMMITTING:
            assert not output.exists()
        if event.phase == TransferPhase.COMPLETED:
            assert output.read_bytes() == payload
            assert not list(tmp_path.glob(".regshape-private-*"))
            assert not list(scratch.iterdir())

    def status(event):
        """Require deferred completed/summary after cleanup, retaining immutable strategy metadata."""
        threads.append(threading.get_ident())
        statuses.append(event)
        if event.event in ("completed", "summary"):
            assert output.read_bytes() == payload and not list(tmp_path.glob(".regshape-private-*"))

    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        result = download_config(
            _client(), "repo", "moving", output,
            download_options=DownloadOptions(
                concurrency=3, ranges_per_blob=3 if ranged else 1, range_size=R,
                scratch_dir=scratch if override else None,
            ),
            progress_callback=observe, manifest_progress_callback=manifests.append,
            phase_callback=phases.append, download_status_callback=status,
        )
    assert result.size == len(payload) and progress[-1].phase == TransferPhase.COMPLETED
    assert manifests[-1].phase == ManifestPhase.RECEIVED and len(phases) == 1
    assert set(threads) == {threading.get_ident()}
    assert len({event.transfer_id for event in progress}) == 1
    assert {event.transfer_id for event in statuses if event.digest} == {progress[0].transfer_id}
    assert statuses[-1].event == "summary"
    assert bool(any("Range" in call for call in calls)) is ranged
    assert all(response.close_calls == 1 for response in responses + registry.responses)
    if ranged:
        completed = next(event for event in statuses if event.event == "completed")
        assert completed.stats.bytes_accepted == len(payload)
        assert completed.stats.scratch_bytes_peak <= 3 * R
        assert (completed.scratch_root == str(scratch)) is override
    with pytest.raises(FrozenInstanceError):
        progress[-1].bytes_completed = 0


@native
@pytest.mark.parametrize("phase", ["manifest", "preparing", "transferring", "verifying", "committing", "publishing"])
@pytest.mark.parametrize("error_type", [OSError, ArtifactError, ComponentError, KeyboardInterrupt])
@pytest.mark.parametrize("pooled", [False, True])
def test_early_callback_and_cancellation_preserve_exact_identity(tmp_path, phase, error_type, pooled):
    """Observers throwing domain/I/O/control errors are never normalized or recursively called."""
    primary = error_type("synthetic component observer failure")
    registry = _registry()
    get, _, _ = _pooled_get(registry, b"opaque config")
    failures = []

    def observe(event):
        """Raise once at the chosen callback boundary without mutating caller exception objects."""
        selected = (
            phase == "manifest" and getattr(event, "phase", None) == ManifestPhase.RECEIVED
            or phase == "publishing" and event == "publishing"
            or isinstance(event, TransferProgress) and event.phase.value == phase
        )
        if selected:
            failures.append(event)
            raise primary

    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(error_type) as caught:
        download_config(
            _client() if pooled else registry, "repo", "moving", tmp_path / "out",
            download_options=DownloadOptions.sequential() if pooled else DownloadOptions.legacy(),
            progress_callback=observe, manifest_progress_callback=observe, phase_callback=observe,
        )
    assert caught.value is primary and len(failures) == 1
    assert not list(tmp_path.iterdir())


@native
@pytest.mark.parametrize("terminal", ["progress", "completed", "summary"])
def test_late_terminal_callback_errors_retain_complete_public_output(tmp_path, terminal):
    """Late observers can fail the call but never roll back the irreversible public file."""
    payload = b"verified config"
    registry = _registry(payload)
    get, _, _ = _pooled_get(registry, payload)
    primary = OSError("late observer failure")

    def progress(event):
        """Fail only on operation-level completion, not receipt COMMITTING."""
        if terminal == "progress" and event.phase == TransferPhase.COMPLETED:
            raise primary

    def status(event):
        """Fail only on the selected deferred terminal status."""
        if event.event == terminal:
            raise primary

    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(OSError) as caught:
        download_config(
            _client(), "repo", "moving", tmp_path / "out",
            progress_callback=progress, download_status_callback=status,
        )
    assert caught.value is primary
    assert (tmp_path / "out").read_bytes() == payload and set(tmp_path.iterdir()) == {tmp_path / "out"}


@native
def test_publication_collision_preserves_other_owner_and_chains_fileexists(tmp_path):
    """A competitor arriving after preflight keeps both identity/content and a specific recovery error."""
    output = tmp_path / "out"
    identity = []

    def collide(phase):
        """Acquire the public filename just before the operation's no-replace publication."""
        output.write_bytes(b"competitor")
        identity.append(output.stat())

    with pytest.raises(ComponentError, match="already exists") as caught:
        download_config(_registry(), "repo", "moving", output, phase_callback=collide)
    assert isinstance(caught.value.__cause__, FileExistsError)
    assert output.read_bytes() == b"competitor" and os.path.samestat(identity[0], output.stat())
    assert set(tmp_path.iterdir()) == {output}


@native
@pytest.mark.parametrize("after", [False, True])
def test_publication_syscall_is_irreversible_even_if_python_never_observes_return(tmp_path, after):
    """Cancellation before linking cleans staging; after the actual syscall it retains complete output."""
    output, original = tmp_path / "out", os.link
    primary = KeyboardInterrupt("publication boundary")

    def interrupted(*args, **kwargs):
        """Inject cancellation on either side of the actual no-replace link."""
        if after:
            original(*args, **kwargs)
        raise primary

    with patch.object(os, "link", side_effect=interrupted) as link:
        with (
            patch.object(os, "supports_dir_fd", os.supports_dir_fd | {link}),
            patch.object(os, "supports_follow_symlinks", os.supports_follow_symlinks | {link}),
            pytest.raises(KeyboardInterrupt) as caught,
        ):
            download_config(_registry(), "repo", "moving", output)
    assert caught.value is primary
    assert output.exists() is after
    assert set(tmp_path.iterdir()) == ({output} if after else set())
    if after:
        assert output.read_bytes() == b"opaque config"


@native
@pytest.mark.parametrize("ranged", [False, True])
@pytest.mark.parametrize("failure", ["before", "after", "cleanup", "finalize", "completed", "transport"])
def test_pooled_staged_handoff_has_one_authoritative_cleanup_owner(tmp_path, ranged, failure):
    """Every late receiver fault preserves primary identity, drains scratch and deletes owned files once."""
    payload = PAYLOAD if ranged else b"small"
    get, _, responses = range_registry(payload)
    primary = RuntimeError("synthetic pooled handoff fault")
    register, cleanup, finalize, close = (
        SafeDirectory.register, _BlobOutput.cleanup, _RangedBlob.finalize, DownloadTransport.close,
    )
    deleted, registrations = [], []
    unlink = SafeDirectory.unlink

    def registered(directory, name, **kwargs):
        """Fail immediately before/after ledger insertion; never use a second ownership flag."""
        if name == "blob" and failure == "before":
            raise primary
        register(directory, name, **kwargs)
        if name == "blob":
            registrations.append((threading.get_ident(), kwargs["expected_identity"]))
            if failure == "after":
                raise primary

    def cleaned(sink, accepted):
        """Inject accepted-sink cleanup failure while leaving final failure cleanup operational."""
        if accepted and sink.staging is not None and failure == "cleanup":
            raise primary
        return cleanup(sink, accepted)

    def finalized(blob, accepted):
        """Drain ranged scratch even when successful source finalization then fails."""
        finalize(blob, accepted)
        if accepted and failure == "finalize":
            raise primary

    def closed(transport):
        """Finish real transport teardown then propagate a primary failure before publication."""
        close(transport)
        if failure == "transport":
            raise primary

    def status(event):
        """Test raw staged-receiver completed status before any operation publication."""
        if event.event == "completed" and failure == "completed":
            raise primary

    def removed(directory, name):
        """Count only staging-owned blob removal, verifying no stale/double ledger deletion."""
        if name == "blob":
            deleted.append(name)
        return unlink(directory, name)

    if failure == "finalize" and not ranged:
        pytest.skip("Only ranged sources own scratch finalization")
    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(SafeDirectory, "register", registered),
        patch.object(SafeDirectory, "unlink", removed),
        patch.object(_BlobOutput, "cleanup", cleaned),
        patch.object(_RangedBlob, "finalize", finalized),
        patch.object(DownloadTransport, "close", closed),
        pytest.raises(RuntimeError) as caught,
    ):
        with SafeDirectory.open(tmp_path) as parent:
            with SafeDirectory.private_namespace(parent) as namespace, namespace.temporary() as staging:
                _get_blob_with_context(
                    _client(), "repo", address(payload), "blob",
                    total_size=len(payload), max_bytes=len(payload), output_dir_fd=staging.fd,
                    staging=(staging, tmp_path / namespace.name / staging.name),
                    transfer_id="artifact-row",
                    download_options=DownloadOptions(
                        concurrency=3, ranges_per_blob=3 if ranged else 1, range_size=R,
                    ),
                    download_status_callback=status,
                )
    assert caught.value is primary and not list(tmp_path.iterdir())
    assert deleted == ([] if failure == "before" else ["blob"])
    assert all(thread == threading.get_ident() for thread, _ in registrations)
    assert all(response.close_calls == 1 for response in responses)


@native
@pytest.mark.parametrize("ranged", [False, True])
def test_shared_dispatcher_forwards_transfer_identity(tmp_path, ranged):
    """Full/ranged staged receipt correlates progress/status using the supplied artifact row."""
    payload = PAYLOAD if ranged else b"blob"
    get, _, _ = range_registry(payload)
    progress, statuses = [], []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        with SafeDirectory.open(tmp_path) as parent:
            with SafeDirectory.private_namespace(parent) as namespace, namespace.temporary() as staging:
                info = _get_blob_with_context(
                    _client(), "repo", address(payload), "blob",
                    total_size=len(payload), max_bytes=len(payload), output_dir_fd=staging.fd,
                    staging=(staging, tmp_path / namespace.name / staging.name),
                    transfer_id="artifact-row",
                    download_options=DownloadOptions(
                        concurrency=3, ranges_per_blob=3 if ranged else 1, range_size=R,
                    ),
                    progress_callback=progress.append, download_status_callback=statuses.append,
                )
                assert info.size == len(payload) and "blob" in staging.files
    assert {event.transfer_id for event in progress} == {"artifact-row"}
    assert {event.transfer_id for event in statuses if event.digest} == {"artifact-row"}
    assert not list(tmp_path.iterdir())


def test_public_get_blob_leaves_private_dispatch_identity_and_staging_unset(tmp_path):
    """The public facade preserves its nonstaged defaults rather than fabricating artifact ownership."""
    from regshape.libs.blobs import get_blob
    from regshape.libs.models.blob import BlobInfo

    result = BlobInfo(address(b"x"), "application/octet-stream", 1)
    with patch(
        "regshape.libs.blobs.operations._get_blob_with_context", return_value=result,
    ) as dispatch:
        assert get_blob(_client(), "repo", result.digest) is result
    assert "staging" not in dispatch.call_args.kwargs
    assert "transfer_id" not in dispatch.call_args.kwargs


@native
@pytest.mark.parametrize("after", [False, True])
def test_handoff_never_adopts_or_deletes_a_replacement(tmp_path, after, caplog):
    """Identity changes before/after registration retain replacement bytes and report failed cleanup."""
    payload = b"verified"
    get, _, _ = range_registry(payload)
    original = SafeDirectory.register
    primary = RuntimeError("after insertion failure")
    directories = []

    def replaced(directory, name, **kwargs):
        """Replace with a different live inode around the authoritative ledger insertion."""
        if name != "blob":
            return original(directory, name, **kwargs)
        directories.append(directory)
        if after:
            original(directory, name, **kwargs)
        fd = os.open("replacement", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory.fd)
        try:
            os.write(fd, b"another owner")
        finally:
            os.close(fd)
        os.replace("replacement", name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        if after:
            raise primary
        return original(directory, name, **kwargs)

    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(SafeDirectory, "register", replaced),
        pytest.raises(RuntimeError if after else ArtifactError) as caught,
    ):
        with SafeDirectory.open(tmp_path) as parent:
            with SafeDirectory.private_namespace(parent) as namespace, namespace.temporary() as staging:
                _get_blob_with_context(
                    _client(), "repo", address(payload), "blob",
                    total_size=len(payload), max_bytes=len(payload), output_dir_fd=staging.fd,
                    staging=(staging, tmp_path / namespace.name / staging.name),
                )
    retained = list(tmp_path.rglob("blob"))
    assert len(retained) == 1 and retained[0].read_bytes() == b"another owner"
    assert ("blob" in directories[0].files) is after
    if after:
        assert caught.value is primary
        assert not os.path.samestat(directories[0].files["blob"], retained[0].stat())
    assert "cleanup" in caplog.text.lower()


@native
@pytest.mark.parametrize("location", ["parent", "read", "write", "response", "publish"])
def test_filesystem_error_taxonomy_and_owned_partial_cleanup(tmp_path, location):
    """Owned parent/read/link errors are chained; receiver write/response errors remain unchanged."""
    registry = _registry()
    primary = OSError(errno.ENOSPC, "synthetic disk full")
    output = tmp_path / "out"
    if location == "parent":
        target = patch.object(SafeDirectory, "open", side_effect=primary)
    elif location == "read":
        target = patch.object(SafeDirectory, "read_bytes", side_effect=primary)
    elif location == "write":
        from regshape.libs.manifests.download import _DownloadFile
        target = patch.object(_DownloadFile, "write", side_effect=primary)
    elif location == "response":
        target = patch.object(StreamResponse, "close", side_effect=primary)
    else:
        target = patch.object(SafeDirectory, "publish_file", side_effect=primary)
    expected = OSError if location in ("write", "response") else ComponentError
    with target, pytest.raises(expected) as caught:
        download_config(registry, "repo", "moving", output)
    if expected is OSError:
        assert caught.value is primary
    else:
        assert caught.value.__cause__ is primary
    assert not list(tmp_path.iterdir())


@native
def test_cleanup_failure_is_secondary_to_observer_and_retains_only_owned_staging(tmp_path, caplog):
    """Failed staging removal cannot replace a callback error or delete public/replacement entries."""
    primary = ComponentError("observer primary")
    original = SafeDirectory.unlink

    def refused(directory, name):
        """Fail only removal of the registered raw manifest after component acquisition aborts."""
        if name.startswith("manifest-"):
            raise OSError(errno.EACCES, "synthetic cleanup refusal")
        return original(directory, name)

    def observer(phase):
        """Cancel before publication after both verified files entered the staging ledger."""
        raise primary

    with patch.object(SafeDirectory, "unlink", refused), pytest.raises(ComponentError) as caught:
        download_config(_registry(), "repo", "moving", tmp_path / "out", phase_callback=observer)
    assert caught.value is primary and not (tmp_path / "out").exists()
    assert list(tmp_path.rglob("manifest-*"))
    assert "cleanup" in caplog.text.lower()


@native
def test_postpublication_cleanup_failure_retains_output_without_terminal_success(tmp_path):
    """Owned cleanup failure after the hard link fails the call but cannot reclaim the public file."""
    output = tmp_path / "out"
    progress = []
    original = SafeDirectory.unlink
    primary = OSError(errno.EACCES, "synthetic late cleanup refusal")

    def refused(directory, name):
        """Refuse only the registered manifest once publication has completed."""
        if output.exists() and name.startswith("manifest-"):
            raise primary
        return original(directory, name)

    with patch.object(SafeDirectory, "unlink", refused), pytest.raises(OSError) as caught:
        download_config(_registry(), "repo", "moving", output, progress_callback=progress.append)
    assert caught.value is primary
    assert output.read_bytes() == b"opaque config"
    assert progress[-1].phase == TransferPhase.COMMITTING
    assert all(event.phase != TransferPhase.COMPLETED for event in progress)


@native
def test_singular_artifact_dispatch_retains_existing_row_identity(tmp_path):
    """Artifact C=1 uses the same staged dispatcher and correlates progress/status to its row."""
    payload = b"artifact config"
    registry = _registry(payload)
    get, _, _ = _pooled_get(registry, payload)
    rows, statuses = [], []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        pull_artifact(
            _client(), "repo", "moving", tmp_path / "layout",
            download_options=DownloadOptions.sequential(),
            progress_callback=rows.append, download_status_callback=statuses.append,
        )
    identities = {row.transfer_id for row in rows if row.digest == address(payload)}
    assert len(identities) == 1
    assert {event.transfer_id for event in statuses if event.digest == address(payload)} == identities


@native
def test_fresh_process_download_forbids_click_and_cli_imports(tmp_path):
    """Direct SDK operation works in a fresh process with all framework imports denied."""
    script = '''
import sys
from importlib.abc import MetaPathFinder
class Forbidden(MetaPathFinder):
    """Reject framework imports rather than relying on an already populated module cache."""
    def find_spec(self, fullname, path=None, target=None):
        """Fail on any Click or CLI import made by the direct SDK operation."""
        if fullname == "click" or fullname.startswith(("click.", "regshape.cli")):
            raise AssertionError("framework import: " + fullname)
sys.meta_path.insert(0, Forbidden())
from regshape.libs.components import download_config
from regshape.tests.artifact_fixtures import Registry, document, edge, address
payload = b"framework-independent config"
registry = Registry(document(config=edge(payload)), {address(payload): payload})
result = download_config(registry, "repo", "moving", sys.argv[1])
assert result.size == len(payload)
assert not any(name == "click" or name.startswith(("click.", "regshape.cli")) for name in sys.modules)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "out")],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2])},
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "out").read_bytes() == b"framework-independent config"

"""Verify planned artifact acquisition, physical scheduling and publication boundaries.

Loopback HTTP proves shared object/range caps. Synthetic registries inject graph,
observer and stored-content faults without external credentials or services.
"""

import base64
import json
import os
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from regshape.cli.main import regshape
from regshape.libs.artifacts import (
    ArtifactError,
    ArtifactLimits,
    pull_artifact,
    snapshot_layout,
)
from regshape.libs.artifacts.graph import OCI_INDEX, OCI_MANIFEST
from regshape.libs.artifacts.storage import ContentStore, SafeDirectory
from regshape.libs.blobs import DownloadOptions
from regshape.libs.transport import RegistryClient
from regshape.tests.artifact_fixtures import (
    Registry,
    StreamResponse,
    address,
    document,
    edge,
    nested_registry,
)
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - minimum Click
from regshape.tests.test_blob_batch import _client
from regshape.tests.test_blob_ranges import _registry as range_registry
from regshape.tests.test_manifest_cli_progress import (
    reset_telemetry as reset_telemetry,  # noqa: PLC0414 - isolate command telemetry
)

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Artifact native publication requires POSIX",
)
R = 65_536


@pytest.mark.parametrize("ranges", [False, True])
@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
def test_physical_objects_and_ranges_share_one_cap(tmp_path, ranges, output_format):
    """Three blocked bodies overlap, while ranges and full objects share C and per-blob W."""
    payloads = [b"config", b"large" * R, b"other layer"]
    root = document(
        config=edge(payloads[0]), layers=[edge(raw) for raw in payloads[1:]]
    )
    objects = {address(raw): raw for raw in [root, *payloads]}
    lock, barrier = threading.Lock(), threading.Barrier(3)
    active, peaks = Counter(), Counter()
    calls, errors = [], []
    peak = started = 0

    class Handler(BaseHTTPRequestHandler):
        """Serve the pinned manifest and exact single ranges over real local sockets."""

        def log_message(self, *args):
            """Keep loopback access logs out of test evidence."""

        def do_GET(self):
            """Frame headers before coordinating the first three independent body lifetimes."""
            nonlocal peak, started
            reference = self.path.rsplit("/", 1)[-1]
            body = root if reference == "moving" else objects[reference]
            interval = self.headers.get("Range")
            headers = {}
            if interval:
                start, end = map(int, interval.removeprefix("bytes=").split("-"))
                end = min(end, len(body) - 1)
                headers = {
                    "Content-Range": f"bytes {start}-{end}/{len(body)}",
                    "ETag": '"stable"',
                }
                body = body[start : end + 1]
            blob = "/blobs/" in self.path
            with lock:
                calls.append((reference, interval))
                if blob:
                    started += 1
                    number = started
                    active[reference] += 1
                    peak = max(peak, sum(active.values()))
                    peaks[reference] = max(peaks[reference], active[reference])
            self.send_response(206 if interval else 200)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                if blob and number <= 3:
                    barrier.wait(timeout=10)
                self.wfile.write(body)
                self.wfile.flush()
            except (OSError, threading.BrokenBarrierError) as error:
                errors.append(error)
            finally:
                if blob:
                    with lock:
                        active[reference] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    caller, callback_threads = threading.get_ident(), []
    statuses, events, bookkeeping = [], [], []
    register, promote = SafeDirectory.register, ContentStore.promote

    def observe(event):
        """Record all public progress on the invoking thread, including interleaved blobs."""
        callback_threads.append(threading.get_ident())
        events.append(event)

    def status(event):
        """Record immutable aggregate metrics and prove summary follows publication."""
        callback_threads.append(threading.get_ident())
        statuses.append(event)
        if event.event == "summary":
            assert (tmp_path / "out").exists()

    def registered(directory, filename):
        """Track caller-only shared directory mutation without changing its safety checks."""
        bookkeeping.append(threading.get_ident())
        return register(directory, filename)

    def promoted(store, filename, digest):
        """Track caller-only store promotion while preserving native no-replace operations."""
        bookkeeping.append(threading.get_ident())
        return promote(store, filename, digest)

    try:
        with (
            patch.object(SafeDirectory, "register", registered),
            patch.object(ContentStore, "promote", promoted),
        ):
            result = pull_artifact(
                _client(f"127.0.0.1:{server.server_port}"),
                "repo",
                "moving",
                tmp_path / "out",
                output_format=output_format,
                download_options=DownloadOptions(
                    concurrency=3,
                    ranges_per_blob=2 if ranges else 1,
                    range_size=R,
                ),
                progress_callback=observe,
                download_status_callback=status,
            )
    finally:
        barrier.abort()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not errors and not thread.is_alive()
    assert peak == statuses[-1].stats.active_requests_peak == 3
    assert max(peaks.values()) <= (2 if ranges else 1)
    assert statuses[-1].stats.scratch_bytes_peak <= (3 * R if ranges else 0)
    assert statuses[-1].stats.request_count == len(calls)
    assert (
        statuses[-1].stats.bytes_received
        == statuses[-1].stats.bytes_accepted
        == sum(map(len, payloads))
    )
    assert set(callback_threads + bookkeeping) == {caller}
    assert bool(any(interval for _, interval in calls)) is ranges
    assert calls.count(("moving", None)) == 1
    assert result.bytes_stored == sum(map(len, objects.values()))
    with snapshot_layout(tmp_path / "out") as snapshot:
        assert {obj.digest for obj in snapshot.graph.objects} == objects.keys()
        for obj in snapshot.graph.objects:
            assert (
                snapshot.store.verify(
                    obj.digest,
                    obj.size,
                    cap=max(map(len, objects.values())),
                )
                == objects[obj.digest]
            )
    assert set(tmp_path.iterdir()) == {tmp_path / "out"}
    for digest in map(address, payloads):
        rows = [event for event in events if event.digest == digest]
        assert rows[-1].phase == "stored"
        assert len({event.transfer_id for event in rows}) == 1
        assert {event.transfer_id for event in statuses if event.digest == digest} == {
            rows[0].transfer_id
        }


@pytest.mark.parametrize(
    "field,boundary",
    [
        ("max_objects", 4),
        ("max_edges", 5),
        ("max_depth", 2),
        ("max_total_bytes", None),
        ("max_manifest_bytes", None),
    ],
)
def test_complete_plan_limits_are_reserved_before_any_blob(tmp_path, field, boundary):
    """Exact graph budgets pass; one less fails before any blob, even one visited earlier."""
    payloads = [b"config", b"layer"]
    child = document(
        config=edge(payloads[0]), layers=[edge(payloads[1]), edge(payloads[0])]
    )
    root = document(children=[edge(child, OCI_MANIFEST)])
    objects = {address(raw): raw for raw in [root, child, *payloads]}
    if boundary is None:
        boundary = (
            sum(map(len, objects.values()))
            if field == "max_total_bytes"
            else max(map(len, objects.values()))
        )
    registry = Registry(root, objects)
    with patch.object(RegistryClient, "get", side_effect=registry.get):
        pull_artifact(
            _client(),
            "repo",
            "moving",
            tmp_path / "exact",
            limits=ArtifactLimits(**{field: boundary}),
        )
    registry.calls.clear()
    with (
        patch.object(RegistryClient, "get", side_effect=registry.get),
        pytest.raises(ArtifactError),
    ):
        pull_artifact(
            _client(),
            "repo",
            "moving",
            tmp_path / "bad",
            limits=ArtifactLimits(**{field: boundary - 1}),
        )
    assert not any("/blobs/" in path for path in registry.calls)
    assert set(tmp_path.iterdir()) == {tmp_path / "exact"}


@pytest.mark.parametrize("problem", ["size", "type", "inline", "role", "depth"])
def test_late_graph_contradictions_precede_blob_scheduling(tmp_path, problem):
    """Repeated/inline/document-role conflicts and reused subtree heights share verifier rules."""
    config = b"shared"
    first = document(config=edge(config))
    repeated = edge(config)
    if problem == "size":
        repeated["size"] += 1
    elif problem == "type":
        repeated["mediaType"] = "other/type"
    elif problem == "inline":
        repeated["data"] = "%%%%"
    if problem == "role":
        second = document(children=[edge(first, OCI_MANIFEST)])
        first = document(config=edge(second, OCI_INDEX))
        root = document(children=[edge(first, OCI_MANIFEST), edge(second, OCI_INDEX)])
    elif problem == "depth":
        first = document(children=[edge(document(children=[]), OCI_INDEX)])
        second = document(children=[edge(first, OCI_INDEX)])
        root = document(children=[edge(first, OCI_INDEX), edge(second, OCI_INDEX)])
    else:
        second = document(config=repeated)
        root = document(
            children=[edge(first, OCI_MANIFEST), edge(second, OCI_MANIFEST)]
        )
    registry = Registry(
        root,
        {address(raw): raw for raw in (config, first, second, document(children=[]))},
    )
    with (
        patch.object(RegistryClient, "get", side_effect=registry.get),
        pytest.raises(ArtifactError),
    ):
        pull_artifact(
            _client(),
            "repo",
            "moving",
            tmp_path / "out",
            limits=ArtifactLimits(max_depth=2) if problem == "depth" else None,
        )
    assert not any("/blobs/" in path for path in registry.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("output_format", ["oci", "oci-tar-gzip"])
def test_plan_retains_provenance_roles_reuse_and_pinned_bytes(tmp_path, output_format):
    """Final local validation cannot replace network/embedded provenance or first-role identity."""
    embedded, shared = b"embedded", b"shared"
    first = document(
        config=edge(embedded, data=base64.b64encode(embedded).decode()),
        layers=[edge(shared)],
    )
    second = document(config=edge(shared), layers=[edge(embedded), edge(shared)])
    root = document(
        children=[
            edge(first, OCI_MANIFEST),
            edge(second, OCI_MANIFEST),
            edge(first, OCI_MANIFEST),
        ]
    )
    objects = {address(raw): raw for raw in (root, first, second, embedded, shared)}
    registry = Registry(root, objects)
    registry.after_get = lambda path: setattr(registry, "root", document(children=[]))
    events = []
    with patch.object(RegistryClient, "get", side_effect=registry.get):
        result = pull_artifact(
            _client(),
            "repo",
            "moving",
            tmp_path / "out",
            output_format=output_format,
            progress_callback=events.append,
        )
    assert result.root.digest == address(root) and result.reused_references == 4
    assert result.embedded_objects == 1 and result.downloaded_objects == 4
    assert result.bytes_downloaded == sum(map(len, (root, first, second, shared)))
    assert result.bytes_stored == sum(map(len, objects.values()))
    assert {event.role for event in events if event.digest == address(shared)} == {
        "layer"
    }
    assert {event.source for event in events if event.digest == address(embedded)} == {
        "embedded"
    }
    assert len(registry.calls) == len(set(registry.calls)) == 4
    assert all("/manifests/" in path for path in registry.calls[:3])
    with snapshot_layout(tmp_path / "out") as snapshot:
        assert {obj.digest for obj in snapshot.graph.objects} == objects.keys()
        for obj in snapshot.graph.objects:
            assert (
                snapshot.store.verify(obj.digest, obj.size, cap=4096)
                == objects[obj.digest]
            )


@pytest.mark.parametrize(
    "surface,phase",
    [
        ("progress", "transferring"),
        ("progress", "stored"),
        ("status", "strategy"),
        ("status", "completed"),
    ],
)
def test_observer_failure_drains_without_partial_publication(tmp_path, surface, phase):
    """Exact callback exceptions survive cleanup; failed observers are never reentered."""
    registry, _, _ = nested_registry()
    primary = OSError("synthetic observer fault")
    failed = False

    def observe(event):
        """Fail once at the requested blob lifecycle boundary, detecting recursive callbacks."""
        nonlocal failed
        assert not failed
        if getattr(event, "phase" if surface == "progress" else "event") == phase and (
            getattr(event, "kind", "blob") == "blob"
        ):
            failed = True
            raise primary

    with (
        patch.object(RegistryClient, "get", side_effect=registry.get),
        pytest.raises(OSError) as caught,
    ):
        pull_artifact(
            _client(),
            "repo",
            "moving",
            tmp_path / "out",
            **{
                f"{surface}_callback"
                if surface == "progress"
                else "download_status_callback": observe
            },
        )
    assert failed and caught.value is primary
    assert all(response.close_calls == 1 for response in registry.responses)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("concurrency", [1, 4])
@pytest.mark.parametrize("extra_blob", [False, True])
def test_final_verifier_rejects_post_receive_corruption(
    tmp_path, concurrency, extra_blob
):
    """Corruption fails the blob row without stored success or publication in either mode."""
    payloads = [b"original", b"other"] if extra_blob else [b"original"]
    root = document(
        config=edge(payloads[0]), layers=[edge(raw) for raw in payloads[1:]]
    )
    objects = {address(raw): raw for raw in payloads}
    registry = Registry(root, objects)
    original = ContentStore.promote
    events, promoted = [], []

    def promote(store, filename, digest):
        """Corrupt only a promoted blob in this controlled fault-injection namespace."""
        original(store, filename, digest)
        if digest not in objects:
            return
        promoted.append(digest)
        if len(promoted) == 1:
            algorithm, encoded = digest.split(":")
            fd = os.open(encoded, os.O_WRONLY, dir_fd=store.algorithms[algorithm].fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"modified")

    with (
        patch.object(RegistryClient, "get", side_effect=registry.get),
        patch.object(ContentStore, "promote", promote),
        pytest.raises(ArtifactError, match="digest or size mismatch"),
    ):
        pull_artifact(
            _client(),
            "repo",
            "moving",
            tmp_path / "out",
            download_options=DownloadOptions(concurrency=concurrency),
            progress_callback=events.append,
        )
    assert len(promoted) == (len(payloads) if concurrency > 1 else 1)
    phases = [event.phase for event in events if event.digest == promoted[0]]
    assert phases[-1] == "failed"
    assert "verifying" in phases and "stored" not in phases
    assert not any(
        event.phase in ("stored", "failed") and event.digest in promoted[1:]
        for event in events
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("affected,fail", [(2, False), (1, True)])
@pytest.mark.parametrize("suppressed", ["--json", "--no-progress"])
def test_actual_etag_fallback_warns_once_after_artifact_result_or_error(
    tmp_path,
    runner,
    affected,
    fail,
    suppressed,
):
    """Actual multi-blob range resets survive dedup and warn after stdout/error and cleanup."""
    payloads = [bytes([index]) * (5 * R) for index in range(affected)]
    root = document(
        config=edge(payloads[0]),
        layers=[
            *(edge(raw) for raw in payloads[1:]),
            edge(payloads[0]),
        ],
    )
    registry = Registry(root)

    def modify(start, end, headers, response):
        """Rotate one later validator and optionally fail its full-stream fallback."""
        if headers.get("Range") and start == R:
            response.headers["ETag"] = '"rotated"'
        if fail and "Range" not in headers:
            return StreamResponse(b"", status=404)
        return response

    receivers = {address(raw): range_registry(raw, modify=modify) for raw in payloads}

    def get(client, path, **kwargs):
        """Use real manifest receipt and the real ranged engine, substituting only HTTP."""
        if "/manifests/" in path:
            return registry.get(path, **kwargs)
        receiver, _, _ = receivers[path.rsplit("/", 1)[-1]]
        return receiver(client, path, **kwargs)

    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch(
            "regshape.libs.transport.client.resolve_credentials",
            return_value=(None, None),
        ),
    ):
        result = runner.invoke(
            regshape,
            [
                "artifact",
                "pull",
                "-a",
                "registry.example/repo:moving",
                "-o",
                str(tmp_path / "out"),
                "--concurrency",
                "3",
                "--ranges-per-blob",
                "2",
                "--range-size",
                str(R),
                suppressed,
            ],
        )
    assert result.exit_code == int(fail), result.exception
    warning = f"Warning: ETag changed for {affected} blobs;"
    assert (
        result.stderr.count("Warning:") == 1
        and warning in result.stderr.splitlines()[-1]
    )
    if fail:
        assert not result.stdout
        assert "did not complete" in result.stderr.splitlines()[-1]
        assert not list(tmp_path.iterdir())
    else:
        if suppressed == "--json":
            assert json.loads(result.stdout)["blobs"] == affected
        else:
            assert result.stdout.startswith("Downloaded complete artifact.")
        assert "verified their digests" in result.stderr.splitlines()[-1]
        assert set(tmp_path.iterdir()) == {tmp_path / "out"}
    for _, _, responses in receivers.values():
        assert all(response.close_calls == 1 for response in responses)

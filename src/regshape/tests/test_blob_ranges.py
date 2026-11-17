"""Exercise real-range negotiation, bounded ordered assembly and per-blob reset.

Synthetic streams inject protocol faults; loopback HTTP verifies physical range
overlap and the committed-prefix window without external registries or tokens.
"""

import gzip
import json
import os
import threading
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
import requests
from urllib3.exceptions import ProtocolError

from regshape.cli.main import regshape
from regshape.libs.artifacts import pull_artifact
from regshape.libs.blobs import BlobDownload, DownloadOptions, get_blob, get_blobs
from regshape.libs.errors import BlobError
from regshape.libs.progress import TransferPhase
from regshape.libs.transport import RegistryClient
from regshape.libs.transport._coordinator import _Worker
from regshape.libs.transport.middleware import RetryConfig
from regshape.tests.artifact_fixtures import Registry, StreamResponse, address, document, edge
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - minimum Click fixture
from regshape.tests.test_blob_batch import _client
from regshape.tests.test_download_workers import _fault_response

R = 65_536
PAYLOAD = (bytes(range(251)) * (6 * R // 251 + 1))[:6 * R - 19]


def _options(tmp_path, **kwargs):
    """Use minimum-sized ranges in a caller-trusted root with no real retry delays."""
    return DownloadOptions(
        concurrency=3, ranges_per_blob=3, range_size=R, scratch_dir=tmp_path, **kwargs,
    )


def _range(payload, start, end, *, etag='"stable"', **headers):
    """Construct an exact single-part response with independently overrideable metadata."""
    end = min(end, len(payload) - 1)
    return StreamResponse(
        payload[start:end + 1], status=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            "Content-Length": str(end - start + 1), "ETag": etag,
            "Content-Type": "application/octet-stream", **headers,
        },
    )


def _registry(payload=PAYLOAD, *, modify=None):
    """Return a thread-safe fake GET and its requests/responses, never implementing HEAD."""
    calls, responses = [], []
    lock = threading.Lock()

    def get(client, path, **kwargs):
        """Serve the requested interval or full entity, with optional per-request faults."""
        headers = kwargs["headers"]
        interval = headers.get("Range")
        start, end = map(int, interval.removeprefix("bytes=").split("-")) if interval else (0, len(payload) - 1)
        with lock:
            calls.append(dict(headers))
        response = (
            _range(payload, start, end) if interval
            else StreamResponse(payload, headers={"Content-Length": str(len(payload))})
        )
        if modify is not None:
            response = modify(start, end, headers, response)
        with lock:
            responses.append(response)
        return response

    return get, calls, responses


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
def test_ordered_ranges_keep_first_real_part_and_bound_scratch(tmp_path, saved, known, algorithm):
    """File and verify-only paths retain range zero, exact size/digest and immutable caller events."""
    path = tmp_path / "output"
    get, calls, responses = _registry()
    statuses, progress, threads = [], [], []
    caller = threading.get_ident()

    def observe(event):
        """Record public observations only on the invoking thread."""
        threads.append(threading.get_ident())
        progress.append(event)

    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), patch.object(
        RegistryClient, "head", side_effect=AssertionError("No HEAD is needed"),
    ):
        info = get_blob(
            _client(), "repo", address(PAYLOAD, algorithm), str(path) if saved else None,
            chunk_size=4096, total_size=len(PAYLOAD) if known else None,
            download_options=_options(tmp_path), progress_callback=observe,
            download_status_callback=statuses.append,
        )
    assert info.size == len(PAYLOAD) and info.digest == address(PAYLOAD, algorithm)
    assert len(calls) == 6 and calls[0]["Range"] == f"bytes=0-{R - 1}"
    assert all(call["Accept-Encoding"] == "identity" for call in calls)
    assert "If-Range" not in calls[0]
    assert all(call["If-Range"] == '"stable"' for call in calls[1:])
    assert all(response.close_calls == 1 for response in responses)
    assert set(threads) == {caller}
    assert progress[-1].phase == TransferPhase.COMPLETED
    counts = [event.bytes_completed for event in progress]
    assert counts == sorted(counts) and counts[-1] == len(PAYLOAD)
    completed = next(event for event in statuses if event.event == "completed")
    assert completed.strategy == "ranged"
    assert completed.stats.bytes_accepted == completed.stats.bytes_received == len(PAYLOAD)
    assert 0 < completed.stats.scratch_bytes_peak <= 3 * R
    assert completed.scratch_root == str(tmp_path)
    assert {event.transfer_id for event in progress} == {completed.transfer_id}
    assert list(tmp_path.iterdir()) == ([path] if saved else [])
    if saved:
        assert path.read_bytes() == PAYLOAD


@pytest.mark.parametrize("size", [0, 1, 4 * R - 1])
def test_known_small_or_zero_uses_one_full_get_without_scratch(tmp_path, size):
    """Known N below 4R bypasses all metadata/range requests, including zero verification."""
    payload = PAYLOAD[:size]
    get, calls, _ = _registry(payload)
    statuses = []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        info = get_blob(
            _client(), "repo", address(payload), total_size=size,
            download_options=_options(tmp_path), download_status_callback=statuses.append,
        )
    assert info.size == size and len(calls) == 1 and "Range" not in calls[0]
    assert all(event.scratch_root is None for event in statuses)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="artifact storage requires POSIX")
@pytest.mark.parametrize("override", [False, True])
def test_artifact_range_scratch_uses_private_staging_unless_overridden(tmp_path, override):
    """Actual ranged artifact receipt uses staging by default and safely removes either scratch."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    registry = Registry(document(config=edge(PAYLOAD)))
    ranged_get, calls, responses = _registry()
    locations, statuses = set(), []

    def get(client, path, **kwargs):
        """Inspect the live namespace at range dispatch, not a mocked policy or constructor."""
        if "/manifests/" in path:
            return registry.get(path, **kwargs)
        directories = list(tmp_path.rglob("regshape-ranges-*"))
        assert len(directories) == 1
        location = directories[0].parent
        if override:
            assert location == scratch
        else:
            assert location.name.startswith(".regshape-staging-")
            assert location.parent.name.startswith(".regshape-private-")
        locations.add(str(location))
        return ranged_get(client, path, **kwargs)

    output = tmp_path / "layout"
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        pull_artifact(
            _client(), "repo", "moving", output,
            download_options=DownloadOptions(
                concurrency=3, ranges_per_blob=3, range_size=R,
                scratch_dir=scratch if override else None,
            ),
            download_status_callback=statuses.append,
        )
    assert len(calls) == 6 and all(response.close_calls == 1 for response in responses)
    assert (output / "blobs" / "sha256" / address(PAYLOAD).split(":")[1]).read_bytes() == PAYLOAD
    assert {event.scratch_root for event in statuses if event.transfer_id} == locations
    assert not list(scratch.iterdir()) and not list(tmp_path.rglob("regshape-ranges-*"))
    assert set(tmp_path.iterdir()) == {scratch, output}


def test_exact_four_range_threshold_and_case_insensitive_bytes_unit(tmp_path):
    """N=4R is eligible and HTTP range-unit comparison is case insensitive."""
    payload = PAYLOAD[:4 * R]

    def modify(start, end, headers, response):
        """Vary the unit's case without changing its numeric meaning or valid body."""
        response.headers["Content-Range"] = response.headers["Content-Range"].replace("bytes", "Bytes")
        return response

    get, calls, _ = _registry(payload, modify=modify)
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        info = get_blob(
            _client(), "repo", address(payload), total_size=len(payload),
            download_options=_options(tmp_path),
        )
    assert info.size == 4 * R and len(calls) == 4
    assert all("Range" in call for call in calls)
    assert not list(tmp_path.iterdir())


def test_initial_ignored_range_consumes_same_200_response(tmp_path):
    """An ignored first range reuses its response and PREPARING phase without another GET."""
    response = StreamResponse(PAYLOAD, headers={"Content-Length": str(len(PAYLOAD))})
    statuses, progress = [], []
    with patch.object(RegistryClient, "get", return_value=response) as get:
        info = get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path),
            download_status_callback=statuses.append,
            progress_callback=progress.append,
        )
    assert get.call_count == response.close_calls == 1
    assert info.size == len(PAYLOAD)
    completed = next(event for event in statuses if event.event == "completed")
    assert completed.strategy == "full" and completed.reason == "range_ignored"
    assert completed.stats.bytes_accepted == len(PAYLOAD)
    assert sum(event.phase == TransferPhase.PREPARING for event in progress) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("retries", [1, 2])
@pytest.mark.parametrize("saved", [False, True])
def test_ignored_range_keeps_consumed_retry_budget_and_backoff(tmp_path, retries, saved):
    """Reusing a 200 after a range retry cannot grant a second transport retry budget."""
    primary = requests.exceptions.ConnectionError("synthetic pre-payload full fault")
    responses = [
        StreamResponse(b"", status=429),
        _fault_response(b"", primary),
        StreamResponse(PAYLOAD),
    ]
    path = tmp_path / "output"
    statuses, progress, waits = [], [], []

    def receive():
        """Exercise actual range-to-full response ownership on a saved or verify-only transfer."""
        return get_blob(
            _client(), "repo", address(PAYLOAD), str(path) if saved else None,
            total_size=len(PAYLOAD), download_options=_options(tmp_path, max_retries=retries),
            download_status_callback=statuses.append, progress_callback=progress.append,
        )

    with (
        patch.object(RegistryClient, "get", side_effect=responses) as get,
        patch.object(_Worker, "wait", side_effect=waits.append),
    ):
        if retries == 1:
            with pytest.raises(requests.exceptions.ConnectionError) as caught:
                receive()
            assert caught.value is primary
        else:
            assert receive().size == len(PAYLOAD)
    assert get.call_count == retries + 1
    assert all(call.kwargs["headers"].get("Range") for call in get.call_args_list[:2])
    assert waits == [2**index for index in range(retries)]
    assert statuses[-1].stats.retry_count == retries
    assert sum(event.phase == TransferPhase.PREPARING for event in progress) == 1
    assert all(response.close_calls == 1 for response in responses[:retries + 1])
    if saved:
        assert path.read_bytes() == (PAYLOAD if retries == 2 else b"")
    else:
        assert not path.exists()
    assert set(tmp_path.iterdir()) == ({path} if saved else set())


@pytest.mark.parametrize("change,reason", [
    ({"ETag": 'W/"weak"'}, "no_strong_validator"),
    ({"ETag": ""}, "no_strong_validator"),
    ({"Content-Range": f"bytes 0-{R - 1}/*"}, "unknown_size"),
    ({"Content-Encoding": "gzip"}, "nonidentity_encoding"),
    ({"Content-Range": f"bytes 1-{R - 1}/{len(PAYLOAD)}", "Content-Length": str(R - 1)}, "short_range"),
])
def test_capability_fallback_is_explicit_and_runs_with_zero_retries(tmp_path, change, reason):
    """Well-formed capability limitations select one clean full attempt, not a hidden retry."""
    response = (
        _range(PAYLOAD, 1, R - 1) if reason == "short_range"
        else _range(PAYLOAD, 0, R - 1, **change)
    )
    full = StreamResponse(PAYLOAD)
    statuses = []
    with patch.object(RegistryClient, "get", side_effect=[response, full]) as get:
        info = get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path, max_retries=0),
            download_status_callback=statuses.append,
        )
    assert info.size == len(PAYLOAD) and get.call_count == 2
    fallback = [event for event in statuses if event.event == "fallback"]
    assert len(fallback) == 1 and fallback[0].reason == reason
    assert fallback[0].attempt == 2 and fallback[0].stats.bytes_accepted == 0
    assert response.close_calls == full.close_calls == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("changes", [
    {"Content-Range": "items 0-65535/999999"},
    {"Content-Range": "bytes 9-0/999999"},
    {"Content-Range": f"bytes 0-{R}/{len(PAYLOAD)}"},
    {"Content-Range": f"bytes 0-{R - 1}/{len(PAYLOAD) + 1}"},
    {"Content-Range": f"bytes 0-{R - 1}/{2**63}"},
    {"Content-Range": f"bytes 0-{R - 1}/{'9' * 200}"},
    {"Content-Length": str(R + 1)},
    {"Content-Length": "-1"},
    {"Content-Length": "9" * 10000},
    {"Content-Type": "multipart/byteranges; boundary=x"},
])
def test_malformed_range_headers_never_become_capability_fallback(tmp_path, changes):
    """Invalid bounds/units/lengths/overflow and multipart fail before output truncation."""
    path = tmp_path / "existing"
    path.write_bytes(b"keep")
    response = _range(PAYLOAD, 0, R - 1, **changes)
    statuses = []
    with patch.object(RegistryClient, "get", return_value=response) as get, pytest.raises(BlobError):
        get_blob(
            _client(), "repo", address(PAYLOAD), str(path), total_size=len(PAYLOAD),
            download_options=_options(tmp_path), download_status_callback=statuses.append,
        )
    assert get.call_count == response.close_calls == 1
    assert not any(event.event == "fallback" for event in statuses)
    assert path.read_bytes() == b"keep" and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("extra", [-1, 1])
def test_inexact_range_body_fails_without_retry_or_fallback(tmp_path, extra):
    """A declared range must have exactly its promised bytes, before any prefix commits."""
    response = StreamResponse(
        b"x" * (R + extra), status=206,
        headers={"Content-Range": f"bytes 0-{R - 1}/{len(PAYLOAD)}", "ETag": '"stable"'},
    )
    with patch.object(RegistryClient, "get", return_value=response) as get, pytest.raises(BlobError):
        get_blob(_client(), "repo", address(PAYLOAD), download_options=_options(tmp_path))
    assert get.call_count == response.close_calls == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [206, 200])
@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
def test_late_etag_fallback_resets_prefix_sink_hash_and_attempt(tmp_path, status, saved, bounded):
    """An established ETag rotation restarts one clean attempt after all old responses/spools stop."""
    path = tmp_path / "output"
    progress, statuses = [], []
    responses = []
    full_calls = []

    def modify(start, end, headers, response):
        """Rotate after the retained first part; assert drained/truncated state at fresh full GET."""
        if "Range" not in headers:
            full_calls.append(headers)
            assert all(old.close_calls == 1 for old in responses)
            assert all(not tuple(p.iterdir()) for p in tmp_path.glob("regshape-ranges-*"))
            if saved:
                assert path.read_bytes() == b""
            assert "If-Range" not in headers and "If-Match" not in headers
        elif start >= R:
            if status == 200:
                response = StreamResponse(
                    PAYLOAD, headers={"ETag": '"rotated"', "Content-Length": str(len(PAYLOAD))},
                )
            else:
                response.headers["ETag"] = '"rotated"'
        responses.append(response)
        return response

    get, calls, _ = _registry(modify=modify)
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        info = get_blob(
            _client(), "repo", address(PAYLOAD), str(path) if saved else None,
            total_size=len(PAYLOAD), max_bytes=len(PAYLOAD) if bounded else None,
            download_options=_options(tmp_path, max_retries=0),
            progress_callback=progress.append, download_status_callback=statuses.append,
        )
    assert info.size == len(PAYLOAD) and len(full_calls) == 1
    fallback = [event for event in statuses if event.event == "fallback"]
    assert len(fallback) == 1 and fallback[0].reason == "etag_changed"
    assert fallback[0].attempt == 2 and fallback[0].stats.bytes_accepted == 0
    assert len({event.transfer_id for event in progress}) == 1
    assert any(event.attempt == 1 and event.bytes_completed == R for event in progress)
    assert next(event for event in progress if event.attempt == 2).bytes_completed == 0
    completed = next(event for event in statuses if event.event == "completed")
    assert completed.reason == "etag_changed" and completed.attempt == 2
    assert completed.stats.bytes_accepted == len(PAYLOAD)
    assert completed.stats.bytes_received >= len(PAYLOAD) + R
    assert not any(event.event in ("failed", "cancelled") for event in statuses)
    assert list(tmp_path.iterdir()) == ([path] if saved else [])
    if saved:
        assert path.read_bytes() == PAYLOAD


def test_fallback_does_not_cancel_or_replay_an_unrelated_batch_blob(tmp_path):
    """Source-local draining retains unrelated committed full-stream progress and completion."""
    other = b"independent" * 100
    full_get, calls, _ = _registry()
    other_requests = []

    def get(client, path, **kwargs):
        """Serve a second digest once while rotating only the ranged blob's validator."""
        if path.endswith(address(other)):
            other_requests.append(path)
            return StreamResponse(other)
        response = full_get(client, path, **kwargs)
        interval = kwargs["headers"].get("Range", "")
        if interval and interval != f"bytes=0-{R - 1}":
            response.headers["ETag"] = '"rotated"'
        return response

    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        result = get_blobs(
            _client(), "repo", [
                BlobDownload(address(PAYLOAD), total_size=len(PAYLOAD)),
                BlobDownload(address(other), total_size=len(other)),
            ], download_options=_options(tmp_path),
        )
    assert [info.digest for info in result.blobs] == [address(PAYLOAD), address(other)]
    assert len(other_requests) == 1
    assert sum("Range" not in call for call in calls) == 1
    assert result.stats.bytes_accepted == len(PAYLOAD) + len(other)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("reason", ["range_ignored", "short_range"])
def test_late_capability_limitation_resets_once_after_committed_prefix(tmp_path, reason):
    """A late 200 or valid strict subrange resets cleanly without mislabeling a protocol error."""
    def modify(start, end, headers, response):
        """Return a standards-permitted limited response after successful range-zero negotiation."""
        if "Range" in headers and start >= R:
            if reason == "range_ignored":
                return StreamResponse(PAYLOAD)
            return _range(PAYLOAD, start, end - 1)
        return response

    get, calls, _ = _registry(modify=modify)
    statuses = []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get):
        info = get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path, max_retries=0),
            download_status_callback=statuses.append,
        )
    assert info.size == len(PAYLOAD) and sum("Range" not in call for call in calls) == 1
    fallback = [event for event in statuses if event.event == "fallback"]
    assert len(fallback) == 1 and fallback[0].reason == reason
    assert fallback[0].attempt == 2 and fallback[0].stats.bytes_accepted == 0
    assert not list(tmp_path.iterdir())


def test_fallback_waits_for_blocked_range_before_reset_or_spool_cleanup(tmp_path):
    """Cancellation is prompt, but old scratch and sink survive until a blocked reader returns."""
    real_get, _, responses = _registry()
    blocked, release, full_started = threading.Event(), threading.Event(), threading.Event()
    cancel_tokens, faults = [], []
    from regshape.libs.blobs._ranges import _RangePart

    original_run = _RangePart.run

    def run(part, worker):
        """Expose the actual per-source cancellation token, not a proxy future-cancel flag."""
        if part.start == R and not part.full:
            cancel_tokens.append(worker.job.cancelled)
        return original_run(part, worker)

    def get(client, path, **kwargs):
        """Delay one valid old body while a later sibling announces ETag rotation."""
        interval = kwargs["headers"].get("Range")
        if interval is None:
            full_started.set()
            assert release.is_set()
            assert all(response.close_calls == 1 for response in responses)
        response = real_get(client, path, **kwargs)
        if interval == f"bytes={R}-{2 * R - 1}":
            def stream(chunk_size, decode_content=False):
                """Model an active read that cancellation cannot forcibly interrupt."""
                blocked.set()
                assert release.wait(5)
                yield PAYLOAD[R:2 * R]

            response.raw.stream = stream
        elif interval == f"bytes={2 * R}-{3 * R - 1}":
            assert blocked.wait(5)
            response.headers["ETag"] = '"rotated"'
        return response

    def release_reader():
        """Check cancellation before permitting safe join/reset, without claiming a shutdown deadline."""
        try:
            assert blocked.wait(5)
            assert cancel_tokens[0].wait(5)
            assert not full_started.is_set()
            assert list(tmp_path.glob("regshape-ranges-*/*"))
        except BaseException as error:
            faults.append(error)
        finally:
            release.set()

    monitor = threading.Thread(target=release_reader)
    monitor.start()
    try:
        with (
            patch.object(_RangePart, "run", autospec=True, side_effect=run),
            patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        ):
            info = get_blob(
                _client(), "repo", address(PAYLOAD), total_size=len(PAYLOAD),
                download_options=_options(tmp_path),
            )
    finally:
        release.set()
        monitor.join()
    assert not faults and full_started.is_set() and info.size == len(PAYLOAD)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("phase", ["transferring", "verifying"])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_range_progress_exception_cancels_and_cleans_bounded_output(tmp_path, saved, phase, error_type):
    """Observer errors during assembly/verification survive drain and file/verify-only cleanup."""
    path = tmp_path / "output"
    primary = error_type("synthetic progress failure")
    events = []

    def observe(event):
        """Fail once the first ordered payload commits; never accept another callback."""
        assert not events or events[-1] is not primary
        if event.bytes_completed and event.phase.value == phase:
            events.append(primary)
            raise primary
        events.append(event)

    get, _, responses = _registry()
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(
        error_type,
    ) as caught:
        get_blob(
            _client(), "repo", address(PAYLOAD), str(path) if saved else None, total_size=len(PAYLOAD),
            max_bytes=len(PAYLOAD), download_options=_options(tmp_path),
            progress_callback=observe,
        )
    assert caught.value is primary and events[-1] is primary
    assert all(response.close_calls == 1 for response in responses)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
def test_corrupt_content_never_completes_and_deletes_invalid_output(tmp_path, fallback, bounded):
    """Digest mismatch removes even overwritten ordinary output and never becomes a second fallback."""
    path = tmp_path / "output"
    if not bounded:
        path.write_bytes(b"old overwritten bytes")

    def modify(start, end, headers, response):
        """Inject either an incorrect ordered part or a corrupt full fallback body."""
        if fallback:
            if "Range" not in headers:
                return StreamResponse(b"x" * len(PAYLOAD))
            if start >= R:
                response.headers["ETag"] = '"changed"'
        elif start == R:
            return _range(b"x" * len(PAYLOAD), start, end)
        return response

    get, calls, _ = _registry(modify=modify)
    statuses = []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(
        BlobError, match="digest",
    ):
        get_blob(
            _client(), "repo", address(PAYLOAD), str(path), total_size=len(PAYLOAD),
            max_bytes=len(PAYLOAD) if bounded else None, download_options=_options(tmp_path),
            download_status_callback=statuses.append,
        )
    assert not path.exists() and not list(tmp_path.iterdir())
    assert not any(event.event == "completed" for event in statuses)
    assert sum(event.event == "fallback" for event in statuses) == int(fallback)
    assert sum("Range" not in call for call in calls) == int(fallback)


@pytest.mark.parametrize("status,retries", [(429, 0), (429, 3), (503, 2)])
def test_range_status_retry_budget_and_retry_after(tmp_path, status, retries):
    """The range owner honors the exact budget, Retry-After and caller-thread retry statuses."""
    real_get, calls, _ = _registry()
    attempts, waits, statuses = [], [], []

    def get(client, path, **kwargs):
        """Throttle every first-range attempt until the selected budget is consumed."""
        if kwargs["headers"].get("Range") == f"bytes=0-{R - 1}" and len(attempts) <= retries:
            attempts.append(status)
            return StreamResponse(b"", status=status, headers={"Retry-After": "4"})
        return real_get(client, path, **kwargs)

    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(_Worker, "wait", side_effect=waits.append),
        pytest.raises(BlobError) as caught,
    ):
        get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path, max_retries=retries),
            download_status_callback=statuses.append,
        )
    assert caught.value.status_code == status
    assert len(attempts) == retries + 1 and len(waits) == retries
    assert all(delay >= 4 for delay in waits)
    assert sum(event.event == "retry" for event in statuses) == retries
    assert not calls and not list(tmp_path.iterdir())


def test_explicit_caller_status_set_remains_authoritative(tmp_path):
    """A download budget override must not silently add 429 to an explicit SDK retry status set."""
    client = _client(retry_config=RetryConfig(status_codes=(503,), backoff_factor=0))
    with patch.object(RegistryClient, "get", return_value=StreamResponse(b"", status=429)) as get:
        with pytest.raises(BlobError) as caught:
            get_blob(
                client, "repo", address(PAYLOAD), download_options=_options(tmp_path, max_retries=3),
            )
    assert caught.value.status_code == 429 and get.call_count == 1


@pytest.mark.parametrize("stage", ["first", "later"])
def test_default_range_429_budget_recovers_after_three_retries(tmp_path, stage):
    """Unconfigured pooled range calls really recover 429 with three retries and one owner."""
    real_get, calls, _ = _registry()
    attempts, waits, statuses = [], [], []
    target = f"bytes={0 if stage == 'first' else R}-{R - 1 if stage == 'first' else 2 * R - 1}"

    def get(client, path, **kwargs):
        """Return three pre-body throttles, then the original requested range without nested budgets."""
        if kwargs["headers"].get("Range") == target and len(attempts) < 3:
            attempts.append(429)
            return StreamResponse(b"", status=429, headers={"Retry-After": "0"})
        return real_get(client, path, **kwargs)

    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(_Worker, "wait", side_effect=waits.append),
    ):
        info = get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path),
            download_status_callback=statuses.append,
        )
    assert info.size == len(PAYLOAD) and len(attempts) == 3
    assert waits == [1, 2, 4] and len(calls) == 6
    assert sum(event.event == "retry" for event in statuses) == 3
    assert statuses[-1].stats.retry_count == 3


def test_range_body_transport_retry_discards_prefix_spool_without_double_acceptance(tmp_path):
    """An incomplete transport attempt can replay its private part, never an accepted hash prefix."""
    real_get, calls, _ = _registry()
    failed = []

    def get(client, path, **kwargs):
        """Fail once halfway through range zero and require its spool gone before replay."""
        if kwargs["headers"].get("Range") == f"bytes=0-{R - 1}":
            if not failed:
                response = _fault_response(
                    PAYLOAD[:R // 2], requests.exceptions.ConnectionError("synthetic transport fault"),
                )
                response.status_code = 206
                response.headers.update({
                    "Content-Range": f"bytes 0-{R - 1}/{len(PAYLOAD)}", "ETag": '"stable"',
                })
                failed.append(response)
                return response
            assert failed[0].close_calls == 1
            assert not list(tmp_path.glob("regshape-ranges-*/*"))
        return real_get(client, path, **kwargs)

    statuses = []
    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(_Worker, "wait"),
    ):
        info = get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path),
            download_status_callback=statuses.append,
        )
    assert info.size == len(PAYLOAD) and len(calls) == 6
    assert statuses[-1].stats.bytes_received == len(PAYLOAD) + R // 2
    assert statuses[-1].stats.bytes_accepted == len(PAYLOAD)
    assert statuses[-1].stats.retry_count == 1


@pytest.mark.parametrize("stage", ["first", "later"])
@pytest.mark.parametrize("fault", [IncompleteRead(b"", 123), ConnectionResetError("synthetic reset")])
def test_range_wire_interruption_retries_only_uncommitted_part(tmp_path, stage, fault):
    """Real wire-error translation retries one private range without reaccepting earlier bytes."""
    target = 0 if stage == "first" else R
    failed, statuses, waits = [], [], []

    def modify(start, end, headers, response):
        """Fail raw urllib3 receipt after a prefix, then require its private spool removed."""
        if start == target and "Range" in headers:
            if not failed:
                def stream(chunk_size, decode_content=False):
                    """Raise the actual urllib3 wrapper that entity_chunks maps to Requests."""
                    yield PAYLOAD[start:start + R // 2]
                    raise ProtocolError("Connection broken", fault)

                response.raw.stream = stream
                failed.append(response)
            else:
                assert failed[0].close_calls == 1
                if stage == "first":
                    assert not list(tmp_path.glob("regshape-ranges-*/*"))
        return response

    get, calls, responses = _registry(modify=modify)
    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(_Worker, "wait", side_effect=waits.append),
    ):
        info = get_blob(
            _client(), "repo", address(PAYLOAD), total_size=len(PAYLOAD),
            download_options=_options(tmp_path), download_status_callback=statuses.append,
        )
    target_header = f"bytes={target}-{target + R - 1}"
    assert info.size == len(PAYLOAD) and len(calls) == 7
    assert sum(call["Range"] == target_header for call in calls) == 2
    assert all(response.close_calls == 1 for response in responses)
    assert waits == [1]
    assert statuses[-1].stats.bytes_received == len(PAYLOAD) + R // 2
    assert statuses[-1].stats.bytes_accepted == len(PAYLOAD)
    assert statuses[-1].stats.retry_count == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("retries,excluded", [(0, False), (2, False), (2, True)])
def test_range_wire_interruption_respects_budget_and_exception_policy(tmp_path, retries, excluded):
    """Repeated broken framing consumes exactly the allowed part budget, never strategy fallback."""
    statuses, waits = [], []
    error = ProtocolError("Connection broken", IncompleteRead(b"", 123))

    def modify(start, end, headers, response):
        """Fail before any range can be accepted or shared with a sibling."""
        def stream(chunk_size, decode_content=False):
            """Expose the real wire wrapper after bounded partial receipt."""
            yield PAYLOAD[:R // 2]
            raise error

        response.raw.stream = stream
        return response

    client = _client(retry_config=RetryConfig(exceptions=(), backoff_factor=0)) if excluded else _client()
    get, calls, responses = _registry(modify=modify)
    with (
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        patch.object(_Worker, "wait", side_effect=waits.append),
        pytest.raises(BlobError) as caught,
    ):
        get_blob(
            client, "repo", address(PAYLOAD), download_options=_options(tmp_path, max_retries=retries),
            download_status_callback=statuses.append,
        )
    budget = 0 if excluded else retries
    assert len(calls) == len(responses) == budget + 1 and len(waits) == budget
    assert all(response.close_calls == 1 for response in responses)
    assert isinstance(caught.value.__cause__, requests.exceptions.ChunkedEncodingError)
    assert statuses[-1].stats.bytes_accepted == 0
    assert statuses[-1].stats.retry_count == budget
    assert not any(event.event == "fallback" for event in statuses)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
def test_full_fallback_body_cannot_replay_after_commit(tmp_path, saved, bounded):
    """The strategy reset does not grant transport replay after the fresh full stream commits bytes."""
    path = tmp_path / "output"
    primary = requests.exceptions.ConnectionError("synthetic full-body fault")
    full_calls = []

    def modify(start, end, headers, response):
        """Rotate after range zero, then fail a full stream after a committed hash/file prefix."""
        if "Range" not in headers:
            full_calls.append(headers)
            return _fault_response(PAYLOAD[:123], primary)
        if start >= R:
            response.headers["ETag"] = '"rotated"'
        return response

    get, _, _ = _registry(modify=modify)
    statuses = []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(
        requests.exceptions.ConnectionError,
    ) as caught:
        get_blob(
            _client(), "repo", address(PAYLOAD), str(path) if saved else None,
            total_size=len(PAYLOAD), max_bytes=len(PAYLOAD) if bounded else None,
            download_options=_options(tmp_path, max_retries=3),
            download_status_callback=statuses.append,
        )
    assert caught.value is primary and len(full_calls) == 1
    assert not any(event.event == "completed" for event in statuses)
    assert sum(event.event == "fallback" for event in statuses) == 1
    if saved and not bounded:
        assert path.read_bytes() == PAYLOAD[:123]
        path.unlink()
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="replacing an open file requires POSIX rename semantics")
def test_replaced_sink_prevents_late_fallback_and_preserves_replacement(tmp_path):
    """A retained output cannot be truncated for fallback after a caller replaces its identity."""
    path, original = tmp_path / "output", tmp_path / "retained-original"
    full_calls, statuses = [], []

    def replace_output(event):
        """Replace only after the ranged prefix committed, before the validator rotation."""
        if event.attempt == 1 and event.bytes_completed == R and not original.exists():
            path.rename(original)
            path.write_bytes(b"do not truncate or remove")

    def modify(start, end, headers, response):
        """Only the old ranged attempt may run; an unsafe sink reset must prevent full GET."""
        if "Range" not in headers:
            full_calls.append(headers)
        elif start >= R:
            response.headers["ETag"] = '"rotated"'
        return response

    get, _, _ = _registry(modify=modify)
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(
        OSError, match="replaced",
    ):
        get_blob(
            _client(), "repo", address(PAYLOAD), str(path), total_size=len(PAYLOAD),
            download_options=_options(tmp_path), progress_callback=replace_output,
            download_status_callback=statuses.append,
        )
    assert not full_calls
    failed = next(event for event in statuses if event.event == "failed")
    assert failed.reason == "etag_changed" and failed.attempt == 1
    assert failed.stats.bytes_accepted == R
    assert not any(event.event == "fallback" for event in statuses)
    assert path.read_bytes() == b"do not truncate or remove"
    assert set(tmp_path.iterdir()) == {path, original}


@pytest.mark.parametrize("changes", [
    {"Content-Encoding": "gzip"},
    {"ETag": 'W/"weak"'},
    {"Content-Range": f"bytes {R}-{2 * R - 1}/{len(PAYLOAD) + 1}"},
    {"Content-Length": "7"},
])
def test_late_malformed_headers_cannot_hide_behind_validator_rotation(tmp_path, changes):
    """Changing ETag does not excuse an independently invalid late coding/range/length."""
    def modify(start, end, headers, response):
        """Inject late protocol corruption alongside an otherwise fallback-eligible rotation."""
        if start >= R and "Range" in headers:
            response.headers["ETag"] = '"rotated"'
            response.headers.update(changes)
        return response

    get, calls, _ = _registry(modify=modify)
    statuses = []
    with patch.object(RegistryClient, "get", autospec=True, side_effect=get), pytest.raises(BlobError):
        get_blob(
            _client(), "repo", address(PAYLOAD), total_size=len(PAYLOAD),
            download_options=_options(tmp_path), download_status_callback=statuses.append,
        )
    assert not any("Range" not in call for call in calls)
    assert not any(event.event == "fallback" for event in statuses)
    assert not list(tmp_path.iterdir())


def test_fallback_does_not_discard_a_joined_sibling_protocol_failure(tmp_path):
    """A competing fallback must not erase a malformed sibling result whose diagnostics are queued."""
    from regshape.libs.blobs._ranges import _RangePart
    from regshape.libs.transport._coordinator import _Status

    original = _RangePart.run
    barrier = threading.Barrier(2)
    primary = BlobError("synthetic malformed sibling range")

    def run(part, worker):
        """Complete two sibling outcomes together, with an undelivered record on the corrupt one."""
        if part.start in (R, 2 * R) and not part.full:
            barrier.wait(timeout=5)
            if part.start == 2 * R:
                for _ in range(2):
                    worker.emit(_Status("retry", "ranged", "http_status", 503))
                raise primary
        return original(part, worker)

    def modify(start, end, headers, response):
        """Let the lower of the competing jobs request a valid ETag fallback."""
        if start == R and "Range" in headers:
            response.headers["ETag"] = '"rotated"'
        return response

    get, calls, _ = _registry(modify=modify)
    with (
        patch.object(_RangePart, "run", autospec=True, side_effect=run),
        patch.object(RegistryClient, "get", autospec=True, side_effect=get),
        pytest.raises(BlobError) as caught,
    ):
        get_blob(
            _client(), "repo", address(PAYLOAD), download_options=_options(tmp_path),
        )
    assert caught.value is primary
    assert all("Range" in call for call in calls)
    assert not list(tmp_path.iterdir())


def test_unknown_zero_416_still_requires_verified_full_get(tmp_path):
    """A validated empty 416 selects a real empty full GET instead of declaring range success."""
    statuses = []
    with patch.object(RegistryClient, "get", side_effect=[
        StreamResponse(b"", status=416, headers={"Content-Range": "bytes */0"}),
        StreamResponse(b""),
    ]) as get:
        info = get_blob(
            _client(), "repo", address(b""), download_options=_options(tmp_path),
            download_status_callback=statuses.append,
        )
    assert get.call_count == 2 and info.size == 0
    assert sum(event.event == "completed" for event in statuses) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [403, 404, 416])
def test_terminal_http_failures_do_not_fallback(tmp_path, status):
    """Authentication/missing/range rejection stays explicit instead of triggering a full download."""
    with patch.object(RegistryClient, "get", return_value=StreamResponse(b"", status=status)) as get:
        with pytest.raises(BlobError) as caught:
            get_blob(_client(), "repo", address(PAYLOAD), download_options=_options(tmp_path))
    assert caught.value.status_code == status and get.call_count == 1
    assert not list(tmp_path.iterdir())


def test_compressed_http_fallback_verifies_stored_blob_bytes(tmp_path):
    """HTTP coding is decoded on the full path; stored compression remains ordinary entity bytes."""
    stored = gzip.compress(PAYLOAD, mtime=0)
    first = _range(stored, 0, len(stored) - 1, **{"Content-Encoding": "gzip"})
    second = StreamResponse(gzip.compress(stored, mtime=0), headers={"Content-Encoding": "gzip"})
    with patch.object(RegistryClient, "get", side_effect=[first, second]):
        info = get_blob(_client(), "repo", address(stored), download_options=_options(tmp_path))
    assert info.size == len(stored)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def range_server():
    """Run a local framed range registry, with deterministic delayed-prefix controls."""
    state = {
        "lock": threading.Lock(), "requests": [], "active": 0, "peak": 0,
        "higher": [threading.Event(), threading.Event()], "release": threading.Event(),
        "beyond": threading.Event(), "errors": [], "throttle": 0, "throttle_calls": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        """Serve byte-identical single ranges while deliberately completing later parts first."""

        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            """Keep synthetic HTTP traffic out of test output."""

        def do_GET(self):
            """Block range one until two later results fill its still-uncommitted window."""
            with state["lock"]:
                throttled = state["throttle"] > 0
                if throttled:
                    state["throttle"] -= 1
                    state["throttle_calls"] += 1
            if throttled:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            interval = self.headers.get("Range")
            start, end = map(int, interval.removeprefix("bytes=").split("-"))
            end = min(end, len(PAYLOAD) - 1)
            with state["lock"]:
                state["requests"].append((interval, self.headers.get("If-Range")))
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                if start >= 4 * R:
                    state["beyond"].set()
                if start == R and not state["release"].wait(5):
                    state["errors"].append("blocked prefix was not released")
                body = PAYLOAD[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
                self.send_header("ETag", '"stable"')
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if start in (2 * R, 3 * R):
                    state["higher"][start // R - 2].set()
            finally:
                with state["lock"]:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", state
    finally:
        state["release"].set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_http_ranges_overlap_without_advancing_past_committed_window(tmp_path, range_server):
    """Physical requests overlap, but completed later spools cannot authorize unbounded scheduling."""
    authority, state = range_server
    faults = []

    def release():
        """Inspect the full uncommitted window before releasing its blocked lowest range."""
        try:
            assert all(event.wait(5) for event in state["higher"])
            assert not state["beyond"].wait(0.1)
            spools = list(tmp_path.glob("regshape-ranges-*/*"))
            assert sum(path.stat().st_size for path in spools) <= 3 * R
        except BaseException as error:
            faults.append(error)
        finally:
            state["release"].set()

    monitor = threading.Thread(target=release)
    monitor.start()
    path = tmp_path / "output"
    statuses = []
    try:
        info = get_blob(
            _client(authority), "repo", address(PAYLOAD), str(path),
            total_size=len(PAYLOAD), download_options=_options(tmp_path),
            download_status_callback=statuses.append,
        )
    finally:
        state["release"].set()
        monitor.join()
    assert not faults and not state["errors"]
    assert 2 <= state["peak"] <= 3
    assert len(state["requests"]) == 6
    assert state["requests"][0] == (f"bytes=0-{R - 1}", None)
    assert all(etag == '"stable"' for _, etag in state["requests"][1:])
    assert info.size == len(PAYLOAD) and path.read_bytes() == PAYLOAD
    summary = statuses[-1]
    assert summary.event == "summary" and summary.stats.request_count == 6
    assert summary.stats.scratch_bytes_peak <= 3 * R
    assert 2 <= summary.stats.active_requests_peak <= 3
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_cli_retry_override_reaches_real_http_429(runner, tmp_path, range_server, retries):
    """The CLI's explicit zero/bounded budgets govern physical 429 attempts, not just SDK arguments."""
    authority, state = range_server
    state["release"].set()
    state["throttle"] = 3
    waits = []
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch.object(_Worker, "wait", side_effect=waits.append),
    ):
        result = runner.invoke(regshape, [
            "--insecure", "blob", "get", "--repo", f"{authority}/repo",
            "--digest", address(PAYLOAD), "--ranges-per-blob", "3", "--range-size", str(R),
            "--download-retries", str(retries), "--scratch-dir", str(tmp_path), "--no-progress",
        ])
    assert state["throttle_calls"] == min(retries + 1, 3)
    assert len(waits) == retries
    if retries == 3:
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["size"] == len(PAYLOAD)
        assert len(state["requests"]) == 6
    else:
        assert result.exit_code == 1 and not result.stdout
        assert "--download-retries" in result.stderr
        assert not state["requests"]
    assert not list(tmp_path.iterdir())

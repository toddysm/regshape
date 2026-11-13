"""Verify isolated full-stream sinks, pre-commit retries and slow-read shutdown.

Synthetic streams exercise byte faults; ephemeral HTTP servers prove physical
request accounting, scoped authentication and cancellation without false deadlines.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
import requests

from regshape.libs.blobs import DownloadOptions
from regshape.libs.blobs._download_worker import _FullBlob
from regshape.libs.errors import AuthError, BlobError
from regshape.libs.progress import TransferPhase
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport._coordinator import (
    _Coordinator,
    _DownloadCancelled,
    _Worker,
)
from regshape.libs.transport.download import DownloadTransport
from regshape.libs.transport.middleware import RetryConfig
from regshape.tests.artifact_fixtures import StreamResponse, address
from regshape.tests.test_download_transport import (
    server,
)  # noqa: F401 - shared loopback fixture


@pytest.fixture
def client():
    """Create an anonymous borrowed client without credential-store effects."""
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=(None, None)
    ):
        return RegistryClient(TransportConfig("registry.example"))


def _receive(client, blob, *, options=None, progress=None, status=None):
    """Execute one internal worker and return its verified info plus final core stats."""
    results = []
    with DownloadTransport(client, options or DownloadOptions.sequential()) as root:
        root._download_context.retry_config.backoff_factor = 0
        with _Coordinator(
            root._download_context,
            progress_callback=progress,
            status_callback=status,
        ) as coordinator:
            stats = coordinator.run([[blob.job()]], on_result=results.append)
    return results[0], stats


def _fault_response(prefix, error):
    """Yield an optional committed prefix then raise the exact synthetic read error."""
    response = StreamResponse(b"")

    def stream(chunk_size, decode_content=False):
        """Replace only the wire iterator, retaining real response ownership."""
        if prefix:
            yield prefix
        raise error

    response.raw.stream = stream
    return response


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
@pytest.mark.parametrize("payload", [b"", b"isolated full-stream bytes"])
def test_full_stream_verify_and_output(client, tmp_path, saved, algorithm, payload):
    """File and hash-and-discard results agree, including zero/unknown-length bodies."""
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(payload, algorithm), str(path) if saved else None)
    events = []
    response = StreamResponse(payload)
    with patch.object(RegistryClient, "get", return_value=response):
        info, stats = _receive(client, blob, progress=events.append)
    assert info.digest == address(payload, algorithm)
    assert info.size == len(payload)
    assert stats.bytes_received == stats.bytes_accepted == len(payload)
    assert response.close_calls == 1
    assert events[0].phase == TransferPhase.PREPARING
    assert events[-1].phase == TransferPhase.COMPLETED
    assert {event.transfer_id for event in events} == {blob.transfer_id}
    if saved:
        assert path.read_bytes() == payload
    else:
        assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink privilege")
def test_retargeted_symlink_never_becomes_a_verified_result(client, tmp_path):
    """Following a trusted output link must still reject a different target before publication."""
    target, link, substitute = tmp_path / "target", tmp_path / "link", tmp_path / "substitute"
    target.write_bytes(b"original")
    substitute.write_bytes(b"unverified")
    link.symlink_to(target)
    payload = b"verified blob"
    blob = _FullBlob("repo", address(payload), str(link))

    def retarget(event):
        """Redirect only after all payload writes; worker or caller revalidation must fail."""
        if event.phase == TransferPhase.VERIFYING:
            link.unlink()
            link.symlink_to(substitute)

    with (
        patch.object(RegistryClient, "get", return_value=StreamResponse(payload)),
        pytest.raises(OSError, match="replaced"),
    ):
        _receive(client, blob, progress=retarget)
    assert link.is_symlink() and link.resolve() == substitute
    assert substitute.read_bytes() == b"unverified"
    assert target.read_bytes() == payload


def test_hash_and_write_stay_on_worker_callbacks_and_publication_on_caller(
    client, tmp_path
):
    """The central thread never becomes a full-stream payload-copy/hash bottleneck."""
    caller = threading.get_ident()
    writer_threads = []
    observer_threads = []
    results = []
    payload = b"worker bytes"
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(payload), str(path), max_bytes=100)
    original_write = blob.output.write

    def write(chunk):
        """Observe actual per-file writes without replacing sink validation."""
        writer_threads.append(threading.get_ident())
        original_write(chunk)

    def observe(event):
        """Record where existing immutable public progress is delivered."""
        observer_threads.append(threading.get_ident())

    def publish(info):
        """Stand in for caller-owned ContentStore registration/publication."""
        assert threading.get_ident() == caller
        assert blob.output.closed
        assert path.read_bytes() == payload
        results.append(info)

    blob.output.write = write
    with patch.object(RegistryClient, "get", return_value=StreamResponse(payload)):
        with DownloadTransport(client, DownloadOptions.sequential()) as root:
            with _Coordinator(
                root._download_context, progress_callback=observe
            ) as coordinator:
                coordinator.run([[blob.job()]], on_result=publish)
    assert writer_threads and caller not in writer_threads
    assert set(observer_threads) == {caller}
    assert len(results) == 1


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
def test_no_retry_after_payload_commit(client, tmp_path, saved, bounded):
    """A read failure cannot replay an already hashed/written prefix, even verify-only."""
    path = tmp_path / "blob"
    error = requests.exceptions.ConnectionError("synthetic read fault")
    response = _fault_response(b"prefix", error)
    blob = _FullBlob(
        "repo",
        address(b"prefix tail"),
        str(path) if saved else None,
        max_bytes=100 if bounded else None,
    )
    with patch.object(RegistryClient, "get", return_value=response) as get:
        with pytest.raises(requests.exceptions.ConnectionError) as caught:
            _receive(client, blob, options=DownloadOptions(max_retries=3))
    assert caught.value is error
    assert get.call_count == 1
    assert response.close_calls == 1
    if saved and not bounded:
        assert path.read_bytes() == b"prefix"
    else:
        assert not path.exists()


@pytest.mark.parametrize("saved", [False, True])
def test_retry_before_any_commit(client, tmp_path, saved):
    """A pre-payload body failure retries without replaying bytes or reopening an exclusive sink."""
    payload = b"retry succeeds"
    path = tmp_path / "blob"
    first = _fault_response(b"", requests.exceptions.ConnectionError("before payload"))
    second = StreamResponse(payload)
    statuses = []
    blob = _FullBlob(
        "repo",
        address(payload),
        str(path) if saved else None,
        max_bytes=100,
    )
    with patch.object(RegistryClient, "get", side_effect=[first, second]) as get:
        info, stats = _receive(
            client,
            blob,
            options=DownloadOptions(max_retries=2),
            status=statuses.append,
        )
    assert get.call_count == 2 and info.size == len(payload)
    assert stats.retry_count == 1
    assert stats.bytes_received == stats.bytes_accepted == len(payload)
    assert first.close_calls == second.close_calls == 1
    assert [
        event.status.reason for event in statuses if event.status.event == "retry"
    ] == ["transport_error"]
    if saved:
        assert path.read_bytes() == payload


@pytest.mark.parametrize("stage", ["dispatch", "body", "error_body"])
@pytest.mark.parametrize("saved", [False, True])
def test_configured_transport_exception_retries(client, tmp_path, stage, saved):
    """Configured non-Requests failures retry dispatch or body receipt before commitment."""
    client.config.retry_config = RetryConfig(exceptions=(OSError,))
    payload = b"retry succeeds"
    error = OSError("configured transport fault")
    first = _fault_response(b"", error)
    if stage == "error_body":
        first.status_code = 403
    responses = [
        error if stage == "dispatch" else first,
        StreamResponse(payload),
    ]
    path = tmp_path / "blob"
    blob = _FullBlob(
        "repo",
        address(payload),
        str(path) if saved else None,
        max_bytes=100,
    )
    with patch.object(RegistryClient, "get", side_effect=responses) as get:
        info, stats = _receive(client, blob, options=DownloadOptions(max_retries=1))
    assert get.call_count == 2 and info.size == len(payload)
    assert stats.retry_count == 1
    assert stats.bytes_received == stats.bytes_accepted == len(payload)
    if stage != "dispatch":
        assert first.close_calls == 1
    if saved:
        assert path.read_bytes() == payload


@pytest.mark.parametrize("committed", [False, True])
def test_configured_transport_exception_budget_and_commit(client, committed):
    """Configured exception identity survives budget exhaustion or a committed prefix."""
    client.config.retry_config = RetryConfig(exceptions=(OSError,))
    error = OSError("configured body fault")
    responses = [
        _fault_response(b"prefix" if committed else b"", error),
        _fault_response(b"", error),
    ]
    with patch.object(RegistryClient, "get", side_effect=responses) as get:
        with pytest.raises(OSError) as caught:
            _receive(
                client,
                _FullBlob("repo", address(b"prefix rest")),
                options=DownloadOptions(max_retries=1),
            )
    assert caught.value is error
    assert get.call_count == (1 if committed else 2)


@pytest.mark.parametrize("stage", ["open", "write", "close"])
def test_configured_transport_exception_never_retries_sink(client, tmp_path, stage):
    """Matching sink failures remain non-retryable, including before any payload commit."""
    client.config.retry_config = RetryConfig(exceptions=(OSError,))
    payload = b"" if stage == "close" else b"payload"
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(payload), str(path), max_bytes=100)
    error = OSError("sink fault")
    original_close = blob.output.close

    def fail(*args):
        """Close the real sink before injecting a close fault, leaving no leaked handle."""
        if stage == "close":
            original_close()
        raise error

    with (
        patch.object(
            RegistryClient, "get", return_value=StreamResponse(payload)
        ) as get,
        patch.object(blob.output, stage, fail),
    ):
        with pytest.raises(OSError) as caught:
            _receive(client, blob, options=DownloadOptions(max_retries=2))
    assert caught.value is error
    assert get.call_count == 1
    assert not path.exists()


def test_broad_retry_exception_never_retries_validation(client):
    """A broad configured exception policy cannot replay a rejected entity budget."""
    client.config.retry_config = RetryConfig(exceptions=(Exception,))
    with patch.object(
        RegistryClient, "get", return_value=StreamResponse(b"payload")
    ) as get:
        with pytest.raises(BlobError, match="max_bytes"):
            _receive(
                client,
                _FullBlob("repo", address(b"payload"), max_bytes=1),
                options=DownloadOptions(max_retries=2),
            )
    assert get.call_count == 1


@pytest.mark.parametrize(
    "failure", ["missing_credentials", "unsafe_scope", "token_rejected"]
)
def test_broad_retry_policy_never_retries_authentication_rejection(
    server, tmp_path, failure
):
    """Permanent authentication rejection cannot consume a configured transport retry."""
    authority, state = server
    credentials: tuple[str | None, str | None] = ("test-user", "test-password")
    if failure == "missing_credentials":
        state.mode = "basic"
        credentials = (None, None)
    elif failure == "unsafe_scope":
        state.challenge_scope = "repository:other:pull"
    else:
        state.token_status = 403
    with patch(
        "regshape.libs.transport.client.resolve_credentials",
        return_value=credentials,
    ):
        client = RegistryClient(
            TransportConfig(
                authority,
                insecure=True,
                retry_config=RetryConfig(exceptions=(Exception,)),
            )
        )
    path = tmp_path / "blob"
    path.write_bytes(b"caller output")
    blob = _FullBlob("repo", address(state.payload), str(path))
    statuses = []
    with pytest.raises(AuthError):
        _receive(
            client,
            blob,
            options=DownloadOptions(max_retries=2),
            status=statuses.append,
        )
    assert state.requests == state.unauthorized == 1
    assert len(state.token_calls) == (1 if failure == "token_rejected" else 0)
    assert not blob.committed
    assert path.read_bytes() == b"caller output"
    assert not any(event.status.event == "retry" for event in statuses)


@pytest.mark.parametrize(
    "stage",
    ["redirect_limit", "redirect_encoding", "body_encoding", "error_encoding"],
)
def test_broad_retry_policy_never_hides_protocol_rejection(client, tmp_path, stage):
    """Actual response-hook/decoder validation cannot be hidden by a later successful response."""
    client.config.retry_config = RetryConfig(exceptions=(Exception,))
    if stage == "redirect_limit":
        first = StreamResponse(b"x" * 65_537, status=302)
    else:
        first = StreamResponse(
            b"not a gzip stream",
            status=(
                302
                if stage == "redirect_encoding"
                else 403 if stage == "error_encoding" else 200
            ),
            headers={"Content-Encoding": "gzip"},
        )
    second = StreamResponse(b"valid payload")
    responses = iter([first, second])
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(b"valid payload"), str(path), max_bytes=100)
    statuses = []

    def dispatch(path, **kwargs):
        """Run the real pre-return response hook, as Requests does before handing back a response."""
        response = next(responses)
        kwargs["response_hook"](response)
        return response

    with patch.object(RegistryClient, "get", side_effect=dispatch) as get:
        with pytest.raises(
            BlobError
            if stage == "redirect_limit"
            else requests.exceptions.ContentDecodingError
        ):
            _receive(
                client,
                blob,
                options=DownloadOptions(max_retries=2),
                status=statuses.append,
            )
    assert get.call_count == 1
    assert not blob.committed
    assert not path.exists()
    assert first.close_calls == 1 and second.close_calls == 0
    assert not any(event.status.event == "retry" for event in statuses)


@pytest.mark.parametrize("bounded", [False, True])
def test_digest_mismatch_removes_invocation_output(client, tmp_path, bounded):
    """Invalid ordinary overwrites and exclusive staging files are removed by identity."""
    path = tmp_path / "blob"
    if not bounded:
        path.write_bytes(b"old overwritten bytes")
    blob = _FullBlob(
        "repo",
        address(b"expected"),
        str(path),
        max_bytes=100 if bounded else None,
    )
    with patch.object(RegistryClient, "get", return_value=StreamResponse(b"wrong")):
        with pytest.raises(BlobError, match="digest"):
            _receive(client, blob)
    assert not path.exists()


@pytest.mark.parametrize("replacement_phase", ["stream", "cleanup"])
def test_digest_mismatch_never_removes_replacement(
    client, tmp_path, caplog, replacement_phase
):
    """Preserve replacements during POSIX receipt or after closed receipt on every platform."""
    if replacement_phase == "stream" and os.name == "nt":
        pytest.skip("Windows does not permit renaming this open output handle")
    path = tmp_path / "blob"
    path.write_bytes(b"old")
    response = StreamResponse(b"wrong")
    blob = _FullBlob("repo", address(b"expected"), str(path))

    def replace_output():
        """Retain the old inode under another name so its identity cannot be recycled."""
        path.rename(tmp_path / "owned-output")
        path.write_bytes(b"replacement")

    if replacement_phase == "stream":

        def stream(chunk_size, decode_content=False):
            """Replace the entry while a POSIX worker still holds its old inode open."""
            yield b"wrong"
            replace_output()

        response.raw.stream = stream

    original_cleanup = blob.output.cleanup

    def cleanup(accepted):
        """Replace only after worker shutdown when exercising the portable cleanup race."""
        if replacement_phase == "cleanup":
            assert blob.output.closed
            replace_output()
        original_cleanup(accepted)

    with (
        patch.object(RegistryClient, "get", return_value=response),
        patch.object(blob.output, "cleanup", cleanup),
    ):
        with pytest.raises(BlobError, match="digest"):
            _receive(client, blob)
    assert path.read_bytes() == b"replacement"
    assert (tmp_path / "owned-output").read_bytes() == b"wrong"
    assert "preserving the original error" in caplog.text


def test_response_close_error_does_not_replace_body_error(client, caplog):
    """Secondary response disposal errors preserve the original exception object."""
    primary = requests.exceptions.ConnectionError("committed body fault")
    response = _fault_response(b"prefix", primary)
    original_close = response.close

    def close():
        """Close the real synthetic resource before raising its secondary fault."""
        original_close()
        raise OSError("secondary close")

    response.close = close
    with patch.object(RegistryClient, "get", return_value=response):
        with pytest.raises(requests.exceptions.ConnectionError) as caught:
            _receive(client, _FullBlob("repo", address(b"prefix rest")))
    assert caught.value is primary
    assert "preserving the original error" in caplog.text


def test_short_write_is_not_retryable(client, tmp_path):
    """A disk sink's partial commitment is neither success nor a replayable transport error."""
    blob = _FullBlob("repo", address(b"payload"), str(tmp_path / "blob"), max_bytes=100)
    original_open = blob.output.open

    def open_output():
        """Wrap only writes, retaining the real exclusive file and identity."""
        original_open()
        original_file = blob.output.file

        class ShortWriter:
            """Delegate lifetime methods but deliberately violate full-write requirements."""

            def write(self, chunk):
                """Write one byte and return its actual short length."""
                return original_file.write(chunk[:1])

            def close(self):
                """Close the real owned handle."""
                original_file.close()

        blob.output.file = ShortWriter()

    blob.output.open = open_output
    with patch.object(
        RegistryClient, "get", return_value=StreamResponse(b"payload")
    ) as get:
        with pytest.raises(OSError, match="Short write"):
            _receive(client, blob, options=DownloadOptions(max_retries=3))
    assert get.call_count == 1
    assert not (tmp_path / "blob").exists()


@pytest.mark.parametrize("size", [0, 3, 10])
def test_trusted_size_mismatch_and_prewritten_byte_cap(client, tmp_path, size):
    """A declared size is checked before writes and at EOF, not inferred from digest success."""
    path = tmp_path / "blob"
    payload = b"12345"
    blob = _FullBlob(
        "repo", address(payload), str(path), total_size=size, max_bytes=100
    )
    with patch.object(RegistryClient, "get", return_value=StreamResponse(payload)):
        with pytest.raises(BlobError, match="size"):
            _receive(client, blob)
    assert not path.exists()


def test_max_bytes_and_exclusive_output(client, tmp_path):
    """Unknown-length bodies remain pre-write bounded and never replace existing bounded output."""
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(b"12345"), str(path), max_bytes=3)
    with patch.object(RegistryClient, "get", return_value=StreamResponse(b"12345")):
        with pytest.raises(BlobError, match="max_bytes"):
            _receive(client, blob)
    assert not path.exists()
    path.write_bytes(b"original")
    other = _FullBlob("repo", address(b"ok"), str(path), max_bytes=10)
    with patch.object(RegistryClient, "get", return_value=StreamResponse(b"ok")):
        with pytest.raises(FileExistsError):
            _receive(client, other)
    assert path.read_bytes() == b"original"


def test_callback_failure_removes_bounded_completed_output(client, tmp_path):
    """Even a fast worker's completed staging file is discarded after observer failure."""
    payload = b"data"
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(payload), str(path), max_bytes=10)
    error = RuntimeError("callback fault")
    failures = []

    def callback(event):
        """Fail after payload commitment, not on initial preparation."""
        if event.bytes_completed:
            failures.append(True)
            raise error

    with patch.object(RegistryClient, "get", return_value=StreamResponse(payload)):
        with pytest.raises(RuntimeError) as caught:
            _receive(client, blob, progress=callback)
    assert caught.value is error
    assert failures == [True]
    assert not path.exists()


def test_callback_replacement_after_worker_check_never_publishes(client, tmp_path):
    """Caller-side identity validation rejects a closed output replaced by a queued callback."""
    payload = b"verified payload"
    path = tmp_path / "blob"
    blob = _FullBlob("repo", address(payload), str(path), max_bytes=100)
    worker_checked = threading.Event()
    published = []
    original_verify = blob.output.verify_identity

    def verify():
        """Signal only after the worker's first closed-output identity check succeeds."""
        original_verify()
        worker_checked.set()

    def progress(event):
        """Replace the closed filename only after its worker-side check has succeeded."""
        if event.phase == TransferPhase.VERIFYING:
            assert worker_checked.wait(5)
            path.rename(tmp_path / "verified-output")
            path.write_bytes(b"unverified replacement")

    with (
        patch.object(RegistryClient, "get", return_value=StreamResponse(payload)),
        patch.object(blob.output, "verify_identity", verify),
        DownloadTransport(client, DownloadOptions.sequential()) as root,
    ):
        with _Coordinator(
            root._download_context, progress_callback=progress
        ) as coordinator:
            with pytest.raises(OSError, match="replaced"):
                coordinator.run([[blob.job()]], on_result=published.append)
    assert published == []
    assert path.read_bytes() == b"unverified replacement"
    assert (tmp_path / "verified-output").read_bytes() == payload


def test_precommit_status_retry_single_owner_and_released_slot(client):
    """HTTP retries share one budget and release discarded responses before waiting."""
    first = StreamResponse(b"", status=429, headers={"Retry-After": "2"})
    second = StreamResponse(b"", status=503, headers={"Retry-After": "1"})
    third = StreamResponse(b"ok")
    waits = []
    statuses = []

    def wait(worker, seconds):
        """Assert the previous response has closed before applying its server delay."""
        waits.append(seconds)
        assert (first if len(waits) == 1 else second).close_calls == 1
        worker.check_cancelled()

    with patch.object(RegistryClient, "get", side_effect=[first, second, third]) as get:
        with patch.object(_Worker, "wait", wait):
            info, stats = _receive(
                client,
                _FullBlob("repo", address(b"ok")),
                options=DownloadOptions(max_retries=2),
                status=statuses.append,
            )
    assert info.size == 2 and get.call_count == 3
    assert waits == [2.0, 1.0]
    assert stats.retry_count == 2
    assert [
        event.status.http_status for event in statuses if event.status.event == "retry"
    ] == [429, 503]


def test_cancellation_interrupts_backoff(client):
    """Retry delay does not delay a cancellation or silently dispatch another GET."""
    response = StreamResponse(b"", status=429, headers={"Retry-After": "300"})
    with patch.object(RegistryClient, "get", return_value=response) as get:
        with DownloadTransport(client, DownloadOptions.sequential()) as root:
            coordinator = _Coordinator(root._download_context)

            def cancel_on_retry(event):
                """Cancel on the caller immediately after the retry is observed."""
                if event.status.event == "retry":
                    coordinator.cancel()

            coordinator.status_callback = cancel_on_retry
            with coordinator, pytest.raises(_DownloadCancelled):
                coordinator.run(
                    [[_FullBlob("repo", address(b"ok")).job()]],
                    on_result=lambda value: None,
                )
    assert get.call_count == response.close_calls == 1


@pytest.mark.parametrize("mode", ["anonymous", "bearer"])
def test_full_workers_real_pool_and_physical_request_metrics(server, mode):
    """Independent workers share one C cap, one auth discovery and exact hop counts."""
    authority, state = server
    state.mode = mode
    with patch(
        "regshape.libs.transport.client.resolve_credentials",
        return_value=("test-user", "test-password"),
    ):
        client = RegistryClient(TransportConfig(authority, insecure=True))
    options = DownloadOptions(concurrency=3)
    results = []
    with DownloadTransport(client, options) as root:
        with _Coordinator(root._download_context) as coordinator:
            stats = coordinator.run(
                [[_FullBlob("repo", address(state.payload)).job()] for _ in range(8)],
                on_result=results.append,
            )
    assert len(results) == 8
    assert 1 <= stats.active_requests_peak <= 3
    assert stats.bytes_received == stats.bytes_accepted == 8 * len(state.payload)
    assert stats.request_count == (8 if mode == "anonymous" else 10)
    assert len(state.token_calls) == (0 if mode == "anonymous" else 1)
    assert client.last_response is None


def test_operation_count_includes_root_metadata_outside_worker_jobs(server):
    """Root discovery shares invocation accounting while per-blob counts remain local."""
    authority, state = server
    state.mode = "anonymous"
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=(None, None)
    ):
        client = RegistryClient(TransportConfig(authority, insecure=True))
    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with root.get(
            f"/v2/repo/blobs/{address(state.payload)}", stream=True
        ) as response:
            assert response.content == state.payload
        blob = _FullBlob("repo", address(state.payload))
        with _Coordinator(root._download_context) as coordinator:
            stats = coordinator.run([[blob.job()]], on_result=lambda value: None)
            assert stats.request_count == 2
            assert coordinator.snapshot(blob.transfer_id).request_count == 1


def test_failed_redirect_chain_counts_every_dispatch(client):
    """A later redirect-hop failure cannot hide an earlier physical HTTP attempt."""
    primary = requests.exceptions.ConnectionError("redirect target failed")
    response = StreamResponse(b"", status=302, headers={"Location": "/other"})
    calls = []

    def send(request, **kwargs):
        """Serve the first hop and fail the second without using real network credentials."""
        calls.append(request)
        if len(calls) == 1:
            response.request = request
            response.url = request.url
            return response
        raise primary

    blob = _FullBlob("repo", address(b"expected"))
    with DownloadTransport(client, DownloadOptions(max_retries=0)) as root:
        with _Coordinator(root._download_context) as coordinator:
            with patch.object(root._download_context.adapter, "send", send):
                with pytest.raises(requests.exceptions.ConnectionError) as caught:
                    coordinator.run([[blob.job()]], on_result=lambda value: None)
            assert coordinator.snapshot().request_count == 2
            assert coordinator.snapshot(blob.transfer_id).request_count == 2
    assert caught.value is primary
    assert len(calls) == 2


@pytest.fixture
def trickle_server():
    """Serve an inactivity-safe gated trickle; fixture cleanup releases all blocked handlers."""
    read_started = threading.Event()
    second_byte = threading.Event()
    second_sent = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        """Hold a buffered three-byte read open across explicit cancellation."""

        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            """Keep synthetic HTTP details out of terminal/log sinks."""

        def do_GET(self):
            """Send bytes one and two, waiting for permission to finish the buffered read."""
            try:
                self.send_response(200)
                self.send_header("Content-Length", "3")
                self.end_headers()
                self.wfile.write(b"a")
                self.wfile.flush()
                read_started.set()
                assert second_byte.wait(5)
                self.wfile.write(b"b")
                self.wfile.flush()
                second_sent.set()
                assert release.wait(5)
                self.wfile.write(b"c")
                self.wfile.flush()
            finally:
                finished.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"127.0.0.1:{httpd.server_port}", read_started, second_byte, second_sent, release
    finally:
        second_byte.set()
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        if read_started.is_set():
            assert finished.wait(5)


def test_slow_trickle_cancel_does_not_clean_before_read_returns(
    trickle_server, tmp_path
):
    """Stop scheduling promptly but wait for an active buffered read before sink cleanup."""
    authority, first_sent, second_byte, second_sent, release = trickle_server
    read_entered = threading.Event()
    cancelled_before_release = threading.Event()
    cleanup_calls = []
    unexpected_job = []
    path = tmp_path / "blob"
    with patch(
        "regshape.libs.transport.client.resolve_credentials", return_value=(None, None)
    ):
        client = RegistryClient(
            TransportConfig(authority, insecure=True, timeout=(1, 5))
        )
    blob = _FullBlob("repo", address(b"abc"), str(path), max_bytes=10)
    original_cleanup = blob.output.cleanup
    from regshape.libs.blobs import _download_worker

    original_chunks = _download_worker.entity_chunks

    def chunks(response, chunk_size):
        """Signal entry before the real buffered read blocks awaiting the third byte."""
        read_entered.set()
        yield from original_chunks(response, chunk_size)

    def cleanup(accepted):
        """Assert no cleanup runs while a slow worker still owns the file."""
        assert release.is_set()
        assert blob.output.closed
        cleanup_calls.append(accepted)
        original_cleanup(accepted)

    blob.output.cleanup = cleanup
    with DownloadTransport(client, DownloadOptions.sequential()) as root:
        with _Coordinator(root._download_context) as coordinator:

            def cancel():
                """Cancel during an active trickle, prove ownership remains, then release the server."""
                try:
                    assert read_entered.wait(5) and first_sent.wait(5)
                    coordinator.cancel()
                    second_byte.set()
                    assert second_sent.wait(5)
                    assert not blob.output.closed
                    assert cleanup_calls == []
                    assert path.exists()
                    assert unexpected_job == []
                    cancelled_before_release.set()
                finally:
                    release.set()

            controller = threading.Thread(target=cancel)
            controller.start()
            try:
                with patch.object(_download_worker, "entity_chunks", chunks):
                    with pytest.raises(_DownloadCancelled):
                        coordinator.run(
                            [
                                [blob.job()],
                                [_FullBlob("repo", address(b"unused")).job()],
                            ],
                            on_result=unexpected_job.append,
                        )
            finally:
                release.set()
                controller.join(timeout=5)
            assert not controller.is_alive()
            assert cancelled_before_release.is_set()
            assert cleanup_calls == [False]
            assert not path.exists()

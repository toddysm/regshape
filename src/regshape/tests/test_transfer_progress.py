"""Transfer progress contracts and real-client flows without network/daemon access."""

import hashlib
import io
import json
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from regshape.libs.blobs import (
    TransferDirection,
    TransferPhase,
    TransferProgress,
    get_blob,
    upload_blob_chunked,
)
from regshape.libs.errors import BlobError
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.middleware import RetryConfig

PAYLOAD = b"abcdef"
DIGEST = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()
REPO = "team/image"
REGISTRY = "registry.example.invalid"
UPLOAD = f"/v2/{REPO}/blobs/uploads/session?_state=opaque"


def _response(
    data: bytes = b"", status: int = 200, headers: dict[str, str] | None = None,
) -> requests.Response:
    """Provide a known registry issuer and deterministic, already buffered response content."""
    response = requests.Response()
    response.status_code = status
    response.url = f"https://{REGISTRY}/v2/"
    response._content = data
    response._content_consumed = True
    response.headers.update(headers or {})
    return response


def _client() -> MagicMock:
    """Expose the real client interface while preventing credential or network access."""
    client = MagicMock(spec=RegistryClient)
    client.config = TransportConfig(registry=REGISTRY)
    client.get.return_value = _response(PAYLOAD, headers={"Content-Length": "6"})
    client.post.return_value = _response(status=202, headers={"Location": UPLOAD})
    client.patch.return_value = _response(status=202)
    client.put.return_value = _response(status=201, headers={"Docker-Content-Digest": DIGEST})
    return client


def _counts(events: list[TransferProgress]) -> list[int]:
    """Select nonzero payload observations, excluding lifecycle-only events."""
    return [
        event.bytes_completed for event in events
        if event.phase == TransferPhase.TRANSFERRING and event.bytes_completed
    ]


@pytest.mark.parametrize("changes", [
    {"transfer_id": ""}, {"digest": "https://user:secret@example.invalid"},
    {"direction": "sideways"}, {"phase": "unknown"}, {"bytes_completed": -1},
    {"bytes_completed": True}, {"total_bytes": -1}, {"total_bytes": True},
    {"bytes_completed": 7}, {"attempt": 0}, {"attempt": True},
])
def test_progress_model_rejects_invalid_values(changes: dict) -> None:
    """Public observations reject invalid identities, enums, and counter bounds."""
    event = TransferProgress("id", DIGEST, TransferDirection.DOWNLOAD, TransferPhase.TRANSFERRING, 0, 6)
    with pytest.raises((ValueError, TypeError)):
        replace(event, **changes)


@pytest.mark.parametrize("write_file", [False, True])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
def test_download_observes_chunks_before_return(
    tmp_path: Path, write_file: bool, algorithm: str,
) -> None:
    """Both consumption paths emit live immutable events before successful verification."""
    client = _client()
    events: list[TransferProgress] = []
    digest = algorithm + ":" + hashlib.new(algorithm, PAYLOAD).hexdigest()
    output = str(tmp_path / "blob") if write_file else None

    def chunks(chunk_size: int):
        """Check preceding observations while the function is still streaming."""
        assert chunk_size == 2
        for offset in range(0, 6, 2):
            assert events[-1].bytes_completed == offset
            assert all(event.phase != TransferPhase.COMPLETED for event in events)
            yield PAYLOAD[offset:offset + 2]

    response = client.get.return_value
    with (
        patch.object(response, "iter_content", side_effect=chunks),
        patch.object(response, "close", wraps=response.close) as close,
    ):
        info = get_blob(client, REPO, digest, output, 2, progress_callback=events.append)
    assert info.digest == digest and info.size == 6
    assert _counts(events) == [2, 4, 6]
    assert [event.phase for event in events] == [
        TransferPhase.PREPARING, TransferPhase.TRANSFERRING,
        TransferPhase.TRANSFERRING, TransferPhase.TRANSFERRING, TransferPhase.TRANSFERRING,
        TransferPhase.VERIFYING, TransferPhase.COMPLETED,
    ]
    assert len({event.transfer_id for event in events}) == 1
    assert all(event.attempt == 1 and event.digest == digest for event in events)
    assert all(event.total_bytes == 6 for event in events[1:])
    close.assert_called_once()
    if output:
        assert Path(output).read_bytes() == PAYLOAD
    with pytest.raises(FrozenInstanceError):
        events[-1].bytes_completed = 0  # type: ignore[misc]


@pytest.mark.parametrize("headers", [
    {}, {"Content-Length": "bad"}, {"Content-Length": "-1"},
    {"Content-Length": "1_0"}, {"Content-Length": "+6"},
    {"Content-Length": "9", "Content-Encoding": "gzip"},
    {"Content-Length": "9", "Transfer-Encoding": "chunked"},
])
def test_download_untrustworthy_length_stays_unknown(headers: dict[str, str]) -> None:
    """Encoded, invalid, and missing metadata cannot invent a payload denominator."""
    client = _client()
    client.get.return_value = _response(PAYLOAD, headers=headers)
    events: list[TransferProgress] = []
    get_blob(client, REPO, DIGEST, chunk_size=2, progress_callback=events.append)
    assert _counts(events) == [2, 4, 6]
    assert all(event.total_bytes is None for event in events)
    assert events[-1].phase == TransferPhase.COMPLETED


def test_explicit_download_total_uses_consumed_byte_units() -> None:
    """A trusted caller size can describe decoded bytes despite wire encoding."""
    client = _client()
    client.get.return_value = _response(PAYLOAD, headers={"Content-Length": "99", "Content-Encoding": "gzip"})
    events: list[TransferProgress] = []
    get_blob(client, REPO, DIGEST, total_size=6, progress_callback=events.append)
    assert all(event.total_bytes == 6 for event in events)


@pytest.mark.parametrize("total", [0, 3, 7])
@pytest.mark.parametrize("upload", [False, True])
def test_size_mismatch_is_explicit(total: int, upload: bool) -> None:
    """Reject under/overruns without clamping counts or issuing an upload commit."""
    client = _client()
    events: list[TransferProgress] = []
    with pytest.raises(BlobError, match="byte count"):
        if upload:
            upload_blob_chunked(client, REPO, io.BytesIO(PAYLOAD), DIGEST, chunk_size=2,
                                total_size=total, progress_callback=events.append)
        else:
            get_blob(client, REPO, DIGEST, chunk_size=2, total_size=total, progress_callback=events.append)
    assert events[-1].phase == TransferPhase.FAILED
    assert all(event.bytes_completed <= total for event in events)
    client.put.assert_not_called()


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("total", [None, 0])
def test_empty_transfer_has_a_complete_lifecycle(upload: bool, total: int | None) -> None:
    """Distinguish a known empty payload from an unknown total without skipping completion."""
    client = _client()
    digest = "sha256:" + hashlib.sha256(b"").hexdigest()
    client.get.return_value = _response()
    client.put.return_value = _response(status=201, headers={"Docker-Content-Digest": digest})
    events: list[TransferProgress] = []
    if upload:
        source = io.BytesIO()
        upload_blob_chunked(client, REPO, source, digest, total_size=total, progress_callback=events.append)
        assert not source.closed
        client.patch.assert_not_called()
        client.put.assert_called_once()
    else:
        get_blob(client, REPO, digest, total_size=total, progress_callback=events.append)
    assert events[0].phase == TransferPhase.PREPARING
    assert events[-1].phase == TransferPhase.COMPLETED
    assert all(event.bytes_completed == 0 and event.total_bytes == total for event in events)


def test_upload_counts_accepted_chunks_and_preserves_caller_source() -> None:
    """Observe accepted chunks before the final commit, from the caller's current offset."""
    client = _client()
    source = io.BytesIO(b"prefix" + PAYLOAD)
    source.seek(6)
    events: list[TransferProgress] = []

    def accept_chunk(path: str, **kwargs: object) -> requests.Response:
        """Validate progress before accepting each buffered chunk."""
        count = len(_counts(events))
        assert path == UPLOAD
        assert kwargs["data"] == PAYLOAD[count * 2:count * 2 + 2]
        assert events[-1].bytes_completed == count * 2
        return _response(status=202)

    def commit(*args: object, **kwargs: object) -> requests.Response:
        """Completion must not be announced while the final request is in flight."""
        assert events[-1].phase == TransferPhase.COMMITTING
        assert _counts(events) == [2, 4, 6]
        return _response(status=201, headers={"Docker-Content-Digest": DIGEST})

    client.patch.side_effect = accept_chunk
    client.put.side_effect = commit
    assert upload_blob_chunked(client, REPO, source, DIGEST, chunk_size=2,
                               total_size=6, progress_callback=events.append) == DIGEST
    assert source.tell() == 12 and not source.closed
    assert events[-2].phase == TransferPhase.VERIFYING
    assert events[-1].phase == TransferPhase.COMPLETED
    assert ("_state", "opaque") in client.put.call_args.kwargs["params"]


@pytest.mark.parametrize("bad_phase", [
    TransferPhase.PREPARING, TransferPhase.TRANSFERRING,
    TransferPhase.VERIFYING, TransferPhase.COMPLETED,
])
@pytest.mark.parametrize("write_file", [False, True])
def test_callback_errors_propagate_without_recursion(
    tmp_path: Path, bad_phase: TransferPhase, write_file: bool,
) -> None:
    """An observer OSError is not mislabeled as file I/O or notified recursively."""
    client = _client()
    error = OSError("observer failure")
    events: list[TransferProgress] = []

    def observe(event: TransferProgress) -> None:
        """Raise the same exception object at the selected notification."""
        events.append(event)
        if event.phase == bad_phase:
            raise error

    with (
        patch.object(client.get.return_value, "close") as close,
        pytest.raises(OSError) as caught,
    ):
        get_blob(client, REPO, DIGEST, str(tmp_path / "blob") if write_file else None,
                 chunk_size=2, progress_callback=observe)
    assert caught.value is error
    assert events[-1].phase == bad_phase
    assert not any(event.phase == TransferPhase.FAILED for event in events)
    assert close.call_count == (0 if bad_phase == TransferPhase.PREPARING else 1)


def test_callback_error_after_written_chunk_closes_file(tmp_path: Path) -> None:
    """After chunk processing, propagate observer errors unchanged and release the file."""
    client = _client()
    output = tmp_path / "blob"
    error = OSError("observer failure after write")

    def observe(event: TransferProgress) -> None:
        """Fail once the first payload chunk has been processed."""
        if event.bytes_completed == 2:
            raise error

    with pytest.raises(OSError) as caught:
        get_blob(client, REPO, DIGEST, str(output), 2, progress_callback=observe)
    assert caught.value is error
    assert output.read_bytes() == b"ab"
    output.unlink()


def test_secondary_observer_error_does_not_mask_primary(caplog: pytest.LogCaptureFixture) -> None:
    """Failure reporting logs only exception type and preserves the original transfer error."""
    client = _client()
    primary = requests.exceptions.Timeout("original transfer error")
    client.get.side_effect = primary
    events: list[TransferProgress] = []

    def observe(event: TransferProgress) -> None:
        """Fail only during secondary failure notification."""
        events.append(event)
        if event.phase == TransferPhase.FAILED:
            raise RuntimeError("credential-like-private-value")

    with pytest.raises(requests.exceptions.Timeout) as caught:
        get_blob(client, REPO, DIGEST, progress_callback=observe)
    assert caught.value is primary
    assert [event.phase for event in events] == [TransferPhase.PREPARING, TransferPhase.FAILED]
    assert "preserving the original transfer error" in caplog.text
    assert "credential-like-private-value" not in caplog.text


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(2), GeneratorExit()])
def test_cancelled_download_closes_response(error: BaseException) -> None:
    """Cancellation gets one terminal event and never turns into successful completion."""
    client = _client()
    response = client.get.return_value
    events: list[TransferProgress] = []
    with (
        patch.object(response, "iter_content", side_effect=error),
        patch.object(response, "close") as close,pytest.raises(type(error))
    ):
        get_blob(client, REPO, DIGEST, progress_callback=events.append)
    close.assert_called_once()
    assert events[-1].phase == TransferPhase.CANCELLED
    assert sum(event.phase == TransferPhase.CANCELLED for event in events) == 1


@pytest.mark.parametrize("upload", [False, True])
def test_digest_failure_is_not_completion(tmp_path: Path, upload: bool) -> None:
    """Both paths notify failure after verification and retain existing integrity behavior."""
    client = _client()
    events: list[TransferProgress] = []
    bad_digest = "sha256:" + "0" * 64
    output = tmp_path / "blob"
    with pytest.raises(BlobError, match="Digest mismatch"):
        if upload:
            client.put.return_value = _response(status=201, headers={"Docker-Content-Digest": bad_digest})
            upload_blob_chunked(client, REPO, io.BytesIO(PAYLOAD), DIGEST, progress_callback=events.append)
        else:
            get_blob(client, REPO, bad_digest, str(output), progress_callback=events.append)
    assert [event.phase for event in events][-2:] == [TransferPhase.VERIFYING, TransferPhase.FAILED]
    assert not output.exists()


def test_cleanup_failure_inside_caller_exception_is_not_silently_ignored() -> None:
    """An unrelated outer except block must not hide a new cleanup failure."""
    client = _client()
    events: list[TransferProgress] = []
    try:
        raise ValueError("unrelated caller exception")
    except ValueError:
        with (
            patch.object(client.get.return_value, "close", side_effect=OSError("close failed")),
            pytest.raises(requests.exceptions.RequestException, match="close the blob"),
        ):
            get_blob(client, REPO, DIGEST, progress_callback=events.append)
    assert events[-1].phase == TransferPhase.FAILED


def test_cleanup_failure_preserves_transfer_error(caplog: pytest.LogCaptureFixture) -> None:
    """Report cleanup errors without replacing an in-flight network exception."""
    client = _client()
    primary = requests.exceptions.ConnectionError("original")
    with (
        patch.object(client.get.return_value, "iter_content", side_effect=primary),
        patch.object(client.get.return_value, "close", side_effect=OSError("cleanup")),
        pytest.raises(requests.exceptions.ConnectionError) as caught,
    ):
        get_blob(client, REPO, DIGEST, progress_callback=lambda event: None)
    assert caught.value is primary
    assert "cleanup failed" in caplog.text


@pytest.mark.parametrize("argument,value", [
    ("chunk_size", 0), ("chunk_size", -1), ("chunk_size", True),
    ("total_size", -1), ("total_size", True), ("total_size", "6"),
    ("progress_callback", 3),
])
@pytest.mark.parametrize("upload", [False, True])
def test_invalid_options_fail_before_io(argument: str, value: object, upload: bool) -> None:
    """Invalid options cannot become empty uploads or unbounded reads."""
    client = _client()
    source = io.BytesIO(PAYLOAD)
    with pytest.raises((BlobError, TypeError)):
        if upload:
            upload_blob_chunked(client, REPO, source, DIGEST, **{argument: value})
        else:
            get_blob(client, REPO, DIGEST, **{argument: value})
    client.get.assert_not_called()
    client.post.assert_not_called()
    assert source.tell() == 0


def test_none_callback_preserves_existing_length_behavior() -> None:
    """Absent observers do not add inferred-header validation to existing callers."""
    client = _client()
    client.get.return_value = _response(PAYLOAD, headers={"Content-Length": "99"})
    assert get_blob(client, REPO, DIGEST).size == 99


@pytest.mark.parametrize("upload", [False, True])
def test_explicit_total_is_checked_without_an_observer(upload: bool) -> None:
    """A newly supplied trusted total remains a validation contract with rendering absent."""
    client = _client()
    with pytest.raises(BlobError, match="byte count"):
        if upload:
            upload_blob_chunked(client, REPO, io.BytesIO(PAYLOAD), DIGEST, total_size=5)
        else:
            get_blob(client, REPO, DIGEST, total_size=5)


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("write_file", [False, True])
def test_real_client_preserves_live_download_streaming(
    tmp_path: Path, middleware: bool, write_file: bool,
) -> None:
    """Both transport modes keep the urllib3 entity lazy through intermediate progress."""
    from regshape.tests.artifact_fixtures import StreamResponse

    response = StreamResponse(PAYLOAD, headers={"Content-Length": "6"})
    events: list[TransferProgress] = []

    def observe(event: TransferProgress) -> None:
        """Check streaming laziness during actual Requests response iteration."""
        events.append(event)
        if event.phase == TransferPhase.TRANSFERRING:
            assert response._content is False
            assert not response._content_consumed

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", return_value=response) as http,
        patch("requests.Session.request", side_effect=http),
        patch.object(response, "close", wraps=response.close) as close,
    ):
        client = RegistryClient(TransportConfig(registry=REGISTRY, enable_middleware=middleware))
        get_blob(client, REPO, DIGEST, str(tmp_path / "blob") if write_file else None,
                 chunk_size=2, progress_callback=observe)
    assert _counts(events) == [2, 4, 6]
    assert http.call_args.kwargs["stream"] is True
    close.assert_called_once()
    assert events[-1].phase == TransferPhase.COMPLETED


def test_events_do_not_expose_request_metadata() -> None:
    """Only an opaque ID, digest, and typed transfer observations enter the callback."""
    client = _client()
    client.config.registry = "user:private-value@registry.example.invalid"
    events: list[TransferProgress] = []
    upload_blob_chunked(client, REPO, io.BytesIO(PAYLOAD), DIGEST, progress_callback=events.append)
    encoded = json.dumps([asdict(event) for event in events])
    assert "private-value" not in encoded and "_state" not in encoded and REGISTRY not in encoded


@pytest.mark.parametrize("upload", [False, True])
def test_invalid_progress_digest_is_rejected_without_exposing_input(upload: bool) -> None:
    """Opt-in identity validation prevents URLs or arbitrary strings entering events."""
    client = _client()
    events: list[TransferProgress] = []
    with pytest.raises(BlobError) as caught:
        if upload:
            upload_blob_chunked(client, REPO, io.BytesIO(), "sha256:private-value",
                                progress_callback=events.append)
        else:
            get_blob(client, REPO, "sha256:private-value", progress_callback=events.append)
    assert "private-value" not in str(caught.value)
    assert events == []
    client.get.assert_not_called()
    client.post.assert_not_called()


def test_download_network_error_is_not_mislabeled_as_file_error(tmp_path: Path) -> None:
    """Streaming to disk must preserve a transport exception and close its resources."""
    client = _client()
    primary = requests.exceptions.Timeout("network timeout")
    events: list[TransferProgress] = []
    with (
        patch.object(client.get.return_value, "iter_content", side_effect=primary),
        patch.object(client.get.return_value, "close") as close,
        pytest.raises(requests.exceptions.Timeout) as caught,
    ):
        get_blob(client, REPO, DIGEST, str(tmp_path / "blob"), progress_callback=events.append)
    assert caught.value is primary
    close.assert_called_once()
    assert events[-1].phase == TransferPhase.FAILED


def test_download_file_failure_is_domain_error(tmp_path: Path) -> None:
    """Keep actual file errors distinct from callback and transport errors."""
    client = _client()
    events: list[TransferProgress] = []
    with pytest.raises(BlobError, match="Cannot write"):
        get_blob(client, REPO, DIGEST, str(tmp_path / "absent" / "blob"), progress_callback=events.append)
    assert events[-1].phase == TransferPhase.FAILED


@pytest.mark.parametrize("failure", ["patch", "read", "commit", "callback", "cancel"])
def test_failed_upload_preserves_source_and_accepted_prefix(failure: str) -> None:
    """A later failure never counts an unaccepted chunk or claims completed upload."""
    client = _client()
    source = io.BytesIO(PAYLOAD)
    events: list[TransferProgress] = []
    primary = RuntimeError("observer failure")
    if failure == "patch":
        client.patch.side_effect = [_response(status=202), _response(status=500)]
    elif failure == "read":
        source = MagicMock(spec=io.BytesIO)
        source.read.side_effect = [b"ab", OSError("source read failure")]
    elif failure == "commit":
        client.put.return_value = _response(status=500)
    elif failure == "cancel":
        client.patch.side_effect = [_response(status=202), KeyboardInterrupt()]

    def observe(event: TransferProgress) -> None:
        """A callback error stops the operation without recursive terminal delivery."""
        events.append(event)
        if failure == "callback" and event.bytes_completed == 2:
            raise primary

    expected = {
        "patch": BlobError, "read": OSError, "commit": BlobError,
        "callback": RuntimeError, "cancel": KeyboardInterrupt,
    }[failure]
    with pytest.raises(expected):
        upload_blob_chunked(client, REPO, source, DIGEST, chunk_size=2, progress_callback=observe)
    if failure == "read":
        source.close.assert_not_called()
    else:
        assert not source.closed
    assert _counts(events) == ([2, 4, 6] if failure == "commit" else [2])
    assert not any(event.phase == TransferPhase.COMPLETED for event in events)
    if failure != "callback":
        assert events[-1].phase == (TransferPhase.CANCELLED if failure == "cancel" else TransferPhase.FAILED)
    if failure != "commit":
        client.put.assert_not_called()


def test_download_ignores_empty_chunks_and_uses_distinct_invocation_ids() -> None:
    """Empty keepalive chunks do not invent byte updates or share per-call state."""
    client = _client()
    first: list[TransferProgress] = []
    second: list[TransferProgress] = []
    with patch.object(client.get.return_value, "iter_content", return_value=iter([b"ab", b"", b"cd", b"ef"])):
        get_blob(client, REPO, DIGEST, progress_callback=first.append)
    client.get.return_value = _response(PAYLOAD)
    get_blob(client, REPO, DIGEST, progress_callback=second.append)
    assert _counts(first) == [2, 4, 6]
    assert first[0].transfer_id != second[0].transfer_id
    assert first[-1].bytes_completed == second[-1].bytes_completed == 6


@pytest.mark.parametrize("middleware,failure", [(False, "auth"), (True, "auth"), (True, "status"), (True, "timeout")])
def test_real_client_retries_do_not_double_count_chunks(middleware: bool, failure: str) -> None:
    """Exercise authentication and retry middleware with actual client dispatch and mocked HTTP."""
    events: list[TransferProgress] = []
    patches: list[bytes] = []

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Fail the first PATCH once, then accept the identical buffered retry."""
        if method == "POST":
            return _response(status=202, headers={"Location": UPLOAD})
        if method == "PATCH":
            data = kwargs["data"]
            assert isinstance(data, bytes)
            patches.append(data)
            if len(patches) == 1:
                assert _counts(events) == []
                if failure == "timeout":
                    raise requests.exceptions.Timeout("synthetic timeout")
                if failure == "status":
                    return _response(status=503)
                return _response(status=401, headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.example.invalid/token",service="registry.example.invalid"',
                })
            assert len(_counts(events)) == len(patches) - 2
            return _response(status=202, headers={"Location": UPLOAD})
        assert method == "PUT"
        assert events[-1].phase == TransferPhase.COMMITTING
        return _response(status=201, headers={"Docker-Content-Digest": DIGEST})

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.auth.registryauth.authenticate", return_value="synthetic-token"),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond) as http,
    ):
        client = RegistryClient(TransportConfig(
            registry=REGISTRY, enable_middleware=middleware,
            enable_retries=failure != "auth",
            retry_config=RetryConfig(max_retries=1, backoff_factor=0),
        ))
        upload_blob_chunked(client, REPO, io.BytesIO(PAYLOAD), DIGEST, chunk_size=2,
                            total_size=6, progress_callback=events.append)
    assert patches == [b"ab", b"ab", b"cd", b"ef"]
    assert _counts(events) == [2, 4, 6]
    assert http.call_count == 6
    assert events[-1].phase == TransferPhase.COMPLETED

"""Replay, byte observation and error contracts for monolithic streamed uploads."""

import hashlib
import io
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
import requests

from regshape.libs.blobs import TransferPhase, TransferProgress, upload_blob_stream
from regshape.libs.decorators import TelemetryConfig, configure_telemetry
from regshape.libs.errors import BlobError
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.body import _UploadBody
from regshape.libs.transport.middleware import RetryConfig

DATA = b"abcdef"
DIGEST = "sha256:" + hashlib.sha256(DATA).hexdigest()
LOCATION = "/v2/repo/blobs/uploads/session?_state=opaque"


def _response(status: int, headers: dict[str, str] | None = None) -> requests.Response:
    """Make buffered HTTP responses without real network access."""
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = b""
    response._content_consumed = True
    return response


class _Nonseekable(io.BytesIO):
    """Expose a readable source whose consumed bytes cannot be replayed."""

    def seekable(self) -> bool:
        """Tell the transport not to attempt a rewind."""
        return False

    def seek(self, offset: int, whence: int = 0) -> int:
        """Fail if code incorrectly tries to seek this source."""
        raise io.UnsupportedOperation("not seekable")


@pytest.mark.parametrize("middleware,failure", [
    (mode, failure) for mode in (False, True) for failure in ("none", "auth", "probe", "redirect")
] + [(True, "timeout"), (True, "status")])
def test_streamed_body_replays_identical_payload(middleware: bool, failure: str) -> None:
    """Exercise dispatch/replay with body consumption before final responses."""
    source = io.BytesIO(b"prefix" + DATA)
    source.seek(6)
    events: list[TransferProgress] = []
    bodies: list[bytes] = []

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Read in bounded chunks and trigger a single replay path."""
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        if method == "GET":
            assert failure == "probe" and url.endswith("/v2/")
            return _response(401, {"WWW-Authenticate": 'Bearer realm="https://auth.example.invalid/token",service="registry"'})
        assert method == "PUT"
        body = kwargs["data"]
        assert kwargs["headers"]["Content-Length"] == "6"
        assert ("_state", "opaque") in kwargs["params"]
        chunks = []
        while chunk := body.read(2):
            chunks.append(chunk)
            assert events[-1].bytes_completed == len(b"".join(chunks))
            assert all(e.phase != TransferPhase.COMPLETED for e in events)
        bodies.append(b"".join(chunks))
        if len(bodies) == 1 and failure != "none":
            if failure == "auth":
                return _response(401, {"WWW-Authenticate": 'Bearer realm="https://auth.example.invalid/token",service="registry"'})
            if failure == "probe":
                return _response(401)
            if failure == "timeout":
                raise requests.exceptions.Timeout("network timeout")
            if failure == "status":
                return _response(503)
            body.seek(0)
            assert b"".join(iter(lambda: body.read(2), b"")) == DATA
        return _response(201, {"Docker-Content-Digest": DIGEST})

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.auth.registryauth.authenticate", return_value="synthetic-token"),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
    ):
        client = RegistryClient(TransportConfig(
            registry="registry.example.invalid", enable_middleware=middleware,
            enable_retries=failure in ("timeout", "status"),
            retry_config=RetryConfig(max_retries=1, backoff_factor=0),
        ))
        assert upload_blob_stream(client, "repo", source, DIGEST, progress_callback=events.append) == DIGEST
    assert bodies == ([DATA] if failure in ("none", "redirect") else [DATA, DATA])
    assert source.tell() == 12 and not source.closed
    assert events[-1].phase == TransferPhase.COMPLETED
    assert events[-1].bytes_completed == 6
    assert events[-1].attempt == (1 if failure == "none" else 2)
    assert sum(e.phase == TransferPhase.RETRYING for e in events) == (failure != "none")
    assert len({e.transfer_id for e in events}) == 1


@pytest.mark.parametrize("middleware", [False, True])
def test_observer_network_exception_is_not_retried(middleware: bool) -> None:
    """Restore a callback error even if the HTTP adapter wraps it as a network failure."""
    error = requests.exceptions.Timeout("observer, not network")
    source = io.BytesIO(DATA)
    events: list[TransferProgress] = []
    puts = 0

    def observe(event: TransferProgress) -> None:
        """Fail from the first intermediate byte observation."""
        events.append(event)
        if event.bytes_completed:
            raise error

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Mimic Requests translating a body-read exception to ConnectionError."""
        nonlocal puts
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        puts += 1
        try:
            kwargs["data"].read(2)
        except requests.exceptions.Timeout as exc:
            raise requests.exceptions.ConnectionError("wrapped") from exc
        raise AssertionError("Observer must fail")

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
        pytest.raises(requests.exceptions.Timeout) as caught,
    ):
        client = RegistryClient(TransportConfig(
            registry="registry.example.invalid", enable_middleware=middleware,
            enable_retries=True, retry_config=RetryConfig(max_retries=2, backoff_factor=0),
        ))
        upload_blob_stream(client, "repo", source, DIGEST, progress_callback=observe)
    assert caught.value is error and puts == 1 and not source.closed
    assert not any(e.phase in (TransferPhase.COMPLETED, TransferPhase.FAILED) for e in events)


@pytest.mark.parametrize("failure", ["auth", "status"])
def test_consumed_nonseekable_source_is_not_replayed(failure: str) -> None:
    """Fail before a second dispatch instead of sending a truncated/empty retry."""
    source = _Nonseekable(DATA)
    events: list[TransferProgress] = []
    puts = 0

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Consume some source bytes, then ask for replay."""
        nonlocal puts
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        puts += 1
        assert kwargs["data"].read(2) == b"ab"
        if failure == "auth":
            return _response(401, {"WWW-Authenticate": 'Bearer realm="https://auth.example.invalid/token",service="registry"'})
        return _response(503)

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.auth.registryauth.authenticate", return_value="synthetic-token"),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
        pytest.raises(BlobError, match="nonseekable"),
    ):
        client = RegistryClient(TransportConfig(
            registry="registry.example.invalid", enable_retries=True,
            retry_config=RetryConfig(max_retries=1, backoff_factor=0),
        ))
        upload_blob_stream(client, "repo", source, DIGEST, total_size=6, progress_callback=events.append)
    assert puts == 1 and not source.closed and events[-1].phase == TransferPhase.FAILED


@pytest.mark.parametrize("payload,total", [(b"", 0), (DATA, 6), (DATA, 3), (DATA, 9), (DATA, None)])
def test_nonseekable_size_contract(payload: bytes, total: int | None) -> None:
    """Use a trusted frame length without consuming trailing nonseekable data."""
    source = _Nonseekable(payload)
    events: list[TransferProgress] = []
    framed = payload[:total] if total is not None else payload
    digest = "sha256:" + hashlib.sha256(framed).hexdigest()

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Consume the exact body supplied to the simulated HTTP transport."""
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        assert b"".join(iter(lambda: kwargs["data"].read(2), b"")) == framed
        return _response(201, {"Docker-Content-Digest": digest})

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example.invalid"))
        if total is not None and total <= len(payload):
            upload_blob_stream(client, "repo", source, digest, total_size=total, progress_callback=events.append)
            assert events[-1].phase == TransferPhase.COMPLETED
            assert source.read() == payload[total:]
        else:
            with pytest.raises(BlobError):
                upload_blob_stream(client, "repo", source, digest, total_size=total, progress_callback=events.append)
            assert events[-1].phase == TransferPhase.FAILED
    assert not source.closed


def test_pipe_payload_does_not_wait_for_writer_eof() -> None:
    """A producer may keep its pipe open until the framed upload has completed."""
    reader, writer = os.pipe()
    source = os.fdopen(reader, "rb", buffering=0)
    producer = os.fdopen(writer, "wb", buffering=0)
    producer.write(DATA)

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Consume exactly the framed body without consulting the still-open writer."""
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        assert b"".join(iter(lambda: kwargs["data"].read(2), b"")) == DATA
        return _response(201, {"Docker-Content-Digest": DIGEST})

    try:
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            client = RegistryClient(TransportConfig(registry="registry.example.invalid"))
            future = pool.submit(upload_blob_stream, client, "repo", source, DIGEST, total_size=6)
            try:
                assert future.result(timeout=2) == DIGEST
                assert not source.closed
            finally:
                producer.close()
    finally:
        producer.close()
        source.close()


@pytest.mark.parametrize("middleware", [False, True])
def test_stream_digest_mismatch_closes_wrapper_not_source(middleware: bool) -> None:
    """A consumed upload with the wrong confirmed digest fails exactly once."""
    source = io.BytesIO(DATA)
    events: list[TransferProgress] = []
    bodies: list[_UploadBody] = []

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Consume the complete body and return an inconsistent digest."""
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        body = kwargs["data"]
        assert isinstance(body, _UploadBody)
        bodies.append(body)
        assert b"".join(iter(lambda: body.read(2), b"")) == DATA
        return _response(201, {"Docker-Content-Digest": "sha256:" + "0" * 64})

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
        pytest.raises(BlobError, match="different digest"),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example.invalid", enable_middleware=middleware))
        upload_blob_stream(client, "repo", source, DIGEST, progress_callback=events.append)
    assert [e.phase for e in events][-2:] == [TransferPhase.VERIFYING, TransferPhase.FAILED]
    assert sum(e.phase == TransferPhase.FAILED for e in events) == 1
    assert not any(e.phase == TransferPhase.COMPLETED for e in events)
    assert bodies[0].closed and not source.closed


@pytest.mark.parametrize("middleware", [False, True])
def test_source_error_is_restored_without_replay(middleware: bool) -> None:
    """A wrapped partial source-read error stays primary and emits a single failure."""
    error = OSError("source failure")
    events: list[TransferProgress] = []
    bodies: list[_UploadBody] = []

    class BrokenSource(io.BytesIO):
        """Fail after one partial payload while retaining caller ownership."""

        def read(self, size: int = -1) -> bytes:
            """Raise the original file error on the second client read."""
            if self.tell() >= 2:
                raise error
            return super().read(size)

    source = BrokenSource(DATA)

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Mimic an adapter wrapping a read failure as a retriable connection error."""
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        body = kwargs["data"]
        assert isinstance(body, _UploadBody)
        bodies.append(body)
        try:
            b"".join(iter(lambda: body.read(2), b""))
        except OSError as exc:
            raise requests.exceptions.ConnectionError("wrapped source error") from exc
        raise AssertionError("Source must fail")

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
        pytest.raises(OSError) as caught,
    ):
        client = RegistryClient(TransportConfig(
            registry="registry.example.invalid", enable_middleware=middleware, enable_retries=True,
            retry_config=RetryConfig(max_retries=2, backoff_factor=0, exceptions=(OSError,)),
        ))
        upload_blob_stream(client, "repo", source, DIGEST, progress_callback=events.append)
    assert caught.value is error and len(bodies) == 1
    assert bodies[0].closed and not source.closed
    assert events[-1].phase == TransferPhase.FAILED and events[-1].bytes_completed == 2
    assert sum(e.phase == TransferPhase.FAILED for e in events) == 1


def test_real_requests_adapter_receives_stream_and_updates_metrics() -> None:
    """Exercise real Requests body preparation rather than substituting requests.request."""
    events: list[TransferProgress] = []
    config = TelemetryConfig(metrics_enabled=True, output=io.StringIO())
    configure_telemetry(config)
    bodies: list[_UploadBody] = []

    class BoundedSource(io.BytesIO):
        """Reject payload pre-buffering or reads larger than this adapter requests."""

        def read(self, size: int = -1) -> bytes:
            """Permit only bounded reads, including the one-byte EOF check."""
            assert 0 <= size <= 2
            return super().read(size)

    source = BoundedSource(DATA)

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Consume a prepared counted body with explicit Content-Length, not chunked encoding."""
        if request.method == "POST":
            response = _response(202, {"Location": LOCATION})
        else:
            assert request.headers["Content-Length"] == "6"
            assert "Transfer-Encoding" not in request.headers
            assert isinstance(request.body, _UploadBody)
            bodies.append(request.body)
            assert b"".join(iter(lambda: request.body.read(2), b"")) == DATA
            response = _response(201, {"Docker-Content-Digest": DIGEST})
        response.request = request
        response.url = request.url
        return response

    try:
        with (
            patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
            patch("requests.adapters.HTTPAdapter.send", side_effect=send),
        ):
            client = RegistryClient(TransportConfig(registry="registry.example.invalid"))
            upload_blob_stream(client, "repo", source, DIGEST, progress_callback=events.append)
        assert config.metrics.total_bytes_sent == 6
        assert bodies[0].closed and not source.closed
        assert [e.bytes_completed for e in events if e.phase == TransferPhase.TRANSFERRING] == [0, 2, 4, 6]
    finally:
        configure_telemetry(TelemetryConfig())


@pytest.mark.parametrize("middleware", [False, True])
def test_real_requests_redirect_rewinds_body(middleware: bool) -> None:
    """Exercise Requests redirect preparation and seek rather than simulating a replay call."""
    events: list[TransferProgress] = []
    bodies: list[bytes] = []

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Return one same-origin 307 after consuming the original body."""
        if request.method == "POST":
            response = _response(202, {"Location": LOCATION})
        else:
            bodies.append(b"".join(iter(lambda: request.body.read(2), b"")))
            response = (
                _response(307, {"Location": "/redirected"})
                if len(bodies) == 1
                else _response(201, {"Docker-Content-Digest": DIGEST})
            )
        response.request = request
        response.url = request.url
        return response

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.adapters.HTTPAdapter.send", side_effect=send),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example.invalid", enable_middleware=middleware))
        upload_blob_stream(client, "repo", io.BytesIO(DATA), DIGEST, progress_callback=events.append)
    assert bodies == [DATA, DATA]
    assert events[-1].attempt == 2 and events[-1].bytes_completed == 6


def test_partial_network_failure_rewinds_without_accumulating() -> None:
    """A partial first attempt is replaced, not added to the successful payload count."""
    events: list[TransferProgress] = []
    puts = 0
    source = io.BytesIO(b"prefix" + DATA)
    source.seek(6)

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Break the connection after two bytes on the first attempt."""
        nonlocal puts
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        puts += 1
        body = kwargs["data"]
        if puts == 1:
            assert body.read(2) == b"ab"
            raise requests.exceptions.ConnectionError("partial network failure")
        assert b"".join(iter(lambda: body.read(2), b"")) == DATA
        return _response(201, {"Docker-Content-Digest": DIGEST})

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
    ):
        client = RegistryClient(TransportConfig(
            registry="registry.example.invalid", enable_retries=True,
            retry_config=RetryConfig(max_retries=1, backoff_factor=0),
        ))
        upload_blob_stream(client, "repo", source, DIGEST, progress_callback=events.append)
    assert puts == 2 and source.tell() == 12 and not source.closed
    assert [(e.phase, e.bytes_completed) for e in events if e.phase == TransferPhase.RETRYING] == [(TransferPhase.RETRYING, 0)]
    assert events[-1].bytes_completed == 6 and events[-1].attempt == 2


def test_seekable_length_mismatch_fails_before_network() -> None:
    """Inspecting length restores the caller offset and cannot initiate a bad upload."""
    source = io.BytesIO(b"prefix" + DATA)
    source.seek(6)
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request") as http,
        pytest.raises(BlobError, match="remaining source"),
    ):
        upload_blob_stream(RegistryClient(TransportConfig(registry="registry.example.invalid")),
                           "repo", source, DIGEST, total_size=5)
    http.assert_not_called()
    assert source.tell() == 6 and not source.closed


def test_exhausted_retries_never_complete() -> None:
    """A fully read body still fails when the registry never accepts its commit."""
    events: list[TransferProgress] = []
    puts = 0

    def respond(method: str, url: str, **kwargs: object) -> requests.Response:
        """Consume every attempt then reject the commit."""
        nonlocal puts
        if method == "POST":
            return _response(202, {"Location": LOCATION})
        puts += 1
        assert b"".join(iter(lambda: kwargs["data"].read(2), b"")) == DATA
        return _response(503)

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=respond),
        pytest.raises(BlobError, match="Registry error"),
    ):
        client = RegistryClient(TransportConfig(
            registry="registry.example.invalid", enable_retries=True,
            retry_config=RetryConfig(max_retries=1, backoff_factor=0),
        ))
        upload_blob_stream(client, "repo", io.BytesIO(DATA), DIGEST, progress_callback=events.append)
    assert puts == 2 and events[-1].phase == TransferPhase.FAILED
    assert not any(e.phase == TransferPhase.COMPLETED for e in events)


@pytest.mark.parametrize("status", [302, 303])
@pytest.mark.parametrize("middleware", [False, True])
def test_method_changing_redirect_cannot_confirm_upload(status: int, middleware: bool) -> None:
    """A redirected GET response is not evidence that the blob PUT was committed."""
    events: list[TransferProgress] = []

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Return a successful GET after Requests changes the upload method."""
        if request.method == "POST":
            response = _response(202, {"Location": LOCATION})
        elif request.method == "PUT":
            assert b"".join(iter(lambda: request.body.read(2), b"")) == DATA
            response = _response(status, {"Location": "/redirected"})
        else:
            assert request.method == "GET"
            response = _response(200, {"Docker-Content-Digest": DIGEST})
        response.request = request
        response.url = request.url
        return response

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.adapters.HTTPAdapter.send", side_effect=send),
        pytest.raises(BlobError, match="completing PUT"),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example.invalid", enable_middleware=middleware))
        upload_blob_stream(client, "repo", io.BytesIO(DATA), DIGEST, progress_callback=events.append)
    assert events[-1].phase == TransferPhase.FAILED
    assert not any(e.phase == TransferPhase.COMPLETED for e in events)


@pytest.mark.parametrize("middleware", [False, True])
def test_body_discarding_301_cannot_confirm_upload(middleware: bool) -> None:
    """Requests can preserve PUT while dropping its body; no digest fallback may hide that."""
    events: list[TransferProgress] = []
    puts = 0
    source = io.BytesIO(DATA)

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Return a successful bodyless PUT after the original stream was consumed."""
        nonlocal puts
        if request.method == "POST":
            response = _response(202, {"Location": LOCATION})
        else:
            assert request.method == "PUT"
            puts += 1
            if puts == 1:
                assert b"".join(iter(lambda: request.body.read(2), b"")) == DATA
                response = _response(301, {"Location": "/redirected"})
            else:
                assert request.body is None
                response = _response(201)
        response.request = request
        response.url = request.url
        return response

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.adapters.HTTPAdapter.send", side_effect=send),
        pytest.raises(BlobError, match="counted request body"),
    ):
        client = RegistryClient(TransportConfig(registry="registry.example.invalid", enable_middleware=middleware))
        upload_blob_stream(client, "repo", source, DIGEST, progress_callback=events.append)
    assert puts == 2 and not source.closed
    assert events[-1].phase == TransferPhase.FAILED
    assert not any(e.phase == TransferPhase.COMPLETED for e in events)

"""Manifest observations and stream ownership through real clients with synthetic HTTP."""

import gzip
import hashlib
import io
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, asdict, dataclass, replace
from unittest.mock import Mock, patch

import pytest
import requests
from urllib3.response import HTTPResponse

from regshape.libs.blobs import get_blob
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.manifests import (
    ManifestKind,
    ManifestPhase,
    ManifestProgress,
    get_manifest,
)
from regshape.libs.models.mediatype import (
    DOCKER_MANIFEST_LIST_V2,
    DOCKER_MANIFEST_V2,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_MANIFEST,
)
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.middleware import (
    BaseMiddleware,
    CachingMiddleware,
    RetryConfig,
)
from regshape.libs.transport.models import RegistryRequest, RegistryResponse

REGISTRY = "registry.example.invalid"
REPO = "team/artifact"
ACCEPT = OCI_IMAGE_MANIFEST


class StreamResponse(requests.Response):
    """Unbuffered synthetic response with observable content reads and explicit close calls."""

    def __init__(
        self,
        payload: bytes = b"{}",
        status: int = 200,
        headers: dict[str, str] | None = None,
        encoding: str | None = "utf-8",
        raw_headers: dict[str, str] | None = None,
    ) -> None:
        """Model the registry issuer while separating advertised metadata from raw framing."""
        super().__init__()
        self.status_code = status
        self.url = f"https://{REGISTRY}/v2/"
        self.headers.update(headers or {})
        self.encoding = encoding
        self.raw = HTTPResponse(
            body=io.BytesIO(payload), headers=raw_headers, preload_content=False
        )
        self.close_calls = 0
        self.content_reads = 0

    @property
    def content(self) -> bytes:
        """Count eager-body access so observed downloads cannot hide prebuffering."""
        self.content_reads += 1
        return super().content

    def close(self) -> None:
        """Record ownership release while retaining Requests' actual cleanup behavior."""
        self.close_calls += 1
        super().close()


@pytest.fixture(autouse=True)
def isolated_telemetry() -> Iterator[None]:
    """Restore caller telemetry after every test, including injected diagnostic failures."""
    previous = get_telemetry_config()
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(previous)


def client(
    *,
    middleware: bool = True,
    caching: bool = False,
    retries: bool = False,
) -> RegistryClient:
    """Create a real client with explicit synthetic credentials and no credential-store reads."""
    return RegistryClient(
        TransportConfig(
            registry=REGISTRY,
            username="synthetic-user",
            password="synthetic-password",
            enable_middleware=middleware,
            enable_caching=caching,
            enable_retries=retries,
            retry_config=RetryConfig(max_retries=1, backoff_factor=0),
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"transfer_id": ""},
        {"transfer_id": None},
        {"kind": "manifest"},
        {"phase": "received"},
        {"bytes_completed": -1},
        {"bytes_completed": True},
        {"bytes_completed": None, "total_bytes": None},
        {"bytes_completed": 1.5},
        {"total_bytes": True},
        {"total_bytes": -1},
        {"total_bytes": 1.5},
        {"bytes_completed": 3},
    ],
)
def test_event_rejects_invalid_values(changes: dict[str, object]) -> None:
    """Invalid identity, enum types, booleans and bounds all raise the declared ValueError."""
    event = ManifestProgress(
        "opaque", ManifestKind.UNKNOWN, ManifestPhase.PREPARING, 0, 2
    )
    with pytest.raises(ValueError):
        replace(event, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("reference", ["v1", "sha256:" + "a" * 64])
def test_incremental_consumption_and_receipt(middleware: bool, reference: str) -> None:
    """Four actual 64 KiB reads are observable before EOF, decoding and cleanup."""
    payload = b"x" * (4 * 65536)
    response = StreamResponse(
        payload,
        headers={
            "Content-Length": str(len(payload)),
            "Content-Type": OCI_IMAGE_INDEX,
        },
    )
    events: list[ManifestProgress] = []

    def observe(event: ManifestProgress) -> None:
        """Assert the stream has not been eagerly buffered and completion follows closure."""
        assert response.content_reads == 0
        if event.phase == ManifestPhase.RECEIVED:
            assert response.close_calls == 1
        else:
            assert response.close_calls == 0
        events.append(event)

    with patch("requests.request", return_value=response) as send:
        result = get_manifest(
            client(middleware=middleware),
            REPO,
            reference,
            ACCEPT,
            progress_callback=observe,
        )
    assert result == (payload.decode(), OCI_IMAGE_INDEX, "")
    assert [e.bytes_completed for e in events] == [
        0,
        0,
        65536,
        131072,
        196608,
        262144,
        262144,
    ]
    assert events[0].kind == ManifestKind.UNKNOWN
    assert all(e.kind == ManifestKind.INDEX for e in events[1:])
    assert events[-1].phase == ManifestPhase.RECEIVED
    assert len({e.transfer_id for e in events}) == 1
    assert set(asdict(events[-1])) == {
        "transfer_id",
        "kind",
        "phase",
        "bytes_completed",
        "total_bytes",
    }
    with pytest.raises(FrozenInstanceError):
        events[0].bytes_completed = 1  # type: ignore[misc]
    send.assert_called_once()
    assert send.call_args.args[0:2] == (
        "GET",
        f"https://{REGISTRY}/v2/{REPO}/manifests/{reference}",
    )
    assert send.call_args.kwargs["headers"] == {"Accept": ACCEPT}
    assert send.call_args.kwargs["stream"] is True


@pytest.mark.parametrize(
    "media_type,kind",
    [
        (OCI_IMAGE_MANIFEST, ManifestKind.MANIFEST),
        (DOCKER_MANIFEST_V2, ManifestKind.MANIFEST),
        (OCI_IMAGE_INDEX, ManifestKind.INDEX),
        (DOCKER_MANIFEST_LIST_V2.upper() + "; charset=utf-8", ManifestKind.INDEX),
        ("application/vnd.example.artifact", ManifestKind.UNKNOWN),
        ("", ManifestKind.UNKNOWN),
    ],
)
def test_advertised_kind_without_parsing(media_type: str, kind: ManifestKind) -> None:
    """Raw non-image/non-JSON text and missing/malformed digest headers remain acceptable."""
    response = StreamResponse(
        b"not JSON",
        headers={"Content-Type": media_type, "Docker-Content-Digest": "reported"},
    )
    events: list[ManifestProgress] = []
    with patch("requests.request", return_value=response):
        assert get_manifest(
            client(), REPO, "tag", ACCEPT, progress_callback=events.append
        ) == (
            "not JSON",
            media_type,
            "reported",
        )
    assert events[-1].kind == kind


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b'{"schemaVersion":2}',
        '{"value":"caf\u00e9"}'.encode(),
        '{"value":"caf\u00e9"}'.encode("utf-16"),
        b"\xff\xfe\x00invalid",
    ],
)
@pytest.mark.parametrize(
    "encoding", [None, "utf-8", "iso-8859-1", "utf-16", "not-an-encoding"]
)
def test_decoding_matches_buffered_requests(
    payload: bytes, encoding: str | None
) -> None:
    """Observed decoding matches the existing Requests fallback for all 25 design cases."""
    buffered = StreamResponse(payload, encoding=encoding)
    streamed = StreamResponse(payload, encoding=encoding)
    events: list[ManifestProgress] = []
    with patch("requests.request", side_effect=[buffered, streamed]):
        normal = get_manifest(client(), REPO, "v1", ACCEPT)
        observed = get_manifest(
            client(), REPO, "v1", ACCEPT, progress_callback=events.append
        )
    assert normal == observed
    assert buffered.content_reads > 0 and streamed.content_reads == 0
    assert buffered.close_calls == streamed.close_calls == 1
    assert events[-1].bytes_completed == len(payload)


@pytest.mark.parametrize(
    "headers,total",
    [
        ({}, None),
        ({"Content-Length": "3"}, 3),
        ({"Content-Length": " 3 "}, 3),
        ({"Content-Length": "-1"}, None),
        ({"Content-Length": "bad"}, None),
        ({"Content-Length": "\u0663"}, None),
        ({"Content-Length": "3", "Content-Encoding": "gzip"}, None),
        ({"Content-Length": "3", "Transfer-Encoding": "chunked"}, None),
        ({"Content-Length": "3", "Content-Encoding": "IDENTITY"}, 3),
    ],
)
def test_comparable_totals_only(headers: dict[str, str], total: int | None) -> None:
    """Noncomparable or invalid sizes remain unknown without inventing a zero total."""
    events: list[ManifestProgress] = []
    with patch(
        "requests.request", return_value=StreamResponse(b"abc", headers=headers)
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)
    assert events[-1].total_bytes == total
    assert events[-1].bytes_completed == 3


def test_compressed_payload_counts_decoded_bytes() -> None:
    """Real urllib3 decompression is observed in payload-byte units, not wire-size units."""
    payload = b"a" * 100000
    compressed = gzip.compress(payload)
    response = StreamResponse(
        compressed,
        headers={
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
        },
        raw_headers={"Content-Encoding": "gzip"},
    )
    events: list[ManifestProgress] = []
    with patch("requests.request", return_value=response):
        assert (
            get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)[
                0
            ]
            == payload.decode()
        )
    assert events[-1].bytes_completed == len(payload)
    assert all(e.total_bytes is None for e in events)


@pytest.mark.parametrize("hint", ["0", "1", "10"])
def test_inconsistent_total_warns_once(
    hint: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Successful EOF remains usable; mismatches invalidate the hint before impossible events."""
    events: list[ManifestProgress] = []
    response = StreamResponse(b"abc", headers={"Content-Length": hint})
    with patch("requests.request", return_value=response):
        assert (
            get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)[
                0
            ]
            == "abc"
        )
    assert events[-1].total_bytes is None
    assert all(
        e.total_bytes is None or e.bytes_completed <= e.total_bytes for e in events
    )
    assert (
        sum("progress total is unknown" in record.message for record in caplog.records)
        == 1
    )


def test_empty_and_keepalive_chunks() -> None:
    """Ignore empty iterator chunks and receive a genuinely empty zero-length body."""
    response = StreamResponse(b"", headers={"Content-Length": "0"})
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", return_value=iter([b"", b""])),
    ):
        assert get_manifest(
            client(), REPO, "v1", ACCEPT, progress_callback=events.append
        ) == ("", "", "")
    assert [e.phase for e in events] == [
        ManifestPhase.PREPARING,
        ManifestPhase.TRANSFERRING,
        ManifestPhase.RECEIVED,
    ]
    assert events[-1].total_bytes == events[-1].bytes_completed == 0


@pytest.mark.parametrize("phase", list(ManifestPhase)[:3])
def test_callback_failure_is_not_recursively_notified(phase: ManifestPhase) -> None:
    """Preparation aborts before I/O; later observer failures close once and preserve identity."""
    response = StreamResponse()
    error = RuntimeError("observer failure")
    events: list[ManifestProgress] = []

    def observe(event: ManifestProgress) -> None:
        """Raise the same caller-owned exception at the selected boundary."""
        events.append(event)
        if event.phase == phase:
            raise error

    with (
        patch("requests.request", return_value=response) as send,
        pytest.raises(RuntimeError) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=observe)
    assert caught.value is error and events[-1].phase == phase
    assert response.close_calls == (0 if phase == ManifestPhase.PREPARING else 1)
    assert send.call_count == (0 if phase == ManifestPhase.PREPARING else 1)


def test_invalid_callback_fails_before_network() -> None:
    """Reject invalid opt-in values without touching the transport."""
    with patch("requests.request") as send, pytest.raises(TypeError):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=object())  # type: ignore[arg-type]
    send.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ChunkedEncodingError("framing"),
        KeyboardInterrupt(),
        SystemExit(4),
    ],
)
def test_stream_failure_and_cancellation(error: BaseException) -> None:
    """A failed iterator never emits RECEIVED and closes before failure notification."""
    response = StreamResponse()
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=error),
        pytest.raises(type(error)) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)
    assert caught.value is error and response.close_calls == 1
    expected = (
        ManifestPhase.FAILED
        if isinstance(error, Exception)
        else ManifestPhase.CANCELLED
    )
    assert events[-1].phase == expected


def test_secondary_failures_preserve_primary_even_when_logging_fails() -> None:
    """Cleanup, failure-observer and diagnostic errors cannot replace the original stream error."""
    primary = requests.exceptions.ConnectionError("primary")
    cleanup = OSError("cleanup")
    diagnostic = RuntimeError("diagnostic")
    response = StreamResponse()

    def observe(event: ManifestProgress) -> None:
        """Fail only when reporting the already-active transfer failure."""
        if event.phase == ManifestPhase.FAILED:
            raise ValueError("observer")

    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=primary),
        patch.object(response, "close", side_effect=cleanup),
        patch("regshape.libs._http.logger.error", side_effect=diagnostic),
        pytest.raises(requests.exceptions.ConnectionError) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=observe)
    assert caught.value is primary
    assert primary.__context__ is diagnostic
    response.close()


def test_sole_close_failure_prevents_received() -> None:
    """Even a completely consumed payload is not RECEIVED until owned cleanup succeeds."""
    response = StreamResponse()
    events: list[ManifestProgress] = []
    error = OSError("close")
    with (
        patch("requests.request", return_value=response),
        patch.object(response, "close", side_effect=error),
        pytest.raises(OSError) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)
    assert caught.value is error and events[-1].phase == ManifestPhase.FAILED
    response.close()


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("status", [404, 500])
def test_http_errors_close_response(observed: bool, status: int) -> None:
    """Both buffered and observed calls retain existing HTTP error mapping and cleanup."""
    response = StreamResponse(
        b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"missing"}]}', status
    )
    events: list[ManifestProgress] = []
    with patch("requests.request", return_value=response), pytest.raises(ManifestError):
        get_manifest(
            client(),
            REPO,
            "v1",
            ACCEPT,
            progress_callback=events.append if observed else None,
        )
    assert response.close_calls == 1
    assert not events or events[-1].phase == ManifestPhase.FAILED


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("auth_fails", [False, True])
@pytest.mark.parametrize("probe", [False, True])
def test_auth_retries_close_discarded_stream(
    middleware: bool,
    auth_fails: bool,
    probe: bool,
) -> None:
    """Close the initial 401 before probing/token exchange in both transport modes."""
    challenge = {
        "WWW-Authenticate": 'Bearer realm="https://registry.example.invalid/token",scope="repository:team/artifact:pull"'
    }
    denied = StreamResponse(b"denied", 401, headers={} if probe else challenge)
    probe_response = StreamResponse(b"", 401, headers=challenge)
    accepted = StreamResponse()
    error = AuthError("synthetic authentication failure")

    def authenticate(*args: object) -> str:
        """Assert ownership ended before token exchange, including failing exchanges."""
        assert denied.close_calls == 1
        if auth_fails:
            raise error
        return "synthetic-token"

    responses = [denied] + ([probe_response] if probe else []) + [accepted]
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", side_effect=responses) as send,
        patch("regshape.libs.auth.registryauth.authenticate", side_effect=authenticate),
    ):
        if auth_fails:
            with pytest.raises(AuthError) as caught:
                get_manifest(
                    client(middleware=middleware),
                    REPO,
                    "v1",
                    ACCEPT,
                    progress_callback=events.append,
                )
            assert caught.value is error
        else:
            get_manifest(
                client(middleware=middleware),
                REPO,
                "v1",
                ACCEPT,
                progress_callback=events.append,
            )
            assert accepted.close_calls == 1
        assert send.call_count == 1 + int(probe) + int(not auth_fails)
    assert denied.close_calls == 1
    assert len({e.transfer_id for e in events}) == 1
    assert events[-1].phase == (
        ManifestPhase.FAILED if auth_fails else ManifestPhase.RECEIVED
    )
    probe_response.close()
    accepted.close()


def test_cache_stream_bypass_with_warm_buffered_entry() -> None:
    """Observed calls bypass both cache directions while buffered requests remain reusable."""
    responses = [StreamResponse() for _ in range(3)]
    transport = client(caching=True)
    events: list[ManifestProgress] = []
    with patch("requests.request", side_effect=responses) as send:
        assert get_manifest(transport, REPO, "v1", ACCEPT)[0] == "{}"
        for _ in range(2):
            assert (
                get_manifest(
                    transport, REPO, "v1", ACCEPT, progress_callback=events.append
                )[0]
                == "{}"
            )
        assert get_manifest(transport, REPO, "v1", ACCEPT)[0] == "{}"
    assert send.call_count == 3
    assert [r.close_calls for r in responses] == [2, 1, 1]
    assert len({e.transfer_id for e in events}) == 2


def test_cache_never_inserts_an_unbuffered_response() -> None:
    """A downstream middleware cannot smuggle a live response into a buffered cache path."""
    cache = CachingMiddleware()
    request = RegistryRequest("GET", "/v2/repo/manifests/v1", {})
    response = StreamResponse()
    handler = Mock(
        return_value=RegistryResponse.from_requests_response(response, stream=True)
    )
    cache(request, handler)
    cache(request, handler)
    assert handler.call_count == 2 and cache.get_cache_size() == 0
    response.close()


@pytest.mark.parametrize("final_status", [200, 503])
def test_retry_discard_closes_before_backoff(final_status: int) -> None:
    """Discarded attempts close before waiting; the final response belongs to the operation."""
    discarded = StreamResponse(b"busy", 503)
    final = StreamResponse(b"{}", final_status)
    events: list[ManifestProgress] = []

    def wait(delay: float) -> None:
        """Assert closure before any configured delay or possible interruption."""
        assert discarded.close_calls == 1 and final.close_calls == 0

    with (
        patch("requests.request", side_effect=[discarded, final]) as send,
        patch("regshape.libs.transport.middleware.time.sleep", side_effect=wait),
    ):
        if final_status == 200:
            get_manifest(
                client(retries=True),
                REPO,
                "v1",
                ACCEPT,
                progress_callback=events.append,
            )
        else:
            with pytest.raises(ManifestError):
                get_manifest(
                    client(retries=True),
                    REPO,
                    "v1",
                    ACCEPT,
                    progress_callback=events.append,
                )
    assert send.call_count == 2 and discarded.close_calls == final.close_calls == 1
    assert len({e.transfer_id for e in events}) == 1


def test_retry_backoff_cancellation_keeps_discard_closed() -> None:
    """Interrupting backoff cannot strand the retry-discarded response."""
    response = StreamResponse(b"busy", 503)
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response) as send,
        patch(
            "regshape.libs.transport.middleware.time.sleep",
            side_effect=KeyboardInterrupt,
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        get_manifest(
            client(retries=True), REPO, "v1", ACCEPT, progress_callback=events.append
        )
    assert response.close_calls == 1 and send.call_count == 1
    assert events[-1].phase == ManifestPhase.CANCELLED


def test_retry_close_failure_is_not_retried() -> None:
    """Even a normally retryable exception raised by close must stop the retry loop."""
    response = StreamResponse(b"busy", 503)
    failure = requests.exceptions.ConnectionError("cleanup failed")
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response) as send,
        patch.object(response, "close", side_effect=failure),
        pytest.raises(requests.exceptions.ConnectionError) as caught,
    ):
        get_manifest(
            client(retries=True), REPO, "v1", ACCEPT, progress_callback=events.append
        )
    assert caught.value is failure and send.call_count == 1
    assert events[-1].phase == ManifestPhase.FAILED
    response.close()


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("channel", ["stderr", "log_file", "metrics"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_pre_handoff_diagnostics_close_stream(
    middleware: bool,
    channel: str,
    close_fails: bool,
) -> None:
    """Post-response diagnostics retain ownership until handoff, even if cleanup also fails."""
    response = StreamResponse()
    config = TelemetryConfig(
        debug_calls_enabled=channel != "metrics",
        metrics_enabled=channel == "metrics",
        output=io.StringIO(),
    )
    failure = OSError("diagnostic failure")
    output = Mock(wraps=io.StringIO())
    output.write.side_effect = failure
    if channel == "stderr":
        config.output = output
    elif channel == "log_file":
        config.log_file = output
    configure_telemetry(config)
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response),
        patch.object(config.metrics, "record_request", side_effect=failure)
        if channel == "metrics"
        else patch.object(response, "reason", "OK"),
        patch.object(
            response,
            "close",
            side_effect=RuntimeError("cleanup") if close_fails else None,
            wraps=None if close_fails else response.close,
        ) as close,
        pytest.raises(OSError) as caught,
    ):
        get_manifest(
            client(middleware=middleware),
            REPO,
            "v1",
            ACCEPT,
            progress_callback=events.append,
        )
    assert caught.value is failure and close.call_count == 1
    assert response.content_reads == 0 and events[-1].phase == ManifestPhase.FAILED
    response.close()


def test_failed_response_hook_closes_unreturned_stream() -> None:
    """The base middleware releases an acquired stream when its response hook raises."""
    failure = RuntimeError("response hook")

    class BrokenHook(BaseMiddleware):
        """Synthetic middleware that fails after a downstream response is acquired."""

        def process_response(
            self, request: RegistryRequest, response: RegistryResponse
        ) -> RegistryResponse:
            """Raise the original hook error before the response reaches its caller."""
            raise failure

    response = StreamResponse()
    request = RegistryRequest("GET", "/v2/repo/manifests/v1", {}, stream=True)
    with pytest.raises(RuntimeError) as caught:
        BrokenHook()(
            request,
            Mock(
                return_value=RegistryResponse.from_requests_response(
                    response, stream=True
                )
            ),
        )
    assert caught.value is failure and response.close_calls == 1


def test_blob_downloads_share_stream_cache_and_retry_safety() -> None:
    """Existing blob verification/counting survives the shared response-lifetime changes."""
    payload = b"verified blob"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    failed = StreamResponse(b"busy", 503)
    headers = {"Content-Length": str(len(payload))}
    first, second = (
        StreamResponse(payload, headers=headers),
        StreamResponse(payload, headers=headers),
    )
    transport = client(caching=True, retries=True)
    with patch("requests.request", side_effect=[failed, first, second]) as send:
        for _ in range(2):
            result = get_blob(transport, REPO, digest)
            assert result.digest == digest and result.size == len(payload)
    assert send.call_count == 3
    assert [r.close_calls for r in (failed, first, second)] == [1, 1, 1]


def test_observer_failure_after_first_payload_chunk() -> None:
    """A consumed chunk is counted before its observer fails and cleanup still runs."""
    response = StreamResponse(b"x" * 131072)
    failure = OSError("observer after payload")
    events: list[ManifestProgress] = []

    def observe(event: ManifestProgress) -> None:
        """Stop after the first real chunk rather than the initial zero-byte observation."""
        events.append(event)
        if event.bytes_completed:
            raise failure

    with (
        patch("requests.request", return_value=response),
        pytest.raises(OSError) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=observe)
    assert caught.value is failure
    assert events[-1].bytes_completed == 65536
    assert events[-1].phase == ManifestPhase.TRANSFERRING and response.close_calls == 1


@pytest.mark.parametrize("middleware", [False, True])
def test_transport_failure_before_response(middleware: bool) -> None:
    """Failure before acquiring a response preserves the error and reports no received bytes."""
    failure = requests.exceptions.Timeout("synthetic timeout")
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", side_effect=failure),
        pytest.raises(requests.exceptions.Timeout) as caught,
    ):
        get_manifest(
            client(middleware=middleware),
            REPO,
            "v1",
            ACCEPT,
            progress_callback=events.append,
        )
    assert caught.value is failure
    assert [e.phase for e in events] == [ManifestPhase.PREPARING, ManifestPhase.FAILED]


@pytest.mark.parametrize("middleware", [False, True])
def test_repeated_unauthorized_response_maps_to_auth_error(middleware: bool) -> None:
    """The final 401 belongs to the operation, not the already-closed challenge response."""
    challenge = StreamResponse(
        status=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="https://registry.example.invalid/token"'
        },
    )
    denied = StreamResponse(
        b'{"errors":[{"code":"UNAUTHORIZED","message":"denied"}]}', 401
    )
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", side_effect=[challenge, denied]),
        patch(
            "regshape.libs.auth.registryauth.authenticate",
            return_value="synthetic-token",
        ),
        pytest.raises(AuthError),
    ):
        get_manifest(
            client(middleware=middleware),
            REPO,
            "v1",
            ACCEPT,
            progress_callback=events.append,
        )
    assert challenge.close_calls == denied.close_calls == 1
    assert events[-1].phase == ManifestPhase.FAILED


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("status", [302, 307])
def test_requests_redirects_keep_stream_ownership(
    middleware: bool, status: int
) -> None:
    """Exercise Requests' real redirect loop without network or environment credentials."""
    redirect = StreamResponse(
        b"redirect body", status, {"Location": "/v2/team/artifact/manifests/resolved"}
    )
    final = StreamResponse(b"final payload")
    responses = iter([redirect, final])
    sent: list[requests.PreparedRequest] = []
    events: list[ManifestProgress] = []

    def send(request: requests.PreparedRequest, **options: object) -> requests.Response:
        """Supply synthetic adapter results while Session owns redirect handling."""
        assert options["stream"] is True
        response = next(responses)
        response.request = request
        assert request.url is not None
        response.url = request.url
        sent.append(request)
        return response

    with requests.Session() as session:
        session.trust_env = False
        with (
            patch.object(session.get_adapter("https://"), "send", side_effect=send),
            patch("requests.request", side_effect=session.request),
        ):
            body, _, _ = get_manifest(
                client(middleware=middleware),
                REPO,
                "v1",
                ACCEPT,
                progress_callback=events.append,
            )
    assert body == "final payload"
    assert [request.method for request in sent] == ["GET", "GET"]
    assert all(request.headers["Accept"] == ACCEPT for request in sent)
    assert redirect.close_calls == final.close_calls == 1
    assert events[-1].bytes_completed == len(b"final payload")
    assert final.content_reads == 0


@pytest.mark.parametrize("middleware", [False, True])
def test_streamed_json_diagnostics_do_not_capture_payload(
    middleware: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep the request-debug schema and header-only preview without rendering library progress."""
    output = io.StringIO()
    configure_telemetry(
        TelemetryConfig(debug_calls_enabled=True, output_format="json", output=output)
    )
    payload = b'{"schemaVersion":2}'
    response = StreamResponse(payload, headers={"Content-Length": str(len(payload))})
    events: list[ManifestProgress] = []
    with patch("requests.request", return_value=response):
        body, _, _ = get_manifest(
            client(middleware=middleware),
            REPO,
            "v1",
            ACCEPT,
            progress_callback=events.append,
        )
    diagnostic = json.loads(output.getvalue())
    assert diagnostic["response"]["body_preview"] is None
    assert diagnostic["response"]["body_size"] == 0
    assert body == payload.decode() and response.content_reads == 0
    assert events[-1].bytes_completed == len(payload)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_real_framing_failure_is_not_an_advisory_size_success() -> None:
    """A real incomplete HTTP frame must propagate, not become a successful short-body warning."""
    response = StreamResponse(b"", headers={"Content-Length": "100"})
    response.raw.close()
    response.raw = HTTPResponse(
        body=io.BytesIO(b"truncated"),
        headers={"Content-Length": "100"},
        preload_content=False,
        enforce_content_length=True,
    )
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response),
        pytest.raises(requests.exceptions.ChunkedEncodingError),
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)
    assert response.close_calls == 1
    assert events[-1].phase == ManifestPhase.FAILED
    assert all(event.phase != ManifestPhase.RECEIVED for event in events)


def test_immutable_primary_survives_cleanup_and_logging_failure() -> None:
    """Exception preservation must not require writable attributes on a caller's error."""

    @dataclass(frozen=True)
    class ImmutableFailure(Exception):
        """Caller-owned exception whose Python attribute assignments are prohibited."""

        detail: str

    primary = ImmutableFailure("primary")
    cause = ValueError("original cause")
    with pytest.raises(ImmutableFailure):
        raise primary from cause
    response = StreamResponse()
    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=primary),
        patch.object(response, "close", side_effect=OSError("cleanup")),
        patch("regshape.libs._http.logger.error", side_effect=RuntimeError("logging")),
        pytest.raises(ImmutableFailure) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=lambda event: None)
    assert caught.value is primary and primary.__cause__ is cause
    response.close()


@pytest.mark.parametrize("status", [404, 500])
def test_truncated_error_body_preserves_original_transport_failure(status: int) -> None:
    """Tolerant OCI error parsing must not swallow a live response framing failure."""
    response = StreamResponse(b"", status, {"Content-Length": "100"})
    response.raw.close()
    response.raw = HTTPResponse(
        body=io.BytesIO(b'{"errors":['),
        headers={"Content-Length": "100"},
        preload_content=False,
        enforce_content_length=True,
    )
    original_iterator = response.iter_content
    failures: list[requests.exceptions.RequestException] = []
    events: list[ManifestProgress] = []

    def chunks(chunk_size: int = 1, decode_unicode: bool = False) -> Iterator[bytes]:
        """Retain the actual Requests exception instance from the first body read."""
        assert not decode_unicode
        try:
            yield from original_iterator(chunk_size=chunk_size)
        except requests.exceptions.RequestException as error:
            failures.append(error)
            raise

    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=chunks) as iterate,
        pytest.raises(requests.exceptions.ChunkedEncodingError) as caught,
    ):
        get_manifest(client(), REPO, "v1", ACCEPT, progress_callback=events.append)
    assert len(failures) == 1 and caught.value is failures[0]
    assert iterate.call_count == response.close_calls == 1
    assert [event.phase for event in events] == [
        ManifestPhase.PREPARING,
        ManifestPhase.FAILED,
    ]

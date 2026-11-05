"""Raw manifest file receives through real clients, synthetic HTTP and owned temporary files."""

import gzip
import hashlib
import io
import tracemalloc
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
from urllib3.response import HTTPResponse

from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.manifests import (
    ManifestDownload,
    ManifestKind,
    ManifestPhase,
    download_manifest,
    get_manifest,
)
from regshape.libs.progress import ManifestProgress
from regshape.tests.test_manifest_progress import ACCEPT, REPO, StreamResponse, client


@pytest.fixture(autouse=True)
def isolated_telemetry() -> Iterator[None]:
    """Keep global diagnostic configuration separate from surrounding test modules."""
    previous = get_telemetry_config()
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(previous)


def digest(payload: bytes, algorithm: str = "sha256") -> str:
    """Compute fixture identity independently of the production receive implementation."""
    return f"{algorithm}:{hashlib.new(algorithm, payload).hexdigest()}"


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
@pytest.mark.parametrize("root", ["tag", "digest"])
@pytest.mark.parametrize("observed", [False, True])
def test_exact_bytes_and_identity(
    tmp_path: Path,
    middleware: bool,
    algorithm: str,
    root: str,
    observed: bool,
) -> None:
    """All primitive root modes preserve bytes and verify identity, including without observers."""
    payload = b' { "z": 2, "a": "\\u00e9" }\n' + b"\xff" * 140000
    identity = digest(payload, algorithm)
    reference = "v1" if root == "tag" else identity
    response = StreamResponse(
        payload,
        headers={
            "Content-Type": ACCEPT + "; charset=iso-8859-1",
            "Content-Length": str(len(payload)),
            "Docker-Content-Digest": identity,
        },
    )
    path = tmp_path / "manifest"
    events: list[ManifestProgress] = []

    def observe(event: ManifestProgress) -> None:
        """Check that receipt follows resource close and that network reads stay incremental."""
        assert response.content_reads == 0
        if event.phase == ManifestPhase.RECEIVED:
            assert response.close_calls == 1
            assert path.read_bytes() == payload
        events.append(event)

    with patch("requests.request", return_value=response) as send:
        result = download_manifest(
            client(middleware=middleware),
            REPO,
            reference,
            ACCEPT,
            path,
            expected_size=len(payload),
            progress_callback=observe if observed else None,
        )
    assert result == ManifestDownload(
        str(path), identity, len(payload), ACCEPT + "; charset=iso-8859-1", identity
    )
    assert path.read_bytes() == payload and response.close_calls == 1
    send.assert_called_once()
    assert send.call_args.kwargs["stream"] is True
    assert send.call_args.args[1].endswith("/manifests/" + reference)
    with pytest.raises(FrozenInstanceError):
        result.size = 0  # type: ignore[misc]
    if observed:
        assert [event.bytes_completed for event in events] == [
            0,
            0,
            65536,
            131072,
            len(payload),
            len(payload),
        ]
        assert events[0].phase == ManifestPhase.PREPARING
        assert events[-1].phase == ManifestPhase.RECEIVED
        assert all(event.kind == ManifestKind.MANIFEST for event in events[1:])
        assert len({event.transfer_id for event in events}) == 1
    else:
        assert events == []


@pytest.mark.parametrize("requested", [None, "sha256", "sha512"])
@pytest.mark.parametrize("expected", [None, "sha256", "sha512"])
@pytest.mark.parametrize("reported", [None, "sha256", "sha512"])
def test_digest_priority_and_cross_algorithm_verification(
    tmp_path: Path,
    requested: str | None,
    expected: str | None,
    reported: str | None,
) -> None:
    """Request, descriptor and registry digests are all checked, without cross-algorithm equality."""
    payload = b" \n{}"
    response = StreamResponse(
        payload,
        headers={
            **(
                {"Docker-Content-Digest": digest(payload, reported)} if reported else {}
            ),
        },
    )
    path = tmp_path / "raw"
    with patch("requests.request", return_value=response):
        result = download_manifest(
            client(),
            REPO,
            digest(payload, requested) if requested else "v1",
            ACCEPT,
            path,
            expected_digest=digest(payload, expected) if expected else None,
        )
    assert result.digest == digest(
        payload, requested or expected or reported or "sha256"
    )
    assert result.reported_digest == (digest(payload, reported) if reported else "")
    assert result.media_type == "" and path.read_bytes() == payload


@pytest.mark.parametrize(
    "value", ["", "sha1:" + "0" * 40, "sha256:" + "A" * 64, "sha512:abcd", " ../bad"]
)
def test_bad_header_digest_rejected_and_cleaned(tmp_path: Path, value: str) -> None:
    """Present empty/unsupported/malformed digest headers are errors, never silently absent."""
    response = StreamResponse(b"{}", headers={"Docker-Content-Digest": value})
    path = tmp_path / "raw"
    with (
        patch("requests.request", return_value=response),
        pytest.raises(ManifestError, match="Invalid Docker"),
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path)
    assert not path.exists() and response.close_calls == 1


@pytest.mark.parametrize("field", ["reference", "expected_digest", "header"])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
def test_wrong_digest_from_every_source_fails(
    tmp_path: Path, field: str, algorithm: str
) -> None:
    """Every authoritative digest source is verified against the original entity bytes."""
    wrong = digest(b"wrong", algorithm)
    response = StreamResponse(
        b"{}", headers={"Docker-Content-Digest": wrong} if field == "header" else {}
    )
    path = tmp_path / "raw"
    with (
        patch("requests.request", return_value=response),
        pytest.raises(ManifestError, match="digest verification"),
    ):
        download_manifest(
            client(),
            REPO,
            wrong if field == "reference" else "v1",
            ACCEPT,
            path,
            expected_digest=wrong if field == "expected_digest" else None,
        )
    assert not path.exists() and response.close_calls == 1


@pytest.mark.parametrize(
    "option,value",
    [
        ("repo", "../x"),
        ("repo", "a?b"),
        ("repo", "/root"),
        ("repo", ""),
        ("repo", "a//b"),
        ("reference", "tag#suffix"),
        ("reference", "v?x"),
        ("reference", "%2f"),
        ("reference", ""),
        ("reference", "sha256:abc"),
        ("reference", "a" * 129),
        ("accept", ""),
        ("accept", "x\r\ninjected: value"),
        ("accept", "non-ascii-\u00e9"),
        ("expected_digest", ""),
        ("expected_digest", "sha256:" + "A" * 64),
        ("expected_size", -1),
        ("expected_size", True),
        ("expected_size", "1"),
        ("expected_size", 1.5),
        ("max_bytes", 0),
        ("max_bytes", -1),
        ("max_bytes", True),
        ("max_bytes", 1.5),
    ],
)
def test_invalid_inputs_have_no_io(tmp_path: Path, option: str, value: object) -> None:
    """Invalid caller inputs fail before network or exclusive file creation."""
    options: dict[str, object] = {"repo": REPO, "reference": "v1", "accept": ACCEPT}
    options[option] = value
    path = tmp_path / "raw"
    with patch("requests.request") as send, pytest.raises(ValueError):
        download_manifest(client(), output_path=path, **options)  # type: ignore[arg-type]
    send.assert_not_called()
    assert not path.exists()


def test_preflight_observer_and_conflicting_size_digest(tmp_path: Path) -> None:
    """Observer type, same-algorithm contradictions and impossible expected sizes fail before I/O."""
    path = tmp_path / "raw"
    with patch("requests.request") as send:
        with pytest.raises(TypeError):
            download_manifest(client(), REPO, "v1", ACCEPT, path, progress_callback=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="disagree"):
            download_manifest(
                client(), REPO, digest(b"x"), ACCEPT, path, expected_digest=digest(b"y")
            )
        with pytest.raises(ManifestError, match="exceeds"):
            download_manifest(
                client(), REPO, "v1", ACCEPT, path, expected_size=3, max_bytes=2
            )
    send.assert_not_called()
    assert not path.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
def test_existing_destination_untouched(tmp_path: Path, kind: str) -> None:
    """Exclusive creation rejects all existing entry types without GET or destructive cleanup."""
    path = tmp_path / "raw"
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    if kind == "file":
        path.write_bytes(b"keep")
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(target if kind == "symlink" else tmp_path / "missing")
    with patch("requests.request") as send, pytest.raises(FileExistsError):
        download_manifest(client(), REPO, "v1", ACCEPT, path)
    send.assert_not_called()
    assert target.read_bytes() == b"keep"
    assert path.exists() or path.is_symlink()


@pytest.mark.parametrize("payload", [b"", b"x", b"xx", b"xxx"])
@pytest.mark.parametrize("expected_size", [None, 0, 1, 2])
def test_exact_size_and_cap_boundaries(
    tmp_path: Path, payload: bytes, expected_size: int | None
) -> None:
    """Known zero and exact/+1 boundaries enforce descriptor sizes independently from headers."""
    response = StreamResponse(payload, headers={"Content-Length": "999"})
    path = tmp_path / "raw"
    events: list[ManifestProgress] = []
    with patch("requests.request", return_value=response):
        if len(payload) > 2 or (
            expected_size is not None and len(payload) != expected_size
        ):
            with pytest.raises(ManifestError):
                download_manifest(
                    client(),
                    REPO,
                    "v1",
                    ACCEPT,
                    path,
                    expected_size=expected_size,
                    max_bytes=2,
                    progress_callback=events.append,
                )
            assert not path.exists() and events[-1].phase == ManifestPhase.FAILED
        else:
            result = download_manifest(
                client(),
                REPO,
                "v1",
                ACCEPT,
                path,
                expected_size=expected_size,
                max_bytes=2,
                progress_callback=events.append,
            )
            assert result.size == len(payload) and path.read_bytes() == payload
            assert events[-1].phase == ManifestPhase.RECEIVED
    assert response.close_calls == 1


def test_compressed_entity_bytes_and_unknown_total(tmp_path: Path) -> None:
    """HTTP coding is removed, not charset-decoded; compressed wire size is not a payload total."""
    payload = b'{ "not": "normalized" }\n' * 10000
    compressed = gzip.compress(payload)
    response = StreamResponse(
        compressed,
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
        raw_headers={"Content-Encoding": "gzip"},
    )
    events: list[ManifestProgress] = []
    path = tmp_path / "raw"
    with patch("requests.request", return_value=response):
        result = download_manifest(
            client(),
            REPO,
            digest(payload),
            ACCEPT,
            path,
            progress_callback=events.append,
        )
    assert path.read_bytes() == payload and result.size == len(payload)
    assert all(event.total_bytes is None for event in events)


@pytest.mark.parametrize("phase", list(ManifestPhase)[:3])
def test_observer_failure_identity_and_cleanup(
    tmp_path: Path, phase: ManifestPhase
) -> None:
    """PREPARING fails before I/O; TRANSFERRING/RECEIVED failure closes and removes only owned output."""
    error = RuntimeError("observer")
    response = StreamResponse(b"{}")
    path = tmp_path / "raw"
    events: list[ManifestProgress] = []

    def observe(event: ManifestProgress) -> None:
        """Raise the same caller exception at the chosen lifecycle boundary."""
        events.append(event)
        if event.phase == phase:
            raise error

    with (
        patch("requests.request", return_value=response) as send,
        pytest.raises(RuntimeError) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path, progress_callback=observe)
    assert caught.value is error and not path.exists()
    assert events[-1].phase == phase
    assert send.call_count == (0 if phase == ManifestPhase.PREPARING else 1)
    assert response.close_calls == send.call_count


@pytest.mark.parametrize("interruption", [False, True])
def test_read_failure_and_secondary_cleanup_preserve_primary(
    tmp_path: Path, interruption: bool, caplog
) -> None:
    """Read/cancellation errors win over response-close and failure-observer errors."""
    primary = (
        KeyboardInterrupt()
        if interruption
        else requests.ConnectionError("synthetic read")
    )
    response = StreamResponse(b"{}")
    path = tmp_path / "raw"
    events: list[ManifestProgress] = []

    def chunks(chunk_size: int) -> Iterator[bytes]:
        """Expose partial consumption, then the original failure."""
        yield b"{"
        raise primary

    def observe(event: ManifestProgress) -> None:
        """Record terminal state before an independent failure-notification error."""
        events.append(event)
        if event.phase in (ManifestPhase.FAILED, ManifestPhase.CANCELLED):
            raise RuntimeError("observer cleanup")

    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=chunks),
        patch.object(response, "close", side_effect=OSError("response close")),
        pytest.raises(type(primary)) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path, progress_callback=observe)
    response.close()
    assert caught.value is primary and not path.exists()
    assert events[-1].phase == (
        ManifestPhase.CANCELLED if interruption else ManifestPhase.FAILED
    )
    assert "preserving the original error" in caplog.text


@dataclass(frozen=True)
class FrozenFailure(Exception):
    """Synthetic caller exception that rejects attribute assignment during cleanup."""

    reason: str


def test_frozen_exception_survives_file_and_logging_failures(tmp_path: Path) -> None:
    """No generator context or manual traceback assignment may replace an immutable error."""
    primary = FrozenFailure("caller")
    path = tmp_path / "raw"
    response = StreamResponse(b"{}")

    def observe(event: ManifestProgress) -> None:
        """Fail after file creation while forcing cleanup logging to fail independently."""
        if event.phase == ManifestPhase.TRANSFERRING:
            raise primary

    with (
        patch("requests.request", return_value=response),
        patch.object(Path, "unlink", side_effect=OSError("cleanup")),
        patch("regshape.libs._http.logger.error", side_effect=RuntimeError("logging")),
    ):
        try:
            download_manifest(
                client(), REPO, "v1", ACCEPT, path, progress_callback=observe
            )
        except FrozenFailure as caught:
            assert caught is primary
        else:
            pytest.fail("Caller error was lost")
    assert response.close_calls == 1
    path.unlink()


@pytest.mark.parametrize("fault", ["write", "short", "close"])
def test_output_faults_remove_owned_output(tmp_path: Path, fault: str) -> None:
    """Output write/short-write/close failures cannot return a received result."""
    path = tmp_path / "raw"
    response = StreamResponse(b"{}")
    original = Path.open
    error = OSError("synthetic output")

    def open_fault(self: Path, *args, **kwargs):
        """Wrap only this output file while retaining real exclusive creation and descriptor identity."""
        file = original(self, *args, **kwargs)
        if self != path:
            return file
        wrapper = Mock(wraps=file)
        if fault == "write":
            wrapper.write.side_effect = error
        elif fault == "short":
            wrapper.write.return_value = 1
        else:

            def close() -> None:
                """Close the real handle before reporting a flush/close failure."""
                file.close()
                raise error

            wrapper.close.side_effect = close
        return wrapper

    with (
        patch("requests.request", return_value=response),
        patch.object(Path, "open", open_fault),
        pytest.raises(OSError) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path)
    if fault != "short":
        assert caught.value is error
    assert not path.exists() and response.close_calls == 1


def test_replaced_output_is_not_removed(tmp_path: Path, caplog) -> None:
    """A caller-side rename/replacement during notification is not deleted during failure cleanup."""
    path = tmp_path / "raw"
    moved = tmp_path / "owned-original"
    primary = RuntimeError("replace output")

    def observe(event: ManifestProgress) -> None:
        """Replace the entry after receipt, then fail, exercising cleanup identity guards."""
        if event.phase == ManifestPhase.RECEIVED:
            path.rename(moved)
            path.write_bytes(b"replacement")
            raise primary

    with (
        patch("requests.request", return_value=StreamResponse(b"{}")),
        pytest.raises(RuntimeError) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path, progress_callback=observe)
    assert caught.value is primary and path.read_bytes() == b"replacement"
    assert moved.read_bytes() == b"{}"
    assert "Manifest output cleanup" in caplog.text


@pytest.mark.parametrize("status", [401, 404, 500])
def test_bounded_http_errors_close_and_cleanup(tmp_path: Path, status: int) -> None:
    """Existing status taxonomy is retained without eager reads of the acquired response."""
    response = StreamResponse(
        b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"missing"}]}', status
    )
    path = tmp_path / "raw"
    transport = Mock()
    transport.config.registry = "registry.example.invalid"
    transport.get.return_value = response
    with pytest.raises(AuthError if status == 401 else ManifestError):
        download_manifest(transport, REPO, "v1", ACCEPT, path)
    assert (
        response.close_calls == 1 and response.content_reads == 0 and not path.exists()
    )


def test_oversized_error_body_has_a_separate_bounded_cap(tmp_path: Path) -> None:
    """Even a large allowed manifest cannot permit an unbounded error-body parser read."""
    response = StreamResponse(b"x" * 100000, 500)
    with (
        patch("requests.request", return_value=response),
        pytest.raises(ManifestError, match="error-body limit"),
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, tmp_path / "raw")
    assert response.content_reads == 0 and response.close_calls == 1
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize("status", [200, 404, 500])
def test_truncated_body_preserves_first_transport_error(
    tmp_path: Path, status: int
) -> None:
    """Actual urllib3 framing failures propagate once, before tolerant error-body parsing."""
    response = StreamResponse(b"{", status, raw_headers={"Content-Length": "10"})
    failures: list[BaseException] = []
    original = response.iter_content

    def chunks(chunk_size: int) -> Iterator[bytes]:
        """Record the exact exception produced by the real stream, not a mocked substitute."""
        try:
            yield from original(chunk_size=chunk_size)
        except requests.RequestException as exc:
            failures.append(exc)
            raise

    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=chunks),
        pytest.raises(requests.RequestException) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, tmp_path / "raw")
    assert len(failures) == 1 and caught.value is failures[0]
    assert response.close_calls == 1 and not (tmp_path / "raw").exists()


def test_raw_download_bypasses_warm_buffered_cache_without_observer(
    tmp_path: Path,
) -> None:
    """Every raw receive streams even when no observer is supplied; old buffered reuse is untouched."""
    transport = client(caching=True)
    responses = [StreamResponse(b"{}"), StreamResponse(b" {}"), StreamResponse(b"{}\n")]
    with patch("requests.request", side_effect=responses) as send:
        assert get_manifest(transport, REPO, "v1", ACCEPT)[0] == "{}"
        for i in range(2):
            path = tmp_path / str(i)
            download_manifest(transport, REPO, "v1", ACCEPT, path)
        assert send.call_count == 3
    assert (tmp_path / "0").read_bytes() == b" {}"
    assert (tmp_path / "1").read_bytes() == b"{}\n"


def test_retry_discard_closed_before_second_attempt(tmp_path: Path) -> None:
    """Existing request retry owns discarded streams; raw download owns only the final response."""
    discarded = StreamResponse(b"retry", 500)
    accepted = StreamResponse(b"{}")

    def send(*args, **kwargs) -> requests.Response:
        """Inspect closure before exposing the next synthetic response."""
        if send_mock.call_count == 1:
            return discarded
        assert discarded.close_calls == 1
        return accepted

    with patch("requests.request", side_effect=send) as send_mock:
        result = download_manifest(
            client(retries=True), REPO, "v1", ACCEPT, tmp_path / "raw"
        )
    assert result.digest == digest(b"{}") and send_mock.call_count == 2
    assert discarded.close_calls == accepted.close_calls == 1


def test_bounded_success_reads_and_no_json_parsing(tmp_path: Path) -> None:
    """The primitive never needs a complete byte array, valid UTF-8 or valid JSON."""
    payload = b"\xff" * (65536 * 3 + 1)
    response = StreamResponse(payload)
    original = response.iter_content
    reads = []

    def chunks(chunk_size: int) -> Iterator[bytes]:
        """Assert the requested and yielded chunk bounds throughout a multi-chunk transfer."""
        reads.append(chunk_size)
        assert chunk_size <= 65536
        for chunk in original(chunk_size):
            assert len(chunk) <= 65536
            yield chunk

    with (
        patch("requests.request", return_value=response),
        patch.object(response, "iter_content", side_effect=chunks),
    ):
        result = download_manifest(
            client(), REPO, "v1", ACCEPT, tmp_path / "raw", max_bytes=len(payload)
        )
    assert result.size == len(payload) and reads == [65536]


@pytest.mark.parametrize("middleware", [False, True])
@pytest.mark.parametrize("auth_fails", [False, True])
def test_authentication_discards_stream_and_preserves_identity(
    tmp_path: Path,
    middleware: bool,
    auth_fails: bool,
) -> None:
    """The initial 401 closes before authentication; only successful final content is saved."""
    challenge = {
        "WWW-Authenticate": 'Bearer realm="https://registry.example.invalid/token",scope="repository:team/artifact:pull"'
    }
    denied = StreamResponse(b"denied", 401, headers=challenge)
    accepted = StreamResponse(b"{}")
    primary = AuthError("synthetic authentication failure")
    path = tmp_path / "raw"
    events: list[ManifestProgress] = []

    def authenticate(*args: object) -> str:
        """Check release at the auth handoff before returning a synthetic token."""
        assert denied.close_calls == 1
        if auth_fails:
            raise primary
        return "synthetic-token"

    with (
        patch("requests.request", side_effect=[denied, accepted]) as send,
        patch("regshape.libs.auth.registryauth.authenticate", side_effect=authenticate),
    ):
        if auth_fails:
            with pytest.raises(AuthError) as caught:
                download_manifest(
                    client(middleware=middleware),
                    REPO,
                    "v1",
                    ACCEPT,
                    path,
                    progress_callback=events.append,
                )
            assert caught.value is primary and not path.exists()
        else:
            result = download_manifest(
                client(middleware=middleware),
                REPO,
                "v1",
                ACCEPT,
                path,
                progress_callback=events.append,
            )
            assert result.digest == digest(b"{}") and path.read_bytes() == b"{}"
            assert accepted.close_calls == 1
    assert send.call_count == (1 if auth_fails else 2) and denied.close_calls == 1
    assert len({event.transfer_id for event in events}) == 1
    accepted.close()


@pytest.mark.parametrize("middleware", [False, True])
def test_diagnostics_before_handoff_close_and_remove_file(
    tmp_path: Path, middleware: bool
) -> None:
    """A post-response diagnostic failure cannot strand an acquired stream or empty output."""
    primary = OSError("synthetic diagnostics")
    output = Mock(wraps=io.StringIO())
    output.write.side_effect = primary
    configure_telemetry(TelemetryConfig(debug_calls_enabled=True, output=output))
    response = StreamResponse(b"{}")
    with (
        patch("requests.request", return_value=response),
        pytest.raises(OSError) as caught,
    ):
        download_manifest(
            client(middleware=middleware), REPO, "v1", ACCEPT, tmp_path / "raw"
        )
    assert caught.value is primary
    assert response.close_calls == 1 and not (tmp_path / "raw").exists()


def test_sole_response_close_failure_is_not_received(tmp_path: Path) -> None:
    """A successful body cannot become successful output when closing its response fails."""
    response = StreamResponse(b"{}")
    error = OSError("synthetic response close")
    events: list[ManifestProgress] = []
    with (
        patch("requests.request", return_value=response),
        patch.object(response, "close", side_effect=error),
        pytest.raises(OSError) as caught,
    ):
        download_manifest(
            client(),
            REPO,
            "v1",
            ACCEPT,
            tmp_path / "raw",
            progress_callback=events.append,
        )
    response.close()
    assert caught.value is error and not (tmp_path / "raw").exists()
    assert events[-1].phase == ManifestPhase.FAILED
    assert not any(event.phase == ManifestPhase.RECEIVED for event in events)


def test_missing_parent_does_not_create_directories_or_issue_get(
    tmp_path: Path,
) -> None:
    """The primitive never invents or owns a caller's missing directory hierarchy."""
    path = tmp_path / "missing" / "raw"
    with patch("requests.request") as send, pytest.raises(FileNotFoundError):
        download_manifest(client(), REPO, "v1", ACCEPT, path)
    send.assert_not_called()
    assert not path.parent.exists()


def test_heap_does_not_scale_with_uncompressed_body(tmp_path: Path) -> None:
    """Real urllib3 reads keep 2/32 MiB generated bodies outside Python's retained heap."""

    class GeneratedBody(io.RawIOBase):
        """Yield repeated bytes without allocating a complete response fixture."""

        def __init__(self, size: int) -> None:
            """Record remaining bytes and the largest requested bounded read."""
            super().__init__()
            self.remaining = size
            self.largest_read = 0

        def read(self, size: int = -1) -> bytes:
            """Reject unbounded reads and synthesize only the requested next block."""
            assert 0 < size <= 65536
            self.largest_read = max(self.largest_read, size)
            count = min(self.remaining, size)
            self.remaining -= count
            return b"x" * count

    peaks = []
    for size in (2 * 1024**2, 32 * 1024**2):
        generated = GeneratedBody(size)
        response = StreamResponse()
        response.raw = HTTPResponse(body=generated, preload_content=False)
        path = tmp_path / str(size)
        tracemalloc.start()
        try:
            with patch("requests.request", return_value=response):
                result = download_manifest(
                    client(), REPO, "v1", ACCEPT, path, max_bytes=size
                )
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
        assert (
            generated.largest_read <= 65536
            and path.stat().st_size == result.size == size
        )
        assert response.content_reads == 0 and response.close_calls == 1
    assert peaks[1] - peaks[0] < 1024**2


@pytest.mark.parametrize("phase", [ManifestPhase.TRANSFERRING, ManifestPhase.RECEIVED])
def test_silent_output_replacement_cannot_return_success(
    tmp_path: Path, phase: ManifestPhase, caplog
) -> None:
    """Even a normally returning observer cannot make a replacement file look verified."""
    path = tmp_path / "raw"
    moved = tmp_path / "owned-original"
    replaced = False

    def observe(event: ManifestProgress) -> None:
        """Move the owned file once without directly raising an observer error."""
        nonlocal replaced
        if event.phase == phase and not replaced:
            path.rename(moved)
            path.write_bytes(b"replacement")
            replaced = True

    with (
        patch("requests.request", return_value=StreamResponse(b"{}")),
        pytest.raises(OSError, match="replaced"),
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path, progress_callback=observe)
    assert path.read_bytes() == b"replacement" and moved.read_bytes() == b"{}"
    assert "Manifest output cleanup" in caplog.text


def test_write_and_close_failure_keep_first_error(tmp_path: Path, caplog) -> None:
    """A second owned-output failure cannot replace the original write exception."""
    path = tmp_path / "raw"
    original = Path.open
    primary = OSError("write")

    def open_fault(self: Path, *args, **kwargs):
        """Return a real exclusive handle with deterministic write and close faults."""
        file = original(self, *args, **kwargs)
        if self != path:
            return file
        wrapper = Mock(wraps=file)
        wrapper.write.side_effect = primary

        def close() -> None:
            """Release the actual descriptor before reporting the secondary error."""
            file.close()
            raise RuntimeError("close")

        wrapper.close.side_effect = close
        return wrapper

    with (
        patch.object(Path, "open", open_fault),
        patch("requests.request", return_value=StreamResponse(b"{}")),
        pytest.raises(OSError) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path)
    assert caught.value is primary and not path.exists()
    assert "Manifest output close" in caplog.text


def test_failed_identity_acquisition_reports_retained_file(
    tmp_path: Path, caplog
) -> None:
    """Without a reliable file identity cleanup refuses to guess which entry is safe to remove."""
    path = tmp_path / "raw"
    primary = OSError("identity")
    with (
        patch("regshape.libs.manifests.download.os.fstat", side_effect=primary),
        patch("requests.request") as send,
        pytest.raises(OSError) as caught,
    ):
        download_manifest(client(), REPO, "v1", ACCEPT, path)
    assert caught.value is primary and path.read_bytes() == b""
    assert "newly created file retained" in caplog.text
    send.assert_not_called()

"""End-to-end image progress with real layouts, mocked Docker export and consuming HTTP calls."""

import hashlib
import io
import json
import shutil
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.blobs import upload_blob_chunked, upload_blob_stream
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.docker import push_image
from regshape.libs.errors import BlobError, DockerError, ManifestError
from regshape.libs.layout.operations import (
    _hash_blob_stream,
    generate_manifest,
    push_layout,
    read_blob,
    read_index,
)
from regshape.libs.models.manifest import ImageManifest, parse_manifest
from regshape.libs.progress import TransferDirection, TransferPhase, TransferProgress
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.middleware import RetryConfig
from regshape.tests.test_layout_push import _build_layout
from regshape.tests.test_progress_display import Terminal


@pytest.fixture(autouse=True)
def reset_telemetry():
    """Keep global telemetry state independent across library and Click scenarios."""
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(TelemetryConfig())


def _layout(tmp_path: Path, *, shared: bool = True) -> tuple[Path, dict[str, bytes]]:
    """Build two distinct manifests sharing layer/config digests, or a single Docker image."""
    path = _build_layout(tmp_path, ref_name="first")
    if shared:
        generate_manifest(path, ref_name="second", annotations={"test.revision": "second"})
    first = read_index(path).manifests[0]
    manifest = parse_manifest(read_blob(path, first.digest).decode())
    assert isinstance(manifest, ImageManifest)
    payloads = {d.digest: read_blob(path, d.digest) for d in [*manifest.layers, manifest.config]}
    return path, payloads


def _response(status: int, headers: dict[str, str] | None = None) -> requests.Response:
    """Create an already-consumed registry response without a live socket."""
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = b""
    response._content_consumed = True
    return response


class Registry:
    """Consume both upload protocols, rotate session state, and optionally fail one operation."""

    def __init__(
        self, payloads: dict[str, bytes], existing: set[str] | None = None,
        *, failure: str | None = None, retry_method: str | None = None,
    ) -> None:
        """Own deterministic server buffers and a clock advanced by each request/read."""
        self.payloads = payloads
        self.existing = existing or set()
        self.failure = failure
        self.retry_method = retry_method
        self.retried = False
        self.now = 0.0
        self.calls: list[tuple[str, str]] = []
        self.buffers: dict[str, bytearray] = {}
        self.states: dict[str, str] = {}
        self.stored: dict[str, bytes] = {}
        self.attempts: list[tuple[str, bytes]] = []

    def respond(
        self, method: str, url: str, *,
        data: bytes | io.RawIOBase | None = None,
        params: list[tuple[str, str]] | dict[str, str] | None = None,
        **kwargs: object,
    ) -> requests.Response:
        """Validate upload URLs and consume counted bodies before returning commit responses."""
        self.now += 0.2
        path = urlsplit(url).path
        self.calls.append((method, path))
        query = dict(parse_qsl(urlsplit(url).query))
        query.update(dict(params or {}))
        if "/manifests/" in path:
            assert method == "PUT" and isinstance(data, bytes)
            if self.failure == "manifest":
                return _response(500)
            return _response(201, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(data).hexdigest()})
        if method == "HEAD":
            digest = unquote(path.rsplit("/", 1)[1])
            return _response(200 if digest in self.existing else 404, {
                "Content-Length": str(len(self.payloads[digest])),
                "Docker-Content-Digest": digest,
            })
        if method == "POST":
            session = str(len(self.buffers))
            self.buffers[session] = bytearray()
            self.states[session] = "initial"
            return _response(202, {"Location": f"/v2/repo/blobs/uploads/{session}?_state=initial"})
        session = path.rsplit("/", 1)[1]
        assert query["_state"] == self.states[session]
        if method == "PATCH":
            assert isinstance(data, bytes)
            if self.retry_method == method and not self.retried:
                self.retried = True
                return _response(503)
            self.buffers[session].extend(data)
            self.states[session] = f"offset-{len(self.buffers[session])}"
            return _response(202, {
                "Location": f"/v2/repo/blobs/uploads/{session}?_state={self.states[session]}",
                "Range": f"0-{len(self.buffers[session]) - 1}",
            })
        assert method == "PUT"
        digest = query["digest"]
        if isinstance(data, io.RawIOBase):
            chunks: list[bytes] = []
            while True:
                self.now += 0.2
                chunk = data.read(7)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = b"".join(chunks)
        else:
            assert data == b""
            payload = bytes(self.buffers[session])
        self.attempts.append((digest, payload))
        if self.retry_method == method and not self.retried:
            self.retried = True
            return _response(503)
        if self.failure == "network":
            raise requests.exceptions.ConnectionError("synthetic upload failure")
        if self.failure in ("200", "202", "204"):
            return _response(int(self.failure), {"Docker-Content-Digest": digest})
        if self.failure == "redirect-get":
            response = _response(201, {"Docker-Content-Digest": digest})
            response.request = requests.Request("GET", url).prepare()
            return response
        if self.failure == "digest":
            return _response(201, {"Docker-Content-Digest": "sha256:" + "f" * 64})
        assert payload == self.payloads[digest]
        self.stored[digest] = payload
        return _response(201, {"Docker-Content-Digest": digest})


def _client(*, retry: bool = False) -> RegistryClient:
    """Use actual registry dispatch with optional zero-delay retry middleware."""
    return RegistryClient(TransportConfig(
        registry="registry.example.invalid", enable_middleware=retry,
        enable_retries=retry, retry_config=RetryConfig(max_retries=1, backoff_factor=0),
    ))


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("existing", ["none", "one", "all"])
@pytest.mark.parametrize("force", [False, True])
def test_layout_unique_progress_and_legacy_callbacks(
    tmp_path: Path, chunked: bool, existing: str, force: bool,
) -> None:
    """Shared blobs produce one terminal observation/row while legacy occurrence reports remain intact."""
    path, payloads = _layout(tmp_path)
    present = set(payloads) if existing == "all" else (set(list(payloads)[:1]) if existing == "one" else set())
    registry = Registry(payloads, present)
    events: list[TransferProgress] = []
    legacy: list[tuple[str, dict[str, object]]] = []
    terminal = Terminal()
    uploader = upload_blob_chunked if chunked else upload_blob_stream

    def observe(event: TransferProgress) -> None:
        """Record typed events and exercise real screen rendering for every observation."""
        events.append(event)
        display.observe(event)

    def legacy_observe(event: str, **kwargs: object) -> None:
        """Retain the original string-event shape independently of typed byte observations."""
        legacy.append((event, kwargs))

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        patch(f"regshape.libs.blobs.{uploader.__name__}", wraps=uploader) as uploads,
        ProgressDisplay(stream=terminal, columns=lambda: 160, clock=lambda: registry.now,
                        total_blobs=None, show_counts=True) as display,
    ):
        result = push_layout(path, _client(), "repo", force=force, chunked=chunked, chunk_size=7,
                             progress_callback=legacy_observe, transfer_callback=observe)
    skipped = set() if force else present
    uploaded = set(payloads) - skipped
    assert result.manifests_pushed == 2 and result.blobs_uploaded == len(uploaded)
    assert result.blobs_skipped == 2 + len(skipped)
    assert result.bytes_uploaded == sum(len(payloads[d]) for d in uploaded)
    assert registry.stored == {d: payloads[d] for d in uploaded}
    assert all(call.args[2].closed for call in uploads.call_args_list)
    assert sum(method == "HEAD" for method, _ in registry.calls) == (0 if force else 2)
    assert {e.digest for e in events if e.phase == TransferPhase.COMPLETED} == uploaded
    assert [e.digest for e in events if e.phase == TransferPhase.SKIPPED] == [d for d in payloads if d in skipped]
    assert all(e.bytes_completed == 0 for e in events if e.phase == TransferPhase.SKIPPED)
    assert all(e.total_bytes == len(payloads[e.digest]) for e in events)
    assert sum(name == "blob_skip" for name, _ in legacy) == result.blobs_skipped
    assert sum(name == "blob_done" for name, _ in legacy) == result.blobs_uploaded
    assert sum(name == "manifest_done" for name, _ in legacy) == 2
    for name, fields in legacy:
        assert set(fields) == (
            {"digest", "size", "media_type"} if name == "blob_start" else
            {"digest", "reference"} if name == "manifest_done" else {"digest", "size"}
        )
    rows = [r for r in terminal.rows if r.startswith("Blob")]
    assert len(rows) == 2
    assert display.completed == len(uploaded) and display.skipped == len(skipped)
    assert f"[{len(uploaded)} done/{len(skipped)} skipped]" in rows[-1]
    if uploaded:
        assert any(
            e.phase == TransferPhase.TRANSFERRING and e.total_bytes is not None
            and 0 < e.bytes_completed < e.total_bytes for e in events
        )


@pytest.mark.parametrize("chunked", [False, True])
def test_layout_retry_does_not_inflate_rows_or_bytes(tmp_path: Path, chunked: bool) -> None:
    """Count retransmission as the same logical blob while keeping rotated session parameters."""
    path, payloads = _layout(tmp_path)
    registry = Registry(payloads, retry_method="PATCH" if chunked else "PUT")
    terminal = Terminal()
    events: list[TransferProgress] = []

    def observe(event: TransferProgress) -> None:
        """Record logical retries while rendering the shared row."""
        events.append(event)
        display.observe(event)

    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        ProgressDisplay(stream=terminal, total_blobs=None, show_counts=True) as display,
    ):
        result = push_layout(path, _client(retry=True), "repo", force=True, chunked=chunked,
                             chunk_size=7, transfer_callback=observe)
    assert registry.retried and result.bytes_uploaded == sum(map(len, payloads.values()))
    assert display.completed == 2 and len([r for r in terminal.rows if r.startswith("Blob")]) == 2
    assert sum(e.phase == TransferPhase.RETRYING for e in events) == (0 if chunked else 1)


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("failure", ["network", "digest", "manifest", "200", "202", "204", "redirect-get"])
def test_layout_failure_never_reports_an_uncommitted_blob(
    tmp_path: Path, chunked: bool, failure: str,
) -> None:
    """Invalid commits and digest/network errors remain failed; manifest failure stops the overall push."""
    path, payloads = _layout(tmp_path)
    registry = Registry(payloads, failure=failure)
    events: list[TransferProgress] = []
    expected_error = (
        requests.exceptions.ConnectionError if failure == "network"
        else ManifestError if failure == "manifest" else BlobError
    )
    with (
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        pytest.raises(expected_error),
    ):
        push_layout(path, _client(), "repo", force=True, chunked=chunked,
                    chunk_size=7, transfer_callback=events.append)
    if failure != "manifest":
        assert events[-1].phase == TransferPhase.FAILED
        assert not any(e.phase == TransferPhase.COMPLETED for e in events)
    else:
        assert sum(e.phase == TransferPhase.COMPLETED for e in events) == 2
        assert sum("/manifests/" in path for _, path in registry.calls) == 1


@pytest.mark.parametrize("command", ["layout", "docker"])
@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("mode", ["tty", "redirected", "disabled", "json", "structured"])
def test_image_command_wiring_and_clean_result_channels(
    tmp_path: Path, command: str, chunked: bool, mode: str,
) -> None:
    """Real command/library flows retain one row per digest and preserve each command's result schema."""
    path, payloads = _layout(tmp_path, shared=command == "layout")
    registry = Registry(payloads, set(list(payloads)[:1]))
    terminal = Terminal(interactive=mode != "redirected")
    exported: list[Path] = []
    displays: list[ProgressDisplay] = []

    def display(*, disabled: bool, structured: bool, total_blobs: int | None, show_counts: bool) -> ProgressDisplay:
        """Use a real renderer on a deterministic terminal, preserving all command options."""
        get_telemetry_config().output = terminal
        instance = ProgressDisplay(
            stream=terminal, columns=lambda: 160, clock=lambda: registry.now,
            disabled=disabled, structured=structured, total_blobs=total_blobs, show_counts=show_counts,
        )
        displays.append(instance)
        return instance

    def export(image: str, output: Path, *, platform: str | None) -> None:
        """Replace only the Docker daemon boundary and check preparation precedes export."""
        assert image == "example:latest" and platform == "linux/arm64"
        if mode == "tty":
            assert terminal.rows[-1] == "Preparing Docker image ..."
        shutil.copytree(path, output)
        exported.append(output)

    args = [command, "push", "--dest", "registry.example.invalid/repo", "--chunk-size", "7"]
    if command == "layout":
        args.extend(["--path", str(path)])
    else:
        args.extend(["--image", "example:latest", "--platform", "linux/arm64"])
    if chunked:
        args.append("--chunked")
    if mode == "json":
        args.append("--json")
    if mode == "disabled":
        args.append("--no-progress")
    if mode == "structured":
        if command == "layout":
            args.extend(["--telemetry-format", "json", "--time-scenarios"])
        else:
            configure_telemetry(TelemetryConfig(output_format="json", time_scenarios_enabled=True))
    with (
        patch(f"regshape.cli.{command}.ProgressDisplay", side_effect=display),
        patch("regshape.libs.docker.operations.export_image", side_effect=export),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        result = CliRunner().invoke(regshape, args)
    assert result.exit_code == 0, result.output
    assert len(registry.stored) == 1 and displays[0].completed == displays[0].skipped == 1
    methods = [method for method, _ in registry.calls]
    transferred = next(iter(registry.stored.values()))
    assert methods.count("HEAD") == 2 and methods.count("POST") == 1
    assert methods.count("PATCH") == ((len(transferred) + 6) // 7 if chunked else 0)
    assert methods.count("PUT") == (3 if command == "layout" else 2)
    assert all(not output.parent.exists() for output in exported)
    if mode == "json":
        document = json.loads(result.stdout)
        assert result.stderr == ""
        if command == "layout":
            assert set(document) == {"layout_path", "destination", "manifests", "summary"}
            assert document["summary"]["blobs_uploaded"] == 1 and document["summary"]["blobs_skipped"] == 3
        else:
            assert set(document) == {
                "image", "destination", "manifests_pushed", "blobs_uploaded", "blobs_skipped", "platform",
            }
            assert document["blobs_uploaded"] == document["blobs_skipped"] == 1
    assert "\x1b" not in result.stdout
    if mode == "tty":
        rows = [r for r in terminal.rows if r.startswith("Blob")]
        assert len(rows) == 2 and "[1 done/1 skipped]" in rows[-1]
        assert sum(r.startswith("Preparing") for r in terminal.rows) == 1
    elif mode == "structured":
        assert "\x1b" not in terminal.getvalue()
        assert all(isinstance(json.loads(line), dict) for line in terminal.getvalue().splitlines())
    else:
        assert terminal.getvalue() == ""


@pytest.mark.parametrize("failure", ["export", "manifest", "cancel"])
def test_docker_failure_cleans_preparation_and_temp_layout(tmp_path: Path, failure: str) -> None:
    """Export and manifest failures never print overall success and always clean temporary data."""
    path, payloads = _layout(tmp_path, shared=False)
    registry = Registry(payloads, failure="manifest" if failure == "manifest" else None)
    terminal = Terminal()
    outputs: list[Path] = []

    def display(*, disabled: bool, structured: bool, total_blobs: int | None, show_counts: bool) -> ProgressDisplay:
        """Exercise the command's actual preparation and error context."""
        return ProgressDisplay(
            stream=terminal, columns=lambda: 160, disabled=disabled, structured=structured,
            total_blobs=total_blobs, show_counts=show_counts,
        )

    def export(image: str, output: Path, *, platform: str | None) -> None:
        """Record the owned temporary layout even when export fails."""
        outputs.append(output)
        if failure == "export":
            raise DockerError("synthetic export failure")
        if failure == "cancel":
            raise KeyboardInterrupt()
        shutil.copytree(path, output)

    with (
        patch("regshape.cli.docker.ProgressDisplay", side_effect=display),
        patch("regshape.libs.docker.operations.export_image", side_effect=export),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        result = CliRunner().invoke(regshape, [
            "docker", "push", "--image", "example:latest", "--dest", "registry.example.invalid/repo",
        ])
    assert result.exit_code == 1 and "Pushed" not in result.stdout
    assert ("Aborted" if failure == "cancel" else "Error") in result.stderr
    assert all(not output.parent.exists() for output in outputs)
    if failure == "export":
        assert "failed" in terminal.rows[0] and registry.calls == []
    if failure == "cancel":
        assert "cancelled" in terminal.rows[0] and registry.calls == []


def test_layout_hashing_and_upload_never_buffer_payload_files(tmp_path: Path) -> None:
    """Preflight and monolithic upload avoid the old whole-blob read path."""
    path, payloads = _layout(tmp_path)
    registry = Registry(payloads)
    original = Path.read_bytes

    def read_bytes(source: Path) -> bytes:
        """Allow manifest parsing but reject whole-buffer reads of any layer/config."""
        assert "sha256:" + source.name not in payloads
        return original(source)

    class BoundedReader(io.BytesIO):
        """Enforce the hash helper's explicit 64 KiB maximum read size."""

        def read(self, size: int = -1) -> bytes:
            """Reject unbounded or oversized hashing reads."""
            assert 0 < size <= 65536
            return super().read(size)

    content = b"x" * 200_000
    assert _hash_blob_stream(BoundedReader(content)) == "sha256:" + hashlib.sha256(content).hexdigest()
    with (
        patch.object(Path, "read_bytes", read_bytes),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
    ):
        result = push_layout(path, _client(), "repo", force=True)
    assert result.blobs_uploaded == 2


def test_docker_observer_errors_keep_identity_and_cleanup(tmp_path: Path) -> None:
    """The Docker orchestration forwards observer errors without wrapping or leaking its temporary layout."""
    path, payloads = _layout(tmp_path, shared=False)
    registry = Registry(payloads)
    outputs: list[Path] = []
    error = RuntimeError("observer failure")

    def export(image: str, output: Path, *, platform: str | None) -> None:
        """Create a real temporary layout at the daemon boundary."""
        outputs.append(output)
        shutil.copytree(path, output)

    def observe(event: TransferProgress) -> None:
        """Fail on the first consumed bytes, not on preparation."""
        if event.bytes_completed:
            raise error

    with (
        patch("regshape.libs.docker.operations.export_image", side_effect=export),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.libs.decorators.call_details.requests.request", side_effect=registry.respond),
        pytest.raises(RuntimeError) as caught,
    ):
        push_image("example:latest", "registry.example.invalid/repo", transfer_callback=observe)
    assert caught.value is error and all(not output.parent.exists() for output in outputs)


@pytest.mark.parametrize("stage", ["start", "done-label", "done-newline"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_preparation_render_failure_keeps_one_non_success_row(
    stage: str, error_type: type[BaseException],
) -> None:
    """Preparation failures survive nested telemetry without retaining a false done row."""
    terminal = Terminal()
    config = TelemetryConfig(output=terminal, time_scenarios_enabled=True)
    configure_telemetry(config)
    original_write, original_flush = terminal.write, terminal.flush
    error = error_type("preparation rendering")
    failed = False

    def write(text: str) -> int:
        """Fail once on either the initial or successful preparation label."""
        nonlocal failed
        target = "Preparing Docker image ..." + (" done" if stage == "done-label" else "")
        if stage != "done-newline" and text == target and not failed:
            failed = True
            raise error
        return original_write(text)

    def flush() -> None:
        """Fail after a completed preparation row has advanced the cursor."""
        nonlocal failed
        if stage == "done-newline" and not failed and terminal.getvalue().endswith("\n") and any(
            row == "Preparing Docker image ... done" for row in terminal.rows
        ):
            failed = True
            raise error
        original_flush()

    @track_scenario("begin upload")
    def begin(display: ProgressDisplay) -> None:
        """Use the first typed observation to finish the preparation phase."""
        display.observe(TransferProgress(
            "first", "sha256:" + "a" * 64, TransferDirection.UPLOAD,
            TransferPhase.PREPARING, 0, 10,
        ))

    with (
        pytest.raises(error_type) as caught,
        patch.object(terminal, "write", side_effect=write),
        patch.object(terminal, "flush", side_effect=flush),
        ProgressDisplay(stream=terminal, total_blobs=None) as display,
    ):
        display.prepare("Preparing Docker image")
        begin(display)
    assert caught.value is error and config.output is terminal
    rows = [r for r in terminal.rows if r.startswith("Preparing")]
    assert len(rows) == 1 and not rows[0].endswith("done")
    assert rows[0].endswith("failed" if error_type is OSError else "cancelled")
    assert not any(row.startswith("Blob") for row in terminal.rows)


def test_invalid_image_observers_fail_before_work() -> None:
    """Both public orchestration APIs reject noncallables before filesystem/export work."""
    with (
        patch("regshape.libs.layout.operations.validate_layout") as validate,
        pytest.raises(TypeError, match="callable"),
    ):
        push_layout("missing", None, "repo", transfer_callback=False)
    validate.assert_not_called()
    with (
        patch("regshape.libs.docker.operations.export_image") as export,
        pytest.raises(TypeError, match="callable"),
    ):
        push_image("example:latest", "registry.example.invalid/repo", transfer_callback=False)
    export.assert_not_called()

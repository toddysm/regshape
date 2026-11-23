"""Exercise registered component commands through actual SDK, transport and native storage.

Synthetic HTTP and shared terminal fixtures keep bytes, progress and fault
boundaries deterministic. Native success paths skip Windows; usage and the
unsupported-platform/preflight contracts remain executable everywhere.
"""

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests
from click.testing import CliRunner

from regshape.cli.downloads import DownloadWarnings
from regshape.cli.main import regshape
from regshape.cli.progress import ProgressDisplay
from regshape.libs.artifacts.storage import SafeDirectory
from regshape.libs.blobs import DownloadStats, DownloadStatus
from regshape.libs.errors import ArtifactError, BlobError, ComponentError
from regshape.libs.progress import TransferPhase
from regshape.tests.artifact_fixtures import (
    Registry,
    StreamResponse,
    address,
    document,
    edge,
)
from regshape.tests.download_fixture import DownloadFixture
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - minimum Click fixture
from regshape.tests.test_artifact_pull_cli import (
    ArtifactConsole,
    FaultLog,
    _console_streams,
)
from regshape.tests.test_manifest_cli_progress import (
    reset_telemetry as reset_telemetry,  # noqa: PLC0414 - isolate shared telemetry
)

_NATIVE = pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Native publication requires POSIX")
_SOURCE = "registry.example/repo:moving"
_CONFIG = b' { "opaque": "not reserialized; whitespace and missing newline preserved" }\n '
_LAYERS = (b"\x1f\x8bcompressed-looking\x00\xff", b"last layer")


def _registry(config: bytes = _CONFIG, layers: tuple[bytes, ...] = _LAYERS) -> Registry:
    """Build opaque component descriptors with independent bytes and observable request order."""
    root = document(
        config=edge(config, "application/example.config"),
        layers=[edge(raw, "application/example.layer") for raw in layers],
    )
    return Registry(root, {address(raw): raw for raw in (config, *layers)})


def _invoke(
    runner: CliRunner, console: ArtifactConsole, registry: Registry, output: Path, *,
    component: str = "config", index: int = 0, options: tuple[str, ...] = (),
    source: str = _SOURCE, global_options: tuple[str, ...] = (),
):
    """Run real command/SDK/client; substitute only HTTP, credentials and terminal channels."""
    def respond(method: str, url: str, **kwargs: Any) -> StreamResponse:
        """Route tag/default-tag/digest GETs to exact synthetic streams, never mock acquisition."""
        assert method == "GET"
        path = urlsplit(url).path
        if path.endswith("/latest"):
            path = path.removesuffix("/latest") + "/moving"
        return registry.get(path, **kwargs)

    with (
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("requests.request", side_effect=respond) as send,
        patch("requests.Session.request", side_effect=send),
        patch("regshape.cli.components.ProgressDisplay", side_effect=console.artifact_display),
    ):
        result = runner.invoke(regshape, [
            *global_options, component, "get", "-a", source, "-o", str(output),
            *(("--index", str(index)) if component == "layer" else ()), *options,
        ])
    return result, send


@pytest.mark.parametrize("component", ["config", "layer"])
def test_help_exposes_file_only_modern_resources(runner, component):
    """Registered resources advertise limits, embedded-only divergence and late-error obligations."""
    result = runner.invoke(regshape, [component, "get", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.stdout.split())
    assert "--artifact" in text and "--output" in text
    assert "--image-ref" not in text and "Deprecated" not in text and "--max-blob-bytes" not in text
    assert ("--index" in text) is (component == "layer")
    assert "registry blob endpoint" in text and "artifact pull" in text
    assert "Always check the exit status" in text and "partial metadata" in text


@pytest.mark.parametrize("component", ["config", "layer"])
@pytest.mark.parametrize("options", [
    (), ("-o", "out"), ("-a", _SOURCE), ("-a", _SOURCE, "-o", "-"),
    ("-a", _SOURCE, "-o", "out", "--chunk-size", "0"),
    ("-a", _SOURCE, "-o", "out", "--max-manifest-bytes", "0"),
    ("-a", _SOURCE, "-o", "out", "--max-total-bytes", "-1"),
    ("-a", _SOURCE, "-o", "out", "--sequential", "--concurrency", "4"),
    ("-a", _SOURCE, "-o", "out", "--legacy-transport", "--download-retries", "0"),
    ("-a", _SOURCE, "-o", "out", "--legacy-transport", "--scratch-dir", "."),
    ("-a", _SOURCE, "-o", "out", "--concurrency", "1", "--ranges-per-blob", "2"),
    ("-a", _SOURCE, "-o", "out", "--download-retries", "11"),
    ("-a", _SOURCE, "-o", "out", "--range-size", "65535"),
    ("-a", _SOURCE, "-o", "out", "-a", _SOURCE + "different"),
    ("--image-ref", _SOURCE, "-o", "out"), ("-i", _SOURCE, "-o", "out"),
    ("-a", "https://user:synthetic-secret@registry.example/repo", "-o", "out"),
])
def test_usage_rejection_precedes_preflight_and_credentials(runner, tmp_path, component, options):
    """Missing/invalid arguments and policy conflicts have no filesystem or credential effects."""
    with (
        patch("regshape.cli.components.preflight_component_download") as preflight,
        patch("regshape.cli.components.RegistryClient") as client,
    ):
        result = runner.invoke(regshape, [
            component, "get", *options, *(("--index", "0") if component == "layer" else ()),
        ])
    assert result.exit_code == 2, result.exception
    assert result.stdout == "" and "synthetic-secret" not in result.stderr
    preflight.assert_not_called()
    client.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("index", [None, "-1", "1.5", "not-an-index"])
def test_layer_index_is_required_nonnegative_integer(runner, index):
    """Invalid layer selection is a Click usage error before the shared SDK preflight."""
    with patch("regshape.cli.components.preflight_component_download") as preflight:
        result = runner.invoke(regshape, [
            "layer", "get", "-a", _SOURCE, "-o", "out", *(("--index", index) if index is not None else ()),
        ])
    assert result.exit_code == 2 and result.stdout == ""
    preflight.assert_not_called()


@_NATIVE
@pytest.mark.parametrize("kind", ["file", "directory", "dangling"])
@pytest.mark.parametrize("component", ["config", "layer"])
def test_existing_output_refused_before_credentials(runner, tmp_path, kind, component):
    """No-follow preflight retains existing entry identities and gives explicit recovery guidance."""
    output = tmp_path / "out"
    if kind == "file":
        output.write_bytes(b"caller owned")
    elif kind == "directory":
        output.mkdir()
    else:
        output.symlink_to(tmp_path / "missing")
    identity = output.lstat()
    with patch("regshape.cli.components.RegistryClient") as client:
        result = runner.invoke(regshape, [
            component, "get", "-a", _SOURCE, "-o", str(output),
            *(("--index", "0") if component == "layer" else ()),
        ])
    assert result.exit_code == 1 and result.stdout == ""
    assert "Choose a new --output path or explicitly remove" in result.stderr
    assert os.path.samestat(identity, output.lstat())
    client.assert_not_called()
    if kind == "file":
        assert output.read_bytes() == b"caller owned"
    assert not list(tmp_path.glob(".regshape-*"))


@pytest.mark.parametrize("component", ["config", "layer"])
def test_native_policy_error_is_formatted_before_client_creation(runner, tmp_path, component):
    """Unsupported native storage retains an operational exit rather than a traceback or false result."""
    with (
        patch("regshape.libs.components.require_archive_publication", side_effect=ArtifactError("unsupported")),
        patch("regshape.cli.components.RegistryClient") as client,
    ):
        result = runner.invoke(regshape, [
            component, "get", "-a", _SOURCE, "-o", str(tmp_path / "out"),
            *(("--index", "0") if component == "layer" else ()),
        ])
    assert result.exit_code == 1 and result.stdout == "" and "unsupported" in result.stderr
    client.assert_not_called()


@_NATIVE
@pytest.mark.parametrize("component,index", [("config", None), ("layer", 0), ("layer", 1)])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_exact_bytes_and_completed_eight_field_results(runner, tmp_path, component, index, pinned, structured):
    """Each tag/digest selection fetches one manifest and only its blob; metadata follows all cleanup."""
    registry, console, output = _registry(), ArtifactConsole(shared=False), tmp_path / "out"
    source = f"registry.example/repo@{address(registry.root)}" if pinned else _SOURCE
    payload = _CONFIG if component == "config" else _LAYERS[index]
    result, _ = _invoke(
        runner, console, registry, output, component=component, index=index,
        source=source, options=("--json",) if structured else (),
    )
    assert result.exit_code == 0, result.exception
    assert output.read_bytes() == payload
    assert len(registry.calls) == 2
    assert "/manifests/" in registry.calls[0] and registry.calls[1].endswith("/blobs/" + address(payload))
    assert all(response.close_calls == 1 for response in registry.responses)
    assert not list(tmp_path.glob(".regshape-*"))
    if structured:
        assert json.loads(console.out.text) == {
            "source": source, "resolved_reference": f"registry.example/repo@{address(registry.root)}",
            "component": component, "layer_index": index, "digest": address(payload), "size": len(payload),
            "media_type": f"application/example.{component}", "output_path": str(output),
        }
        assert console.err.text == ""
    else:
        selected = "config" if component == "config" else f"layer {index}"
        assert console.out.text == (
            f"Downloaded {selected}.\nSource: {source}\n"
            f"Resolved: registry.example/repo@{address(registry.root)}\nOutput: {output}\n"
            f"Digest: {address(payload)}\nSize: {len(payload)} bytes\n"
        )
        frames = ["\n".join(frame) for frame in console.terminal.frames]
        assert any("Manifest" in frame and "Processing" in frame for frame in frames)
        assert any(component.title() in frame and "Downloading" in frame for frame in frames)
        assert any(component.title() in frame and "Downloaded" in frame for frame in frames)
        assert any(f"{len(payload)} B / {len(payload)} B" in frame for frame in frames)
    assert "\x1b" not in console.out.text


@_NATIVE
@pytest.mark.parametrize("options,interactive", [
    ((), True), (("--no-progress",), True), (("--json",), True),
    (("--telemetry-format", "json", "--time-scenarios"), True), ((), False),
])
def test_progress_suppression_does_not_disable_zero_byte_verification(runner, tmp_path, options, interactive):
    """Even zero-byte config completes real typed receipt/publication when animation is disabled."""
    registry, console = _registry(config=b""), ArtifactConsole(shared=False, interactive=interactive)
    output = tmp_path / "empty"
    result, _ = _invoke(runner, console, registry, output, options=options)
    assert result.exit_code == 0, result.exception
    assert output.read_bytes() == b"" and len(registry.calls) == 2
    assert ("\x1b" in console.err.text) is (not options and interactive)
    if not options and interactive:
        assert any("Config" in row and "0 B / 0 B" in row and "Downloaded" in row for row in console.terminal.rows)
    if "--telemetry-format" in options:
        assert all(json.loads(line) for line in console.err.text.splitlines())
    assert "\x1b" not in console.out.text


@_NATIVE
@pytest.mark.parametrize("backend", ["--sequential", "--legacy-transport"])
def test_default_tag_and_identical_modern_selectors_share_sdk_policy(runner, tmp_path, backend):
    """Default latest and duplicate selectors resolve once with either supported serial policy."""
    source = "registry.example/repo"
    result, _ = _invoke(
        runner, ArtifactConsole(), _registry(), tmp_path / "out",
        source=source, options=(backend, "--json", "--artifact", source),
    )
    assert result.exit_code == 0, result.exception


@_NATIVE
@pytest.mark.parametrize("layers,index,message", [
    ((), 0, "manifest has no layers"),
    ((b"only",), 1, "valid index: 0"),
    ((b"first", b"last"), 2, "valid indexes: 0-1"),
])
def test_layer_selection_errors_are_actionable_and_do_not_acquire_blobs(runner, tmp_path, layers, index, message):
    """Empty/out-of-range SDK selection fails after one manifest, without output or false metadata."""
    registry, console = _registry(layers=layers), ArtifactConsole()
    result, _ = _invoke(runner, console, registry, tmp_path / "out", component="layer", index=index)
    assert result.exit_code == 1 and console.out.text == ""
    assert message in console.err.text and len(registry.calls) == 1
    assert not list(tmp_path.iterdir())


@_NATIVE
@pytest.mark.parametrize("component", ["config", "layer"])
def test_index_requires_explicit_child_manifest(runner, tmp_path, component):
    """An index is rejected by the SDK rather than silently choosing a platform."""
    registry, console = Registry(document(children=[])), ArtifactConsole()
    result, _ = _invoke(runner, console, registry, tmp_path / "out", component=component)
    assert result.exit_code == 1 and console.out.text == ""
    assert "explicit child-manifest digest" in console.err.text and len(registry.calls) == 1
    assert not list(tmp_path.iterdir())


@_NATIVE
@pytest.mark.parametrize("failure", [
    "missing", "corrupt", "size", "manifest", "manifest-limit", "budget", "embedded",
])
def test_acquisition_integrity_and_budget_failure_never_emit_results(runner, tmp_path, failure):
    """Real SDK failures preserve exact selected-byte checks and leave no owned incomplete output."""
    registry, console = _registry(), ArtifactConsole()
    if failure in ("missing", "embedded"):
        registry.objects.pop(address(_CONFIG))
        if failure == "embedded":
            registry.root = document(config=edge(_CONFIG, data=base64.b64encode(_CONFIG).decode()))
    elif failure == "corrupt":
        registry.objects[address(_CONFIG)] = b"x" * len(_CONFIG)
    elif failure == "size":
        registry.root = document(config=edge(_CONFIG, size=len(_CONFIG) + 1))
    elif failure == "manifest":
        registry.root = b"not a manifest"
    options = ("--json",)
    if failure == "budget":
        options += ("--max-total-bytes", str(len(registry.root) + len(_CONFIG) - 1))
    elif failure == "manifest-limit":
        options += ("--max-manifest-bytes", str(len(registry.root) - 1))
    result, _ = _invoke(runner, console, registry, tmp_path / "out", options=options)
    assert result.exit_code == 1 and console.out.text == ""
    assert "Error [" in console.err.text and not list(tmp_path.iterdir())
    if failure in ("missing", "embedded"):
        assert "embedded data is not a fallback" in console.err.text
    if failure in ("budget", "manifest-limit"):
        assert len(registry.calls) == 1


@_NATIVE
def test_total_budget_counts_manifest_and_selected_blob_at_exact_boundary(runner, tmp_path):
    """The approved total cap is inclusive, not a blob-only or artificial greater-than constraint."""
    registry, console = _registry(), ArtifactConsole()
    result, _ = _invoke(runner, console, registry, tmp_path / "out", options=(
        "--max-total-bytes", str(len(registry.root) + len(_CONFIG)), "--json",
    ))
    assert result.exit_code == 0, result.exception


@_NATIVE
@pytest.mark.parametrize("error_type", [ArtifactError, ComponentError, BlobError, OSError, requests.ConnectionError])
@pytest.mark.parametrize("callback", ["manifest", "blob", "status"])
def test_same_typed_callback_errors_keep_identity(runner, tmp_path, error_type, callback):
    """Callback errors must not be caught as operation failures even when sharing domain/I/O types."""
    registry, console = _registry(), ArtifactConsole()
    primary = error_type("synthetic observer fault")
    target = {
        "manifest": "regshape.cli.progress.ProgressDisplay.observe",
        "blob": "regshape.cli.progress.ProgressDisplay.observe_component",
        "status": "regshape.cli.downloads.DownloadWarnings.observe",
    }[callback]
    with patch(target, side_effect=primary):
        result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exit_code == 1 and result.exception is primary
    assert console.out.text == "" and not list(tmp_path.iterdir())


@_NATIVE
@pytest.mark.parametrize("fault", ["cancel", "timeout", "strict-parser"])
def test_primary_acquisition_errors_cleanup_and_exit_without_result(runner, tmp_path, fault):
    """Cancellation/transport/parser failures cannot leave partial files or successful metadata."""
    registry, console = _registry(), ArtifactConsole()
    original = registry.get

    def get(path: str, **kwargs: Any) -> StreamResponse:
        """Interrupt at blob acquisition after a manifest was actually received."""
        if "/blobs/" in path:
            if fault == "cancel":
                raise KeyboardInterrupt
            raise requests.Timeout("https://registry.example/?signature=synthetic-secret")
        return original(path, **kwargs)

    if fault == "strict-parser":
        registry.root = b'{"schemaVersion":2,"schemaVersion":2}'
    with patch.object(registry, "get", side_effect=get):
        result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exit_code == 1 and console.out.text == "" and not list(tmp_path.iterdir())
    assert "synthetic-secret" not in console.err.text
    if fault == "cancel":
        assert "Aborted!" in console.err.text


@_NATIVE
@pytest.mark.parametrize("fault,structured", [
    (fault, structured)
    for fault in ("callback", "renderer", "telemetry", "cleanup", "result", "partial-result")
    for structured in (False, True)
    if fault != "renderer" or not structured
])
def test_post_publication_failure_keeps_complete_file_without_early_result(runner, tmp_path, fault, structured):
    """Late failures never delete caller-owned output; result errors may retain a metadata prefix."""
    registry, console, output = _registry(), ArtifactConsole(), tmp_path / "out"
    primary = ComponentError("synthetic callback") if fault == "callback" else OSError("synthetic late fault")
    original_observe = ProgressDisplay.observe_component
    original_cleanup = SafeDirectory.cleanup

    def observe(display: ProgressDisplay, event, **kwargs: Any) -> None:
        """Fail the SDK's terminal notification only after it transferred output ownership."""
        if fault == "callback" and event.phase == TransferPhase.COMPLETED:
            assert output.read_bytes() == _CONFIG
            raise primary
        original_observe(display, event, **kwargs)

    def cleanup(directory: SafeDirectory) -> None:
        """Remove owned staging normally, then simulate a required operation cleanup failure."""
        original_cleanup(directory)
        if fault == "cleanup" and output.exists():
            raise primary

    if fault in ("result", "partial-result"):
        console.out.failure, console.out.partial = primary, 12 if fault == "partial-result" else 0
    elif fault == "renderer":
        console.err.failure = primary
        console.err.failure_when = lambda text: "Config" in text and "Downloaded" in text
    elif fault == "telemetry":
        console.err.failure = primary
        console.err.failure_when = lambda text: "telemetry" in text
    with (
        patch.object(ProgressDisplay, "observe_component", new=observe),
        patch.object(SafeDirectory, "cleanup", new=cleanup),
    ):
        result, _ = _invoke(
            runner, console, registry, output,
            options=(
                *(("--time-scenarios",) if fault == "telemetry" else ()),
                *(("--json",) if structured else ()),
            ),
        )
    assert result.exit_code == 1
    if fault != "cleanup":
        assert result.exception is primary
    assert output.read_bytes() == _CONFIG
    assert len(console.out.text) == (12 if fault == "partial-result" else 0)
    assert not list(tmp_path.glob(".regshape-*"))


@_NATIVE
@pytest.mark.parametrize("fault", ["flush", "warning"])
@pytest.mark.parametrize("structured", [False, True])
def test_final_flush_or_deferred_warning_can_leave_complete_metadata(runner, tmp_path, fault, structured):
    """Consumers must honor nonzero status even when a verified file and complete JSON both exist."""
    registry, console, output = _registry(), ArtifactConsole(shared=False), tmp_path / "out"
    primary = OSError("synthetic deferred sink failure")
    real_observe = DownloadWarnings.observe

    def changed(collector: DownloadWarnings, event: DownloadStatus) -> None:
        """Inject a typed fallback status while retaining the real SDK's acquisition and cleanup."""
        real_observe(collector, DownloadStatus(
            event.transfer_id or "component", address(_CONFIG),
            "fallback", "full", "etag_changed", None, 2, DownloadStats(),
        ))

    real_flush = console.out.flush
    metadata_flushes = 0

    def flush() -> None:
        """Fail once at the collector's final flush, after Click's own result flush succeeds."""
        nonlocal metadata_flushes
        if console.out.text:
            metadata_flushes += 1
            if fault == "flush" and metadata_flushes == 2:
                raise primary
        real_flush()

    if fault == "warning":
        console.err.failure = primary
        console.err.failure_when = lambda text: text.startswith("Warning: ETag")
    with patch.object(DownloadWarnings, "observe", new=changed), patch.object(console.out, "flush", side_effect=flush):
        result, _ = _invoke(
            runner, console, registry, output, options=("--json",) if structured else (),
        )
    assert result.exit_code == 1 and result.exception is primary
    assert output.read_bytes() == _CONFIG
    if structured:
        assert json.loads(console.out.text)["digest"] == address(_CONFIG)
    else:
        assert console.out.text.endswith(f"Size: {len(_CONFIG)} bytes\n")
    assert not list(tmp_path.glob(".regshape-*"))


@_NATIVE
@pytest.mark.parametrize("fault", ["flush", "close"])
def test_owned_telemetry_log_failure_precedes_result(runner, tmp_path, fault):
    """Owned telemetry resources close before final metadata, even under structured suppression."""
    registry, console, output = _registry(), ArtifactConsole(), tmp_path / "out"
    primary = OSError("synthetic log failure")
    log, path = FaultLog(fault, primary), tmp_path / "log"
    real_open = open

    def open_log(name, *args, **kwargs):
        """Substitute the telemetry file only; component files retain real storage semantics."""
        return log if name == str(path) else real_open(name, *args, **kwargs)

    with patch("builtins.open", side_effect=open_log):
        result, _ = _invoke(
            runner, console, registry, output, options=("--json", "--time-scenarios"),
            global_options=("--log-file", str(path)),
        )
    assert result.exit_code == 1 and result.exception is primary
    assert console.out.text == "" and log.close_attempted and output.read_bytes() == _CONFIG
    log.close()


@_NATIVE
@pytest.mark.parametrize("component", ["config", "layer"])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("backend", ["ranged", "sequential", "legacy"])
def test_loopback_download_controls_preserve_exact_bytes_and_drain_scratch(
    runner, tmp_path, component, pinned, backend,
):
    """Real loopback HTTP exercises both resources, tag/digest targets and range/serial policy."""
    payload = b"opaque compressed bytes\x00\xff" * 20000
    registry = _registry(config=payload, layers=(payload,))
    objects = {**registry.objects, "moving": registry.root}
    console, output, scratch = ArtifactConsole(shared=False), tmp_path / "out", tmp_path / "scratch"
    scratch.mkdir()
    options = {
        "ranged": ["--concurrency", "3", "--ranges-per-blob", "3", "--range-size", "65536",
                   "--download-retries", "0", "--scratch-dir", str(scratch)],
        "sequential": ["--sequential", "--download-retries", "0", "--scratch-dir", str(scratch)],
        "legacy": ["--legacy-transport"],
    }[backend]
    with (
        DownloadFixture(objects) as server,
        _console_streams(runner, console),
        patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)),
        patch("regshape.cli.components.ProgressDisplay", side_effect=console.artifact_display),
    ):
        source = f"{server.authority}/repo"
        source += "@" + address(registry.root) if pinned else ":moving"
        result = runner.invoke(regshape, [
            "--insecure", component, "get", "-a", source, "-o", str(output),
            *(("--index", "0") if component == "layer" else ()),
            "--chunk-size", "4096", "--json", *options,
        ])
        server.wait_idle()
    assert result.exit_code == 0, result.exception
    assert output.read_bytes() == payload and json.loads(console.out.text)["size"] == len(payload)
    assert any(interval is not None for _, interval in server.intervals) is (backend == "ranged")
    assert not list(scratch.iterdir()) and not list(tmp_path.glob(".regshape-*"))


@_NATIVE
def test_moving_tag_is_not_resolved_again_after_manifest_receipt(runner, tmp_path):
    """A tag move after its response cannot mix new descriptors into a selected layer."""
    registry, console = _registry(layers=(b"original only layer",)), ArtifactConsole()
    initial = registry.root

    def move(path):
        """Change only the mutable tag after its bytes were fixed in the HTTP response."""
        if "/manifests/" in path:
            registry.root = document(config=edge(b"new"), layers=[edge(b"replacement layer")])

    registry.after_get = move
    result, _ = _invoke(runner, console, registry, tmp_path / "out", component="layer", options=("--json",))
    assert result.exit_code == 0, result.exception
    assert (tmp_path / "out").read_bytes() == b"original only layer"
    assert json.loads(console.out.text)["resolved_reference"].endswith(address(initial))
    assert len(registry.calls) == 2


@_NATIVE
def test_secondary_status_callback_cannot_replace_primary_observer_identity(runner, tmp_path):
    """A later status sink failure cannot make the CLI translate a same-typed primary callback error."""
    registry, console = _registry(), ArtifactConsole()
    primary, secondary = ComponentError("primary observer fault"), OSError("secondary status fault")

    def status(collector, event):
        """Fail only failure/summary notifications emitted while the primary is being unwound."""
        if event.event in ("failed", "summary"):
            raise secondary

    with (
        patch.object(ProgressDisplay, "observe_component", side_effect=primary),
        patch.object(DownloadWarnings, "observe", new=status),
    ):
        result, _ = _invoke(runner, console, registry, tmp_path / "out")
    assert result.exception is primary and result.exit_code == 1
    assert console.out.text == "" and not list(tmp_path.iterdir())


@pytest.mark.parametrize("component", ["config", "layer"])
@pytest.mark.parametrize("source", [
    "registry.example/INVALID:tag", "registry.example/repo@sha256:abc",
    "registry.example:invalid/repo:tag", "registry.example/repo:",
])
def test_invalid_source_grammar_is_usage_before_storage_or_telemetry(runner, tmp_path, component, source):
    """Strict SDK source validation must classify usage before native probes, log creation or credentials."""
    with (
        patch.object(SafeDirectory, "open") as storage,
        patch("regshape.cli.components.RegistryClient") as client,
    ):
        result = runner.invoke(regshape, [
            "--log-file", str(tmp_path / "log"), component, "get", "-a", source,
            "-o", str(tmp_path / "out"), *(("--index", "0") if component == "layer" else ()),
        ])
    assert result.exit_code == 2 and result.stdout == ""
    assert "--artifact" in result.stderr
    storage.assert_not_called()
    client.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("component", ["config", "layer"])
@pytest.mark.parametrize("alias", [
    "same", "relative", "parent", "hardlink",
    pytest.param("dangling", marks=_NATIVE), pytest.param("symlink-parent", marks=_NATIVE),
])
def test_log_output_aliases_are_rejected_before_opening_either_path(
    runner, tmp_path, monkeypatch, component, alias,
):
    """Lexical/equivalent and existing hard-link aliases cannot create or append to the caller's output."""
    output = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    if alias == "hardlink":
        output.write_bytes(b"caller owned")
        log = tmp_path / "log"
        os.link(output, log)
    elif alias == "relative":
        log = "./out"
    elif alias == "parent":
        parent = tmp_path / "subdir"
        parent.mkdir()
        log = str(parent / ".." / "out")
    elif alias == "dangling":
        log = tmp_path / "log"
        log.symlink_to(output)
    elif alias == "symlink-parent":
        parent = tmp_path / "parent"
        parent.symlink_to(tmp_path, target_is_directory=True)
        log = parent / "out"
    else:
        log = output
    with patch("regshape.cli.components.RegistryClient") as client:
        result = runner.invoke(regshape, [
            "--log-file", str(log), component, "get", "-a", _SOURCE, "-o", str(output),
            *(("--index", "0") if component == "layer" else ()),
        ])
    assert result.exit_code == 2 and result.stdout == "" and "--log-file" in result.stderr
    client.assert_not_called()
    if alias == "hardlink":
        assert output.read_bytes() == b"caller owned" and log.read_bytes() == b"caller owned"
    else:
        assert not output.exists()


@pytest.mark.parametrize("component", ["config", "layer"])
@pytest.mark.parametrize("error", [OSError("cannot inspect path"), RuntimeError("symlink loop")])
def test_log_output_comparison_errors_are_explicit_before_telemetry(runner, tmp_path, component, error):
    """Uninspectable aliases fail closed with a runtime diagnostic and no files or credentials."""
    with (
        patch("regshape.cli.components.Path.resolve", side_effect=error),
        patch("regshape.cli.components.RegistryClient") as client,
    ):
        result = runner.invoke(regshape, [
            "--log-file", str(tmp_path / "log"), component, "get", "-a", _SOURCE,
            "-o", str(tmp_path / "out"), *(("--index", "0") if component == "layer" else ()),
        ])
    assert result.exit_code == 1 and result.stdout == ""
    assert "--log-file" in result.stderr and str(error) in result.stderr
    client.assert_not_called()
    assert not list(tmp_path.iterdir())


@_NATIVE
@pytest.mark.parametrize("component", ["config", "layer"])
@pytest.mark.parametrize("sink", ["log", "stderr"])
@pytest.mark.parametrize("error_type", [OSError, ArtifactError, ComponentError, requests.ConnectionError])
@pytest.mark.parametrize("mode", ["interactive", "no-progress", "json", "noninteractive"])
def test_debug_telemetry_sink_failure_during_manifest_retains_identity(
    runner, tmp_path, component, sink, error_type, mode,
):
    """HTTP debug failures retain sink identity whether progress is enabled or suppressed."""
    registry, output = _registry(), tmp_path / "out"
    console = ArtifactConsole(interactive=mode != "noninteractive")
    primary = error_type("synthetic debug sink failure")
    log, log_path, real_open = FaultLog("write" if sink == "log" else "", primary), tmp_path / "log", open

    def open_log(name, *args, **kwargs):
        """Substitute only the owned debug log; component staging remains native."""
        return log if name == str(log_path) else real_open(name, *args, **kwargs)

    if sink == "stderr":
        console.err.failure = primary
        console.err.failure_when = lambda text: "registry.example" in text
    with patch("builtins.open", side_effect=open_log):
        result, send = _invoke(
            runner, console, registry, output, component=component,
            options=("--debug-calls", "--legacy-transport",
                     *((f"--{mode}",) if mode in ("no-progress", "json") else ())),
            global_options=("--log-file", str(log_path)),
        )
    assert send.called and result.exit_code == 1 and result.exception is primary
    assert console.out.text == "" and not output.exists() and log.close_attempted
    assert all(response.close_calls == 1 for response in registry.responses)
    assert not list(tmp_path.glob(".regshape-*"))


@_NATIVE
@pytest.mark.parametrize("component", ["config", "layer"])
def test_terminal_callback_interruption_keeps_complete_output_without_metadata(runner, tmp_path, component):
    """Interruption after publication is nonzero and cannot delete complete caller-owned bytes."""
    registry, console, output = _registry(), ArtifactConsole(), tmp_path / "out"
    original = ProgressDisplay.observe_component

    def observe(display, event, **kwargs):
        """Cancel only the SDK's post-cleanup completion notification."""
        if event.phase == TransferPhase.COMPLETED:
            raise KeyboardInterrupt
        original(display, event, **kwargs)

    with patch.object(ProgressDisplay, "observe_component", new=observe):
        result, _ = _invoke(runner, console, registry, output, component=component)
    assert result.exit_code == 1 and console.out.text == "" and "Aborted!" in console.err.text
    assert output.read_bytes() == (_CONFIG if component == "config" else _LAYERS[0])
    assert not list(tmp_path.glob(".regshape-*"))

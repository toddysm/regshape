"""Thin config/layer resource commands sharing SDK preflight and result lifetimes.

The SDK owns selection, acquisition, integrity, publication and cleanup. This
module only declares options, adapts typed observations and emits metadata after
display/scenario/telemetry cleanup. Complete public files are never removed here.
"""

from collections.abc import Callable
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

import click
import requests

from regshape.cli.downloads import (
    DownloadWarnings,
    _DownloadOutput,
    download_options,
    download_policy,
)
from regshape.cli.formatting import emit_error, emit_json, emit_text
from regshape.cli.options import artifact_option
from regshape.cli.progress import ProgressDisplay
from regshape.cli.telemetry import telemetry_options
from regshape.libs.artifacts import ArtifactLimits
from regshape.libs.blobs import DownloadOptions, DownloadStatus
from regshape.libs.components import (
    Component,
    ComponentDownloadResult,
    download_config,
    download_layer,
    preflight_component_download,
    validate_component_source,
)
from regshape.libs.decorators import get_telemetry_config
from regshape.libs.decorators.output import flush_telemetry
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import ArtifactError, AuthError, BlobError, ManifestError
from regshape.libs.progress import ManifestPhase, ManifestProgress, TransferProgress
from regshape.libs.refs import parse_artifact_ref
from regshape.libs.transport import RegistryClient, TransportConfig

_DEFAULTS = ArtifactLimits()
_Event = TypeVar("_Event")
_HELP = """Save exact verified component bytes from a simple artifact manifest.

The output must be a new file under an existing trusted parent; no overwrite,
stdout payload, extraction or decompression. Native publication requires Linux
or macOS; Windows fails closed. Index targets require an explicit child-manifest
digest. Credentials are resolved automatically.

The selected blob must exist at the registry blob endpoint. Embedded descriptor
data and URLs are not fallbacks: use artifact pull for embedded-only content, or
publish the component blob first.

Late callback, cleanup, renderer or telemetry failure can leave a complete file.
Result-writing failure can leave partial metadata; final stdout flush or deferred
warning failure can leave complete metadata. Always check the exit status, not
file existence or parseable JSON. Complete published files are never deleted.
"""


class _ComponentCommandFailure(Exception):
    """Carry expected operation errors through renderer/scenario cleanup before formatting."""


class _ComponentObservers:
    """Adapt shared typed observations while retaining exact callback-error identity."""

    def __init__(
        self, display: ProgressDisplay, component: Component, warnings: DownloadWarnings,
    ) -> None:
        """Borrow invocation sinks and track whether manifest processing has finished."""
        self.display, self.component, self.warnings = display, component, warnings
        self.errors: list[BaseException] = []
        self.blob_started = False

    def _deliver(self, callback: Callable[[_Event], None], event: _Event) -> None:
        """Identify sink exceptions so operation-domain catches never translate them."""
        try:
            callback(event)
        except BaseException as error:
            self.errors.append(error)
            raise

    def manifest(self, event: ManifestProgress) -> None:
        """Render actual manifest receipt and begin processing without asserting selection success."""
        self._deliver(self._manifest, event)

    def _manifest(self, event: ManifestProgress) -> None:
        """Forward receipt and advance the shared command-owned processing stage."""
        self.display.observe(event)
        if event.phase == ManifestPhase.RECEIVED:
            self.display.processing_manifest()

    def blob(self, event: TransferProgress) -> None:
        """Finish manifest processing only when SDK blob receipt begins; forward absolute bytes."""
        self._deliver(self._blob, event)

    def _blob(self, event: TransferProgress) -> None:
        """Label the selected config/layer without inspecting descriptors or acquiring content."""
        if not self.blob_started:
            self.display.writing_manifest()
            self.display.finish_manifest()
            self.blob_started = True
        self.display.observe_component(event, component=self.component)

    def status(self, event: DownloadStatus) -> None:
        """Collect deferred safe warnings even when animation is suppressed."""
        self._deliver(self.warnings.observe, event)


def _after_telemetry(
    function: Callable[..., ComponentDownloadResult],
) -> Callable[..., None]:
    """Validate source/destination separation before telemetry, then emit only after cleanup."""
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        """Write completed scalar metadata, retaining shared deferred-warning/error semantics."""
        try:
            source = parse_artifact_ref(kwargs["image_ref"])
            validate_component_source(*source)
        except ValueError as error:
            raise click.BadParameter(
                "Use a valid artifact registry, repository and tag or SHA digest, not a URL.",
                param_hint="--artifact",
            ) from error
        try:
            _separate_log_output(kwargs["output"])
        except (OSError, RuntimeError) as error:
            emit_error("--log-file", f"Cannot inspect log/output separation: {error}")
        with DownloadWarnings() as warnings:
            result = function(*args, **kwargs, parsed_source=source, download_warnings=warnings)
            try:
                if kwargs["json_output"]:
                    emit_json(asdict(result))
                else:
                    selected = "config" if result.component == "config" else f"layer {result.layer_index}"
                    emit_text(
                        f"Downloaded {selected}.\n"
                        f"Source: {result.source}\n"
                        f"Resolved: {result.resolved_reference}\n"
                        f"Output: {result.output_path}\n"
                        f"Digest: {result.digest}\n"
                        f"Size: {result.size} bytes"
                    )
            except BaseException:
                warnings.result_failed()
                raise
    return wrapper


def _separate_log_output(output: str) -> None:
    """Reject equivalent log/output paths before telemetry can create or append to either.

    Resolve parent/symlink spellings and detect existing hard links. This is a
    usage check, not a reservation against concurrent namespace changes.
    """
    ctx = click.get_current_context()
    log_path = ctx.obj.get("log_file") if ctx.obj else None
    if not log_path:
        return
    target, log = Path(output).resolve(), Path(log_path).resolve()
    if target == log or (target.exists() and log.exists() and target.samefile(log)):
        raise click.BadParameter(
            "Telemetry log and component output must name different files.", param_hint="--log-file",
        )


def _operation(
    ctx: click.Context, registry: str, repo: str, reference: str, output: str,
    component: Component, index: int | None, limits: ArtifactLimits, chunk_size: int,
    policy: DownloadOptions, observers: _ComponentObservers,
) -> ComponentDownloadResult:
    """Invoke the complete SDK, formatting only operation-origin expected failures.

    Callback, telemetry, cancellation and unexpected exceptions retain identity,
    including diagnostic failures when progress is suppressed. Transport messages
    are omitted because redirect URLs can contain signed credentials.
    """
    config = get_telemetry_config()
    original_log = config.log_file
    log_output = _DownloadOutput(original_log) if original_log is not None else None
    if log_output is not None:
        config.log_file = log_output
    try:
        client = RegistryClient(TransportConfig(
            registry=registry, insecure=ctx.obj.get("insecure", False) if ctx.obj else False,
        ))
        status = None if policy.transport == "legacy" else observers.status
        if component == "config":
            return download_config(
                client, repo, reference, output, limits=limits, chunk_size=chunk_size,
                download_options=policy, progress_callback=observers.blob,
                manifest_progress_callback=observers.manifest, download_status_callback=status,
            )
        if index is None:
            raise AssertionError("Click must supply the required layer index")
        return download_layer(
            client, repo, reference, output, layer_index=index, limits=limits, chunk_size=chunk_size,
            download_options=policy, progress_callback=observers.blob,
            manifest_progress_callback=observers.manifest, download_status_callback=status,
        )
    except (ArtifactError, AuthError, BlobError, ManifestError, requests.RequestException, OSError) as error:
        if (
            any(error is observed for observed in observers.errors)
            or observers.display.sink_failed
            or (isinstance(config.output, _DownloadOutput) and config.output.failure is error)
            or (log_output is not None and log_output.failure is error)
        ):
            raise
        if isinstance(error, requests.RequestException):
            message = f"Transport failed ({type(error).__name__})"
        else:
            message = str(error)
        if isinstance(error, BlobError) and error.status_code == 404:
            message += (
                ". The selected blob must exist at the registry blob endpoint; embedded data "
                "is not a fallback. Use artifact pull for embedded-only content or publish the blob first."
            )
        raise _ComponentCommandFailure(message) from error
    finally:
        config.log_file = original_log


def _get_command(component: Component) -> click.Command:
    """Declare identical resource policy/lifetimes; only layer has a required zero-based index."""
    @click.command("get", help=_HELP)
    @_after_telemetry
    @telemetry_options
    @artifact_option(
        help_text="Artifact source with embedded registry (registry/repo:tag or registry/repo@sha256:...).",
        legacy_aliases=False,
    )
    @click.option("--output", "-o", required=True, type=click.Path(),
                  help="New output file; parent must exist. Never overwrites; '-' is unsupported.")
    @click.option("--chunk-size", type=click.IntRange(min=1), default=65536, show_default=True,
                  help="Streaming I/O buffer bytes, not HTTP range size.")
    @click.option("--max-manifest-bytes", type=click.IntRange(min=1),
                  default=_DEFAULTS.max_manifest_bytes, show_default=True,
                  help="Maximum verified manifest bytes.")
    @click.option("--max-total-bytes", type=click.IntRange(min=1),
                  default=_DEFAULTS.max_total_bytes, show_default=True,
                  help="Maximum unique verified manifest plus selected blob bytes.")
    @click.option("--json", "json_output", is_flag=True,
                  help="Print exactly eight completed metadata fields; disable animation.")
    @click.option("--no-progress", is_flag=True, help="Disable interactive transfer progress.")
    @download_options
    @click.pass_context
    def get(
        ctx: click.Context, image_ref: str, output: str, chunk_size: int,
        max_manifest_bytes: int, max_total_bytes: int, json_output: bool, no_progress: bool,
        download_warnings: DownloadWarnings,
        parsed_source: tuple[str, str, str],
        concurrency: int, ranges_per_blob: int, range_size: int, sequential: bool,
        legacy_transport: bool, download_retries: int | None, scratch_dir: str | None,
        index: int | None = None,
    ) -> ComponentDownloadResult:
        """Preflight before client creation, then return completed SDK metadata after sink cleanup."""
        policy = download_policy(
            ctx, concurrency=concurrency, ranges_per_blob=ranges_per_blob,
            range_size=range_size, sequential=sequential, legacy_transport=legacy_transport,
            download_retries=download_retries, scratch_dir=scratch_dir,
        )
        if output == "-":
            raise click.BadParameter("Choose a new file; stdout payload is unsupported.", param_hint="--output")
        registry, repo, reference = parsed_source
        limits = ArtifactLimits(max_manifest_bytes=max_manifest_bytes, max_total_bytes=max_total_bytes)
        try:
            preflight_component_download(
                registry, repo, reference, output, component=component, layer_index=index, limits=limits,
            )
        except (ArtifactError, OSError) as error:
            emit_error(image_ref, str(error))
        except ValueError as error:
            raise click.BadParameter(str(error)) from error

        @track_scenario(f"{component} get")
        def acquire(progress: ProgressDisplay) -> ComponentDownloadResult:
            """Keep scenario telemetry inside the display, outside the expected-operation catch."""
            return _operation(
                ctx, registry, repo, reference, output, component, index, limits, chunk_size,
                policy, _ComponentObservers(progress, component, download_warnings),
            )

        try:
            with ProgressDisplay(disabled=no_progress, structured=json_output) as progress:
                download_warnings.display = progress
                result = acquire(progress)
                flush_telemetry()
            return result
        except _ComponentCommandFailure as error:
            emit_error(image_ref, str(error))
            raise AssertionError("emit_error must exit") from error

    if component == "layer":
        get.params.append(click.Option(
            ("--index",), required=True, type=click.IntRange(min=0),
            help="Zero-based layer position in manifest order; a single layer has index 0.",
        ))
    return get


@click.group()
def config() -> None:
    """Download config content from a simple artifact manifest."""


@click.group()
def layer() -> None:
    """Download a zero-based content layer from a simple artifact manifest."""


config.add_command(_get_command("config"))
layer.add_command(_get_command("layer"))

"""Complete artifact pulls with shared per-object progress and post-publication results.

Preflight precedes credential lookup. Object receipt/storage, local packaging
and publication have distinct lifetimes; stdout is emitted only after the
display and telemetry (including owned log files) finish successfully.
"""

from collections.abc import Callable
from dataclasses import fields
from functools import wraps
from typing import Any

import click
import requests

from regshape.cli.formatting import emit_error, emit_json, emit_text
from regshape.cli.options import artifact_option
from regshape.cli.progress import ProgressDisplay
from regshape.libs.artifacts import (
    ArtifactFormat,
    ArtifactLimits,
    ArtifactPullResult,
    preflight_artifact_pull,
    pull_artifact,
)
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.output import flush_telemetry
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import ArtifactError, AuthError, BlobError, ManifestError
from regshape.libs.refs import parse_artifact_ref
from regshape.libs.transport import RegistryClient, TransportConfig

_DEFAULTS = ArtifactLimits()


def _result_data(result: ArtifactPullResult) -> dict[str, Any]:
    """Serialize only completed result fields, using the descriptor's wire representation."""
    data = {field.name: getattr(result, field.name) for field in fields(result)}
    data["root"] = result.root.to_dict()
    data["output_format"] = result.output_format.value
    return data


def _after_telemetry(func: Callable[..., ArtifactPullResult]) -> Callable[..., None]:
    """Delay stdout until the wrapped telemetry/display scopes close without error.

    :param func: Command callback wrapped by ``telemetry_options`` returning a
        completed pull; Click must supply its ``json_output`` keyword.
    :returns: Callback emitting one result. Output errors propagate unchanged;
        the library has already transferred complete output ownership to the user.
    """
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        """Print the completed result after required diagnostics and log close, never before."""
        result = func(*args, **kwargs)
        if kwargs["json_output"]:
            emit_json(_result_data(result))
        else:
            emit_text(
                "Downloaded complete artifact.\n"
                f"Source: {result.source}\n"
                f"Resolved: {result.resolved_reference}\n"
                f"Output: {result.output_path}\n"
                f"Format: {result.output_format.value}\n"
                f"Objects: {result.indexes} indexes, {result.manifests} manifests, {result.blobs} blobs\n"
                f"Shared references reused: {result.reused_references}"
            )
    return wrapper


def _source(value: str) -> tuple[str, str, str]:
    """Parse a shared selector without echoing URL/userinfo-like invalid inputs.

    Full tag/digest/repository/authority validation belongs to library preflight.
    This guard prevents the permissive shared parser from discarding URL syntax.
    """
    if (
        not value or "://" in value or value.count("@") > 1
        or ("@" in value and ":" not in value.rsplit("@", 1)[1])
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\?#%")
    ):
        raise click.BadParameter("Use a registry/repository tag or digest, not a URL.", param_hint="--artifact")
    try:
        return parse_artifact_ref(value)
    except ValueError as error:
        raise click.BadParameter(
            "Artifact must embed a registry and repository.", param_hint="--artifact",
        ) from error


@click.group()
def artifact() -> None:
    """Download complete OCI artifacts, including every platform and explicit subject."""


@artifact.command("pull")
@_after_telemetry
@telemetry_options
@artifact_option(help_text="Complete artifact source (registry/repo:tag or registry/repo@sha256:...).")
@click.option("--output", "-o", required=True, type=click.Path(),
              help="New OCI directory or gzip-tar path; parent must exist. Never overwrites.")
@click.option("--format", "output_format", type=click.Choice([item.value for item in ArtifactFormat]),
              default="oci", show_default=True, help="Explicit output format; suffixes do not select it.")
@click.option("--max-manifest-bytes", type=click.IntRange(min=1), default=_DEFAULTS.max_manifest_bytes,
              show_default=True, help="Maximum bytes per manifest/index, including the local wrapper.")
@click.option("--max-objects", type=click.IntRange(min=1), default=_DEFAULTS.max_objects,
              show_default=True, help="Maximum distinct content addresses.")
@click.option("--max-depth", type=click.IntRange(min=1), default=_DEFAULTS.max_depth,
              show_default=True, help="Maximum graph depth; selected root is depth zero.")
@click.option("--max-total-bytes", type=click.IntRange(min=1), default=_DEFAULTS.max_total_bytes,
              show_default=True, help="Maximum unique content bytes, excluding generated metadata.")
@click.option("--max-archive-bytes", type=click.IntRange(min=1), default=_DEFAULTS.max_archive_bytes,
              show_default=True, help="Maximum expanded tar bytes for archive output.")
@click.option("--max-archive-input-bytes", type=click.IntRange(min=1), default=_DEFAULTS.max_archive_input_bytes,
              show_default=True, help="Maximum compressed gzip bytes for archive output.")
@click.option("--json", "json_output", is_flag=True, help="Print completed result JSON; disable progress.")
@click.option("--no-progress", is_flag=True, help="Disable interactive transfer progress.")
@click.pass_context
def pull(
    ctx: click.Context, image_ref: str, output: str, output_format: str,
    max_manifest_bytes: int, max_objects: int, max_depth: int, max_total_bytes: int,
    max_archive_bytes: int, max_archive_input_bytes: int,
    json_output: bool, no_progress: bool,
) -> ArtifactPullResult:
    """Pull all indexes, manifests, configs, layers and explicit subjects without extraction.

    Native safe publication supports Linux and macOS only and requires a trusted
    private namespace: exclude hostile same-user/privileged access. Windows fails
    closed. The destination must be new under an existing, safe, trusted parent.
    Reverse referrer discovery and platform selection are not performed.

    \f
    :returns: Complete publication for the outer post-telemetry result writer.
    :raises click.BadParameter: For invalid source syntax or positive-integer policy.
    """
    registry, repo, reference = _source(image_ref)
    limits = ArtifactLimits(
        max_manifest_bytes=max_manifest_bytes, max_objects=max_objects,
        max_depth=max_depth, max_total_bytes=max_total_bytes,
        max_archive_bytes=max_archive_bytes, max_archive_input_bytes=max_archive_input_bytes,
    )
    selected_format = ArtifactFormat(output_format)
    try:
        preflight_artifact_pull(
            registry, repo, reference, output, output_format=selected_format, limits=limits,
        )
    except ValueError as error:
        raise click.BadParameter(str(error)) from error
    except (ArtifactError, OSError) as error:
        raise click.ClickException(str(error)) from error

    try:
        with ProgressDisplay(disabled=no_progress, structured=json_output, total_blobs=None) as progress:
            result = _pull_with_progress(
                ctx, registry, repo, reference, output, selected_format, limits, progress,
            )
            flush_telemetry()
            progress.finish_preparation()
        return result
    except (ArtifactError, AuthError, BlobError, ManifestError, requests.RequestException) as error:
        emit_error(image_ref, str(error))
        raise AssertionError("emit_error must exit") from error


@track_scenario("artifact pull")
def _pull_with_progress(
    ctx: click.Context, registry: str, repo: str, reference: str, output: str,
    output_format: ArtifactFormat, limits: ArtifactLimits, progress: ProgressDisplay,
) -> ArtifactPullResult:
    """Construct the client only after preflight and keep scenario diagnostics inside the display."""
    client = RegistryClient(TransportConfig(
        registry=registry, insecure=ctx.obj.get("insecure", False) if ctx.obj else False,
    ))
    result = pull_artifact(
        client, repo, reference, output, output_format=output_format, limits=limits,
        progress_callback=progress.observe, phase_callback=progress.artifact_phase,
    )
    progress.finish_preparation()
    return result

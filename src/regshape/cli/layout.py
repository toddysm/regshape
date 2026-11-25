#!/usr/bin/env python3

"""
:mod:`regshape.cli.layout` - CLI commands for OCI Image Layout operations
=========================================================================

.. module:: regshape.cli.layout
   :platform: Unix, Windows
   :synopsis: Click command groups providing ``init``, ``add layer``,
              ``annotate layer``, ``annotate manifest``, ``generate config``,
              ``generate manifest``, ``update config``, ``status``, ``show``,
              ``inspect`` and ``validate`` subcommands for creating and managing OCI Image
              Layouts via a staged build workflow.
              Inspection renders SDK-owned local occurrences without payload reads.
              Recursive validation opts into strict artifact graph policy.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json

import click
import requests

from regshape.cli.formatting import emit_error, emit_json, progress_status
from regshape.cli.layout_inspect import emit_inspection_diagnostics, write_inspection
from regshape.cli.progress import ProgressDisplay
from regshape.cli.telemetry import telemetry_options
from regshape.libs.artifacts.models import ArtifactLimits
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import (
    ArtifactError,
    AuthError,
    BlobError,
    LayoutError,
    LayoutInspectionError,
    ManifestError,
)
from regshape.libs.layout import (
    generate_config,
    generate_manifest,
    init_layout,
    inspect_layout,
    prepare_layer_content,
    read_index,
    read_stage,
    stage_layer,
    update_config_with_result,
    update_layer_annotations,
    update_manifest_annotations,
    validate_layout,
)
from regshape.libs.layout.operations import (
    LayoutPushPlan,
    execute_layout_push,
    parse_push_destination,
)
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_MANIFEST,
)
from regshape.libs.transport import RegistryClient
from regshape.libs.transport.client import TransportConfig

# ===========================================================================
# Private helpers
# ===========================================================================


def _parse_annotations(raw: tuple) -> dict:
    """Parse KEY=VALUE annotation pairs from Click option tuples."""
    result = {}
    for kv in raw:
        if "=" not in kv:
            raise click.BadParameter(
                f"Expected KEY=VALUE, got {kv!r}", param_hint="--annotation"
            )
        k, v = kv.split("=", 1)
        result[k] = v
    return result


# ===========================================================================
# Public Click group
# ===========================================================================


@click.group()
def layout():
    """Create and manage OCI Image Layouts on the local filesystem."""
    pass


# ===========================================================================
# Subgroups
# ===========================================================================


@layout.group("add")
def add():
    """Stage layer blobs in the layout."""
    pass


@layout.group("annotate")
def annotate():
    """Annotate staged layers or the manifest."""
    pass


@layout.group("generate")
def generate():
    """Generate config or manifest blobs from staged state."""
    pass


@layout.group("update")
def update():
    """Update a previously generated config or manifest blob."""
    pass


# ===========================================================================
# layout init
# ===========================================================================


@layout.command("init")
@telemetry_options
@click.option(
    "--path",
    "-p",
    "layout_path",
    required=True,
    type=click.Path(),
    metavar="DIR",
    help="Directory path to initialise as an OCI Image Layout.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout init")
def init_cmd(ctx, layout_path, as_json):
    """Initialise a new, empty OCI Image Layout at DIR."""
    try:
        init_layout(layout_path)
    except (LayoutError, OSError) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        emit_json({"layout_path": str(layout_path)})
    else:
        click.echo(f"Initialised OCI Image Layout at {layout_path}")


# ===========================================================================
# layout add layer
# ===========================================================================


@add.command("layer")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
    help="Root directory of an initialised OCI Image Layout.",
)
@click.option(
    "--file", "-f", "layer_file",
    required=True,
    type=click.Path(exists=True),
    metavar="FILE",
    help="Path to the layer content file.",
)
@click.option(
    "--compress-format",
    type=click.Choice(["gzip", "zstd"], case_sensitive=False),
    default=None,
    metavar="FORMAT",
    help="Force compression format. Auto-detected from file magic bytes when omitted.",
)
@click.option(
    "--media-type",
    default=None,
    metavar="MEDIA_TYPE",
    help="Override the layer media type. Derived from compression when omitted.",
)
@click.option(
    "--annotation", "raw_annotations",
    multiple=True,
    metavar="KEY=VALUE",
    help="Annotation to add to the layer descriptor. May be repeated.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout add layer")
def add_layer(ctx, layout_path, layer_file, compress_format, media_type, raw_annotations, as_json):
    """Read FILE, delegate layer preparation/staging, and render its descriptor."""
    try:
        with open(layer_file, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        emit_error(layer_file, str(exc))

    try:
        data, media_type = prepare_layer_content(
            data, compression=compress_format, media_type=media_type,
        )
        annotations = _parse_annotations(raw_annotations) if raw_annotations else None
        descriptor = stage_layer(layout_path, data, media_type, annotations=annotations)
    except (LayoutError, OSError, click.BadParameter) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        out: dict = {
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        emit_json(out)
    else:
        click.echo(f"Staged layer {descriptor.digest} ({descriptor.size} bytes)")


# ===========================================================================
# layout annotate layer
# ===========================================================================


@annotate.command("layer")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
)
@click.option(
    "--index", "-i", "layer_index",
    required=True,
    type=int,
    metavar="INDEX",
    help="Zero-based index of the staged layer to annotate.",
)
@click.option(
    "--annotation", "raw_annotations",
    multiple=True,
    metavar="KEY=VALUE",
    required=True,
    help="Annotation to set. May be repeated.",
)
@click.option("--replace", is_flag=True, default=False, help="Replace all existing annotations.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout annotate layer")
def annotate_layer(ctx, layout_path, layer_index, raw_annotations, replace, as_json):
    """Add or replace annotations on a staged layer descriptor."""
    try:
        annotations = _parse_annotations(raw_annotations)
        descriptor = update_layer_annotations(
            layout_path, layer_index, annotations, replace=replace
        )
    except (LayoutError, OSError, click.BadParameter) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        out: dict = {
            "index": layer_index,
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        emit_json(out)
    else:
        click.echo(f"Updated layer {layer_index}: {descriptor.digest}")


# ===========================================================================
# layout annotate manifest
# ===========================================================================


@annotate.command("manifest")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
)
@click.option(
    "--annotation", "raw_annotations",
    multiple=True,
    metavar="KEY=VALUE",
    required=True,
    help="Annotation to set. May be repeated.",
)
@click.option("--replace", is_flag=True, default=False, help="Replace all existing annotations.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout annotate manifest")
def annotate_manifest_cmd(ctx, layout_path, raw_annotations, replace, as_json):
    """Add or replace top-level annotations on the manifest blob."""
    try:
        annotations = _parse_annotations(raw_annotations)
        descriptor = update_manifest_annotations(layout_path, annotations, replace=replace)
    except (LayoutError, OSError, click.BadParameter) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        out: dict = {"digest": descriptor.digest, "size": descriptor.size}
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        emit_json(out)
    else:
        click.echo(f"Updated manifest annotations: {descriptor.digest}")


# ===========================================================================
# layout generate config
# ===========================================================================


@generate.command("config")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
)
@click.option(
    "--architecture",
    default=None,
    metavar="ARCH",
    help="Target CPU architecture (e.g. amd64, arm64). Prompts if omitted.",
)
@click.option(
    "--os", "os_name",
    default=None,
    metavar="OS",
    help="Target OS (e.g. linux, windows). Prompts if omitted.",
)
@click.option(
    "--media-type",
    default=None,
    metavar="MEDIA_TYPE",
    help="Config blob media type. Prompts if omitted.",
)
@click.option(
    "--annotation", "raw_annotations",
    multiple=True,
    metavar="KEY=VALUE",
    help="Annotation to add to the config descriptor.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout generate config")
def generate_config_cmd(
    ctx, layout_path, architecture, os_name, media_type, raw_annotations, as_json
):
    """Generate and store an OCI image config blob from the staged layers."""
    if architecture is None:
        architecture = click.prompt("Architecture", default="amd64")
    if os_name is None:
        os_name = click.prompt("OS", default="linux")
    if media_type is None:
        media_type = click.prompt("Media type", default=OCI_IMAGE_CONFIG)

    try:
        annotations = _parse_annotations(raw_annotations) if raw_annotations else None
        descriptor = generate_config(
            layout_path,
            architecture=architecture,
            os_name=os_name,
            media_type=media_type,
            annotations=annotations,
        )
    except (LayoutError, OSError, click.BadParameter) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        out: dict = {
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        emit_json(out)
    else:
        click.echo(f"Generated config {descriptor.digest} ({descriptor.size} bytes)")


# ===========================================================================
# layout generate manifest
# ===========================================================================


@generate.command("manifest")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
)
@click.option(
    "--ref-name",
    default=None,
    metavar="NAME",
    help="Human-readable reference name (e.g. latest). Prompts if omitted.",
)
@click.option(
    "--media-type",
    default=None,
    metavar="MEDIA_TYPE",
    help="Manifest media type. Prompts if omitted.",
)
@click.option(
    "--annotation", "raw_annotations",
    multiple=True,
    metavar="KEY=VALUE",
    help="Annotation to add to the manifest.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout generate manifest")
def generate_manifest_cmd(
    ctx, layout_path, ref_name, media_type, raw_annotations, as_json
):
    """Build and store an OCI image manifest from staged layers and config."""
    if ref_name is None:
        ref_name = click.prompt("Reference name (blank for none)", default="")
        ref_name = ref_name.strip() or None
    if media_type is None:
        media_type = click.prompt("Media type", default=OCI_IMAGE_MANIFEST)

    try:
        annotations = _parse_annotations(raw_annotations) if raw_annotations else None
        descriptor = generate_manifest(
            layout_path,
            ref_name=ref_name,
            media_type=media_type,
            annotations=annotations,
        )
    except (LayoutError, OSError, click.BadParameter) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        out: dict = {
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if ref_name:
            out["ref_name"] = ref_name
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        emit_json(out)
    else:
        ref_str = f" [{ref_name}]" if ref_name else ""
        click.echo(
            f"Generated manifest{ref_str} {descriptor.digest} ({descriptor.size} bytes)"
        )


# ===========================================================================
# layout update config
# ===========================================================================


@update.command("config")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
)
@click.option(
    "--architecture",
    default=None,
    metavar="ARCH",
    help="New CPU architecture value.",
)
@click.option(
    "--os", "os_name",
    default=None,
    metavar="OS",
    help="New OS value.",
)
@click.option(
    "--annotation", "raw_annotations",
    multiple=True,
    metavar="KEY=VALUE",
    help="Annotation to merge into the config.",
)
@click.option(
    "--replace-annotations",
    is_flag=True,
    default=False,
    help="Replace all existing config annotations.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout update config")
def update_config_cmd(
    ctx, layout_path, architecture, os_name, raw_annotations, replace_annotations, as_json
):
    """Update config fields and render the library's manifest-staleness warning."""
    try:
        annotations = _parse_annotations(raw_annotations) if raw_annotations else None
        result = update_config_with_result(
            layout_path,
            architecture=architecture,
            os_name=os_name,
            annotations=annotations,
            replace_annotations=replace_annotations,
        )
    except (LayoutError, OSError, click.BadParameter) as exc:
        emit_error(layout_path, str(exc))

    descriptor = result.descriptor
    if as_json:
        out: dict = {"digest": descriptor.digest, "size": descriptor.size}
        if result.manifest_stale:
            out["warning"] = (
                "Manifest exists and references the old config digest; "
                "regenerate or update it."
            )
        emit_json(out)
    else:
        click.echo(f"Updated config {descriptor.digest} ({descriptor.size} bytes)")
        if result.manifest_stale:
            click.echo(
                "Warning: manifest exists and references the old config digest.",
                err=True,
            )


# ===========================================================================
# layout status
# ===========================================================================


@layout.command("status")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout status")
def status(ctx, layout_path, as_json):
    """Show the current staging state of the layout."""
    try:
        stage = read_stage(layout_path)
    except (LayoutError, OSError) as exc:
        emit_error(layout_path, str(exc))

    if as_json:
        emit_json(stage)
    else:
        layers = stage.get("layers", [])
        config = stage.get("config")
        manifest = stage.get("manifest")
        click.echo(f"Layers staged: {len(layers)}")
        for i, layer in enumerate(layers):
            click.echo(
                f"  [{i}] {layer.get('digest', '?')} "
                f"({layer.get('size', '?')} bytes) "
                f"{layer.get('media_type', '')}"
            )
        config_str = "set -> " + config["digest"] if config else "not set"
        manifest_str = "set -> " + manifest["digest"] if manifest else "not set"
        click.echo(f"Config:   {config_str}")
        click.echo(f"Manifest: {manifest_str}")


# ===========================================================================
# layout show
# ===========================================================================


@layout.command("show")
@telemetry_options
@click.option(
    "--path",
    "-p",
    "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
    help="Root directory of an OCI Image Layout.",
)
@click.pass_context
@track_scenario("layout show")
def show(ctx, layout_path):
    """Print the index.json of an OCI Image Layout.

    Outputs the top-level OCI Image Index as pretty-printed JSON.
    """
    try:
        index = read_index(layout_path)
    except (LayoutError, OSError) as exc:
        emit_error(layout_path, str(exc))

    emit_json(json.loads(index.to_json()))


# ===========================================================================
# layout inspect
# ===========================================================================


@layout.command("inspect")
@telemetry_options
@click.option(
    "--path", "-p", "layout_path", required=True, type=str, metavar="DIR",
    help="Local OCI Image Layout directory to inspect without reading payloads.",
)
@click.option("--json", "as_json", is_flag=True, help="Output the SDK's hierarchical JSON.")
@click.option("--ascii", "ascii_only", is_flag=True, help="Use ASCII connectors and escaped values.")
@click.pass_context
@track_scenario("layout inspect")
def inspect_cmd(
    ctx: click.Context, layout_path: str, as_json: bool, ascii_only: bool
) -> None:
    """Inspect an expanded local tree, preserving partial SDK results and failure status.

    Incompatible flags fail before IO. Invalid roots/output caps produce no stdout;
    descendant diagnostics accompany the partial view on stderr and exit 1.
    Requested scenario timing uses library telemetry on stderr without changing
    tree/JSON stdout. Legacy show, status and validation remain independent.
    """
    if as_json and ascii_only:
        raise click.UsageError("--ascii cannot be combined with --json")
    if not layout_path or "\x00" in layout_path:
        raise click.BadParameter(
            "Path must be nonempty and contain no NUL characters", param_hint="--path"
        )
    try:
        result = inspect_layout(layout_path)
        write_inspection(result, as_json=as_json, ascii_only=ascii_only)
    except LayoutInspectionError as error:
        emit_inspection_diagnostics(error.diagnostics)
        ctx.exit(1)
    if not result.complete:
        ctx.exit(1)


# ===========================================================================
# layout validate
# ===========================================================================


@layout.command("validate")
@telemetry_options
@click.option("--recursive", is_flag=True, help="Verify the complete artifact graph.")
@click.option("--max-manifest-bytes", type=click.IntRange(min=1), default=None)
@click.option("--max-objects", type=click.IntRange(min=1), default=None)
@click.option("--max-depth", type=click.IntRange(min=1), default=None)
@click.option("--max-total-bytes", type=click.IntRange(min=1), default=None)
@click.option("--max-archive-bytes", type=click.IntRange(min=1), default=None)
@click.option("--max-archive-input-bytes", type=click.IntRange(min=1), default=None)
@click.option(
    "--path",
    "-p",
    "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="PATH",
    help="OCI layout directory, or gzip-tar archive with --recursive.",
)
@click.pass_context
@track_scenario("layout validate")
def validate(
    ctx, layout_path, recursive, max_manifest_bytes, max_objects, max_depth,
    max_total_bytes, max_archive_bytes, max_archive_input_bytes,
):
    """Validate the structural and content integrity of an OCI Image Layout.

    Default behavior remains shallow. --recursive verifies all platforms,
    explicit subjects and every config/layer from a safe directory/archive
    snapshot. Limits and gzip-tar input require --recursive.
    """
    values = {
        "max_manifest_bytes": max_manifest_bytes, "max_objects": max_objects,
        "max_depth": max_depth, "max_total_bytes": max_total_bytes,
        "max_archive_bytes": max_archive_bytes,
        "max_archive_input_bytes": max_archive_input_bytes,
    }
    supplied = {key: value for key, value in values.items() if value is not None}
    if supplied and not recursive:
        raise click.UsageError("Safety limits require --recursive")
    try:
        if recursive:
            validate_layout(layout_path, recursive=True, limits=ArtifactLimits(**supplied))
        else:
            validate_layout(layout_path)
    except (ArtifactError, LayoutError, OSError) as exc:
        emit_error(layout_path, str(exc))

    click.echo(f"Layout at {layout_path} is valid.")


# ===========================================================================
# layout push
# ===========================================================================


def _short_digest(digest: str) -> str:
    """Return a truncated digest for display."""
    if ":" in digest:
        return digest[:7 + 12]  # "sha256:" + 12 hex chars
    return digest[:12]


def _format_size(size: int) -> str:
    """Return a human-friendly byte-size string."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


@layout.command("push")
@telemetry_options
@click.option("--no-progress", is_flag=True, default=False, help="Disable interactive transfer progress.")
@click.option("--max-manifest-bytes", type=click.IntRange(min=1), default=None)
@click.option("--max-objects", type=click.IntRange(min=1), default=None)
@click.option("--max-depth", type=click.IntRange(min=1), default=None)
@click.option("--max-total-bytes", type=click.IntRange(min=1), default=None)
@click.option("--max-archive-bytes", type=click.IntRange(min=1), default=None)
@click.option("--max-archive-input-bytes", type=click.IntRange(min=1), default=None)
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="PATH",
    help="Complete OCI layout directory or supported gzip-tar archive.",
)
@click.option(
    "--dest", "-d", "dest",
    required=True,
    metavar="ARTIFACT",
    help="Destination artifact reference (registry/repo or registry/repo:tag).",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Skip blob existence checks and upload all blobs unconditionally.",
)
@click.option(
    "--chunked", is_flag=True, default=False,
    help="Use chunked (streaming) upload protocol for blobs.",
)
@click.option(
    "--chunk-size", type=click.IntRange(min=1), default=65536, show_default=True,
    metavar="BYTES",
    help="Chunk size in bytes (chunked mode only).",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print what would be pushed without making network calls.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
@track_scenario("layout push")
def push_cmd(
    ctx, layout_path, dest, force, chunked, chunk_size, dry_run, as_json, no_progress,
    max_manifest_bytes, max_objects, max_depth, max_total_bytes, max_archive_bytes,
    max_archive_input_bytes,
):
    """Push a complete directory/archive graph with locally verified references.

    Planning precedes client construction, including credential-helper access.
    Dry-run uses that same snapshot/plan and constructs no client. Interactive
    progress retains one row per unique blob; JSON/--no-progress suppress it.
    Native safe snapshots require Linux/Darwin; Windows fails closed.
    """
    try:
        registry, repo, tag_override = parse_push_destination(dest)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--dest") from exc
    values = {
        "max_manifest_bytes": max_manifest_bytes, "max_objects": max_objects,
        "max_depth": max_depth, "max_total_bytes": max_total_bytes,
        "max_archive_bytes": max_archive_bytes,
        "max_archive_input_bytes": max_archive_input_bytes,
    }
    limits = ArtifactLimits(**{key: value for key, value in values.items() if value is not None})
    dest_display = f"{registry}/{repo}"
    if tag_override:
        dest_display += f":{tag_override}"
    try:
        with LayoutPushPlan(layout_path, tag_override, limits=limits) as plan:
            if dry_run:
                _push_dry_run(plan, registry, repo, as_json)
                return
            insecure = ctx.obj.get("insecure", False) if ctx.obj else False
            client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))
            if not as_json:
                progress_status(f"Pushing layout {layout_path} -> {dest_display}\n")
            with ProgressDisplay(
                disabled=no_progress, structured=as_json, total_blobs=None, show_counts=True,
            ) as progress:
                progress.prepare("Preparing OCI layout")
                result = execute_layout_push(
                    plan, client, repo, force=force, chunked=chunked,
                    chunk_size=chunk_size, transfer_callback=progress.observe,
                )
    except (ArtifactError, LayoutError, AuthError, BlobError, ManifestError,
            OSError, requests.exceptions.RequestException) as exc:
        emit_error(dest_display, str(exc))

    # --- Output ---
    if as_json:
        output = {
            "layout_path": str(layout_path),
            "destination": result.destination,
            "manifests": [
                {
                    "digest": m.digest,
                    "reference": m.reference,
                    "media_type": m.media_type,
                    "blobs": [
                        {
                            "digest": b.digest,
                            "size": b.size,
                            "media_type": b.media_type,
                            "action": b.action,
                        }
                        for b in m.blobs
                    ],
                    "status": m.status,
                }
                for m in result.manifests
            ],
            "summary": {
                "manifests_pushed": result.manifests_pushed,
                "blobs_uploaded": result.blobs_uploaded,
                "blobs_skipped": result.blobs_skipped,
                "bytes_uploaded": result.bytes_uploaded,
            },
        }
        emit_json(output)
    else:
        progress_status(
            f"\nPush complete: {result.manifests_pushed} manifest(s), "
            f"{result.blobs_uploaded} blob(s) uploaded, "
            f"{result.blobs_skipped} blob(s) skipped."
        )


def _push_dry_run(plan: LayoutPushPlan, registry: str, repo: str, as_json: bool) -> None:
    """Render intended publications from the active verified plan, without registry access.

    Existing flat JSON keys are retained; media_type additionally identifies
    index reports (whose blobs list is empty). No acknowledged counts/actions
    are fabricated because existence checks and writes have not happened.
    """
    if as_json:
        output = {
            "dry_run": True,
            "layout_path": plan.layout_path,
            "destination": f"{registry}/{repo}",
            "manifests": [
                {
                    "digest": publication.document.digest,
                    "reference": publication.reference,
                    "media_type": publication.document.media_type,
                    "blobs": [
                        {"digest": blob.digest, "size": blob.size, "media_type": blob.media_type}
                        for blob in publication.blobs
                    ],
                }
                for publication in plan.publications
            ],
        }
        emit_json(output)
    else:
        click.echo(f"[dry-run] Layout {plan.layout_path} -> {registry}/{repo}\n")
        for blob in plan.blobs:
            click.echo(f"[dry-run] Would upload blob {_short_digest(blob.digest)} ({_format_size(blob.size)})")
        for publication in plan.publications:
            click.echo(
                f"[dry-run] Would push manifest {_short_digest(publication.document.digest)} "
                f"as '{publication.reference}'"
            )

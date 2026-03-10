#!/usr/bin/env python3

"""
:mod:`regshape.cli.layout` - CLI commands for OCI Image Layout operations
=========================================================================

.. module:: regshape.cli.layout
   :platform: Unix, Windows
   :synopsis: Click command groups providing ``init``, ``add layer``,
              ``annotate layer``, ``annotate manifest``, ``generate config``,
              ``generate manifest``, ``update config``, ``status``, ``show``,
              and ``validate`` subcommands for creating and managing OCI Image
              Layouts via a staged build workflow.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import gzip
import io
import json
import sys

import click

from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, BlobError, LayoutError, ManifestError
from regshape.libs.layout import (
    generate_config,
    generate_manifest,
    init_layout,
    push_layout,
    read_index,
    read_stage,
    stage_layer,
    update_config,
    update_layer_annotations,
    update_manifest_annotations,
    validate_layout,
)
from regshape.libs.refs import parse_image_ref
from regshape.libs.transport import RegistryClient
from regshape.libs.transport.client import TransportConfig
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_LAYER_TAR_ZSTD,
    OCI_IMAGE_MANIFEST,
)


# Compression magic bytes
_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


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


def _detect_compression(data: bytes) -> str:
    """Return 'gzip', 'zstd', or 'none'."""
    if data[:2] == _GZIP_MAGIC:
        return "gzip"
    if data[:4] == _ZSTD_MAGIC:
        return "zstd"
    return "none"


def _compress_gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(data)
    return buf.getvalue()


def _compress_zstd(data: bytes) -> bytes:
    try:
        import zstandard as zstd  # type: ignore[import]
    except ImportError as exc:
        raise click.ClickException(
            "zstandard package required for zstd compression: pip install zstandard"
        ) from exc
    cctx = zstd.ZstdCompressor()
    return cctx.compress(data)


def _media_type_for_compression(compression: str) -> str:
    if compression == "zstd":
        return OCI_IMAGE_LAYER_TAR_ZSTD
    return OCI_IMAGE_LAYER_TAR_GZIP


def _error(context: str, reason: str) -> None:
    """Print an error message to stderr."""
    click.echo(f"Error [{context}]: {reason}", err=True)


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
        _error(layout_path, str(exc))
        sys.exit(1)

    if as_json:
        click.echo(json.dumps({"layout_path": str(layout_path)}, indent=2))
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
    """Stage a layer blob in the layout from FILE."""
    try:
        with open(layer_file, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        _error(layer_file, str(exc))
        sys.exit(1)

    detected = _detect_compression(data)

    if compress_format == "gzip" and detected != "gzip":
        data = _compress_gzip(data)
        detected = "gzip"
    elif compress_format == "zstd" and detected != "zstd":
        try:
            data = _compress_zstd(data)
        except click.ClickException as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        detected = "zstd"
    elif compress_format is None and detected == "none":
        # Default: auto-compress uncompressed content with gzip
        data = _compress_gzip(data)
        detected = "gzip"

    if media_type is None:
        media_type = _media_type_for_compression(detected)

    try:
        annotations = _parse_annotations(raw_annotations) if raw_annotations else None
        descriptor = stage_layer(layout_path, data, media_type, annotations=annotations)
    except (LayoutError, OSError, click.BadParameter) as exc:
        _error(layout_path, str(exc))
        sys.exit(1)

    if as_json:
        out: dict = {
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        click.echo(json.dumps(out, indent=2))
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
        _error(layout_path, str(exc))
        sys.exit(1)

    if as_json:
        out: dict = {
            "index": layer_index,
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        click.echo(json.dumps(out, indent=2))
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
        _error(layout_path, str(exc))
        sys.exit(1)

    if as_json:
        out: dict = {"digest": descriptor.digest, "size": descriptor.size}
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        click.echo(json.dumps(out, indent=2))
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
        _error(layout_path, str(exc))
        sys.exit(1)

    if as_json:
        out: dict = {
            "digest": descriptor.digest,
            "size": descriptor.size,
            "media_type": descriptor.media_type,
        }
        if descriptor.annotations:
            out["annotations"] = descriptor.annotations
        click.echo(json.dumps(out, indent=2))
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
        _error(layout_path, str(exc))
        sys.exit(1)

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
        click.echo(json.dumps(out, indent=2))
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
    """Update the stored config blob (architecture, OS, or annotations)."""
    try:
        annotations = _parse_annotations(raw_annotations) if raw_annotations else None
        descriptor = update_config(
            layout_path,
            architecture=architecture,
            os_name=os_name,
            annotations=annotations,
            replace_annotations=replace_annotations,
        )
    except (LayoutError, OSError, click.BadParameter) as exc:
        _error(layout_path, str(exc))
        sys.exit(1)

    # Warn when a manifest already exists (it will reference the old config digest)
    try:
        stage = read_stage(layout_path)
        manifest_stale = stage.get("manifest") is not None
    except LayoutError:
        manifest_stale = False

    if as_json:
        out: dict = {"digest": descriptor.digest, "size": descriptor.size}
        if manifest_stale:
            out["warning"] = (
                "Manifest exists and references the old config digest; "
                "regenerate or update it."
            )
        click.echo(json.dumps(out, indent=2))
    else:
        click.echo(f"Updated config {descriptor.digest} ({descriptor.size} bytes)")
        if manifest_stale:
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
        _error(layout_path, str(exc))
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(stage, indent=2))
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
        _error(layout_path, str(exc))
        sys.exit(1)

    click.echo(json.dumps(json.loads(index.to_json()), indent=2))


# ===========================================================================
# layout validate
# ===========================================================================


@layout.command("validate")
@telemetry_options
@click.option(
    "--path",
    "-p",
    "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
    help="Root directory of an OCI Image Layout to validate.",
)
@click.pass_context
@track_scenario("layout validate")
def validate(ctx, layout_path):
    """Validate the structural and content integrity of an OCI Image Layout.

    Checks that all blobs referenced by ``index.json`` and all manifests
    within them are present and their digests match the declared values.
    """
    try:
        validate_layout(layout_path)
    except (LayoutError, OSError) as exc:
        _error(layout_path, str(exc))
        sys.exit(1)

    click.echo(f"Layout at {layout_path} is valid.")


# ===========================================================================
# layout push
# ===========================================================================


def _short_digest(digest: str) -> str:
    """Return a truncated digest for display."""
    if ":" in digest:
        return digest[:7 + 1 + 12]  # "sha256:" + 12 hex chars
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
@click.option(
    "--path", "-p", "layout_path",
    required=True,
    type=click.Path(exists=True),
    metavar="DIR",
    help="Root directory of a valid, completed OCI Image Layout.",
)
@click.option(
    "--dest", "-d", "dest",
    required=True,
    metavar="IMAGE_REF",
    help="Destination image reference (registry/repo or registry/repo:tag).",
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
    "--chunk-size", type=int, default=65536, show_default=True,
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
def push_cmd(ctx, layout_path, dest, force, chunked, chunk_size, dry_run, as_json):
    """Push an OCI Image Layout to a remote registry.

    Reads the layout's index.json, uploads all blobs (layers and config),
    then pushes each manifest to the destination registry.
    """
    # --- Parse destination ---
    try:
        registry, repo, reference = parse_image_ref(dest)
    except ValueError as exc:
        _error(layout_path, str(exc))
        sys.exit(1)

    tag_override = reference if reference != "latest" or ":" in dest or "@" in dest else None

    # --- Dry-run mode ---
    if dry_run:
        _push_dry_run(layout_path, registry, repo, tag_override, as_json)
        return

    # --- Build client ---
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False
    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    # --- Progress helpers ---
    use_progress = not as_json and sys.stderr.isatty()
    current_bar = [None]  # mutable wrapper for closure

    def progress_callback(event, **kwargs):
        digest = kwargs.get("digest", "")
        size = kwargs.get("size", 0)
        if event == "blob_start":
            label = f"  {_short_digest(digest)} ({_format_size(size)})"
            if use_progress:
                bar = click.progressbar(
                    length=size, label=label, file=sys.stderr,
                    width=36, show_pos=True,
                )
                bar.__enter__()
                current_bar[0] = bar
                # Immediately complete — we don't have per-byte callback from
                # the library upload, so show as complete once done.
            else:
                click.echo(f"  Uploading {_short_digest(digest)} ({_format_size(size)})...",
                           err=True)
        elif event == "blob_done":
            if use_progress and current_bar[0] is not None:
                bar = current_bar[0]
                bar.update(size)
                bar.__exit__(None, None, None)
                current_bar[0] = None
            else:
                click.echo(f"  Uploaded  {_short_digest(digest)}", err=True)
        elif event == "blob_skip":
            click.echo(f"  {_short_digest(digest)} ({_format_size(size)}) exists, skipping",
                       err=True)
        elif event == "manifest_done":
            ref = kwargs.get("reference", "")
            click.echo(f"  Manifest {_short_digest(digest)} -> {ref}  pushed",
                       err=True)

    # --- Execute push ---
    dest_display = f"{registry}/{repo}"
    if tag_override:
        dest_display += f":{tag_override}"

    if not as_json:
        click.echo(f"Pushing layout {layout_path} -> {dest_display}\n", err=True)

    try:
        result = push_layout(
            layout_path=layout_path,
            client=client,
            repo=repo,
            tag_override=tag_override,
            force=force,
            chunked=chunked,
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )
    except LayoutError as exc:
        _error(layout_path, str(exc))
        sys.exit(1)
    except (AuthError, BlobError, ManifestError) as exc:
        _error(dest_display, str(exc))
        sys.exit(1)

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
        click.echo(json.dumps(output, indent=2))
    else:
        click.echo(
            f"\nPush complete: {result.manifests_pushed} manifest(s), "
            f"{result.blobs_uploaded} blob(s) uploaded, "
            f"{result.blobs_skipped} blob(s) skipped.",
            err=True,
        )


def _push_dry_run(layout_path, registry, repo, tag_override, as_json):
    """Validate layout and print what would be pushed without network calls."""
    from regshape.libs.models.manifest import ImageManifest as _IM
    from regshape.libs.models.manifest import parse_manifest as _pm

    try:
        validate_layout(layout_path)
        index = read_index(layout_path)
    except LayoutError as exc:
        _error(layout_path, str(exc))
        sys.exit(1)

    if not index.manifests:
        _error(layout_path, "index.json contains no manifests")
        sys.exit(1)

    from regshape.libs.layout import read_blob as _rb

    results = []
    for entry in index.manifests:
        manifest_bytes = _rb(layout_path, entry.digest)
        manifest_obj = _pm(manifest_bytes.decode("utf-8"))
        blobs = []
        if isinstance(manifest_obj, _IM):
            for layer in manifest_obj.layers:
                blobs.append((layer.digest, layer.size, layer.media_type))
            blobs.append((manifest_obj.config.digest, manifest_obj.config.size,
                          manifest_obj.config.media_type))

        if tag_override:
            ref = tag_override
        elif entry.annotations and "org.opencontainers.image.ref.name" in entry.annotations:
            ref = entry.annotations["org.opencontainers.image.ref.name"]
        else:
            ref = entry.digest

        results.append((entry, ref, blobs))

    if as_json:
        output = {
            "dry_run": True,
            "layout_path": str(layout_path),
            "destination": f"{registry}/{repo}",
            "manifests": [
                {
                    "digest": entry.digest,
                    "reference": ref,
                    "blobs": [
                        {"digest": d, "size": s, "media_type": mt}
                        for d, s, mt in blobs
                    ],
                }
                for entry, ref, blobs in results
            ],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        click.echo(f"[dry-run] Layout {layout_path} -> {registry}/{repo}\n")
        for entry, ref, blobs in results:
            for digest, size, _mt in blobs:
                click.echo(f"[dry-run] Would upload blob {_short_digest(digest)} ({_format_size(size)})")
            click.echo(f"[dry-run] Would push manifest {_short_digest(entry.digest)} as '{ref}'")

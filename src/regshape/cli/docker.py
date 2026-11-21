#!/usr/bin/env python3

"""
:mod:`regshape.cli.docker` - CLI commands for Docker Desktop integration
========================================================================

.. module:: regshape.cli.docker
   :platform: Unix, Windows
   :synopsis: Click command group providing ``list``, ``export``, and ``push``
              subcommands for interacting with local Docker Desktop images.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click

from regshape.cli.formatting import emit_error, emit_json, format_size, short_digest
from regshape.cli.progress import ProgressDisplay
from regshape.libs.docker import export_image, list_images, push_image
from regshape.libs.errors import (
    ArtifactError,
    AuthError,
    BlobError,
    DockerError,
    LayoutError,
    ManifestError,
)

# ===========================================================================
# Public Click group
# ===========================================================================


@click.group()
def docker():
    """Interact with Docker Desktop local images."""
    pass


# ===========================================================================
# docker list
# ===========================================================================


@docker.command("list")
@click.option(
    "--filter", "-f", "name_filter",
    default=None,
    metavar="NAME",
    help="Filter images by name (substring match).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def list_cmd(ctx, name_filter, as_json):
    """List local Docker images, abbreviating SHA-256 IDs in table output only."""
    try:
        images = list_images(name_filter=name_filter)
    except DockerError as exc:
        emit_error("docker list", str(exc))

    if as_json:
        output = [
            {
                "id": img.id,
                "repo_tags": img.repo_tags,
                "repo_digests": img.repo_digests,
                "size": img.size,
                "created": img.created,
                "architecture": img.architecture,
                "os": img.os,
            }
            for img in images
        ]
        emit_json(output)
    else:
        if not images:
            click.echo("No images found.")
            return

        # Table header
        header = f"{'REPOSITORY':<30} {'TAG':<15} {'IMAGE ID':<14} {'SIZE':<10} {'CREATED'}"
        click.echo(header)

        for img in images:
            short_id = short_digest(img.id)
            size_str = format_size(img.size)
            if img.repo_tags:
                for tag in img.repo_tags:
                    parts = tag.rsplit(":", 1)
                    repo = parts[0] if len(parts) == 2 else tag
                    tag_name = parts[1] if len(parts) == 2 else "<none>"
                    click.echo(
                        f"{repo:<30} {tag_name:<15} {short_id:<14} {size_str:<10} {img.created}"
                    )
            else:
                click.echo(
                    f"{'<none>':<30} {'<none>':<15} {short_id:<14} {size_str:<10} {img.created}"
                )


# ===========================================================================
# docker export
# ===========================================================================


@docker.command("export")
@click.option(
    "--image", "-i",
    required=True,
    metavar="IMAGE",
    help="Docker image reference (name:tag or ID).",
)
@click.option(
    "--output", "-o",
    required=True,
    type=click.Path(),
    metavar="DIR",
    help="Output directory for the OCI Image Layout.",
)
@click.option(
    "--platform",
    default=None,
    metavar="OS/ARCH",
    help="Platform filter (e.g. linux/amd64). Omit to export all platforms.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def export_cmd(ctx, image, output, platform, as_json):
    """Export a Docker image as an OCI Image Layout on disk."""
    try:
        export_image(image, output, platform=platform)
    except (DockerError, LayoutError) as exc:
        emit_error("docker export", str(exc))

    if as_json:
        result = {
            "image": image,
            "output": str(output),
        }
        if platform:
            result["platform"] = platform
        emit_json(result)
    else:
        msg = f"Exported {image} to OCI layout at {output}"
        if platform:
            msg += f" (platform: {platform})"
        click.echo(msg)


# ===========================================================================
# docker push
# ===========================================================================


@docker.command("push")
@click.option("--no-progress", is_flag=True, default=False, help="Disable interactive transfer progress.")
@click.option(
    "--image", "-i",
    required=True,
    metavar="IMAGE",
    help="Docker image reference (name:tag or ID).",
)
@click.option(
    "--dest", "-d",
    required=True,
    metavar="REFERENCE",
    help="Destination artifact reference in the registry (registry/repo[:tag]).",
)
@click.option(
    "--platform",
    default=None,
    metavar="OS/ARCH",
    help="Platform filter (e.g. linux/amd64). Omit to push all platforms.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip blob existence checks; upload all blobs unconditionally.",
)
@click.option(
    "--chunked",
    is_flag=True,
    default=False,
    help="Use chunked (streaming) blob upload protocol.",
)
@click.option(
    "--chunk-size",
    type=click.IntRange(min=1),
    default=65536,
    metavar="BYTES",
    help="Chunk size in bytes for chunked uploads.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def push_cmd(ctx, image, dest, platform, force, chunked, chunk_size, as_json, no_progress):
    """Push a Docker image, showing indeterminate preparation then per-blob progress.

    JSON, noninteractive stderr and --no-progress suppress animation without
    changing export, upload protocols or the result schema. Invalid destinations
    and strict local snapshot/native-platform failures produce an error, not success.
    Summary counts come directly from the same PushResult fields as layout push.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        with ProgressDisplay(
            disabled=no_progress, structured=as_json, total_blobs=None, show_counts=True,
        ) as progress:
            progress.prepare("Preparing Docker image")
            result = push_image(
                image,
                dest,
                platform=platform,
                insecure=insecure,
                force=force,
                chunked=chunked,
                chunk_size=chunk_size,
                transfer_callback=progress.observe,
            )
    except (ArtifactError, DockerError, LayoutError, AuthError, BlobError,
            ManifestError, OSError, ValueError) as exc:
        emit_error("docker push", str(exc))

    if as_json:
        output = {
            "image": image,
            "destination": dest,
            "manifests_pushed": result.manifests_pushed,
            "blobs_uploaded": result.blobs_uploaded,
            "blobs_skipped": result.blobs_skipped,
        }
        if platform:
            output["platform"] = platform
        emit_json(output)
    else:
        click.echo(
            f"Pushed {image} to {dest}: "
            f"{result.manifests_pushed} manifest(s), "
            f"{result.blobs_uploaded} blob(s) uploaded, "
            f"{result.blobs_skipped} blob(s) skipped"
        )

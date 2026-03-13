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

import json
import sys

import click

from regshape.libs.docker import export_image, list_images, push_image
from regshape.libs.errors import AuthError, BlobError, DockerError, LayoutError, ManifestError


# ===========================================================================
# Helpers
# ===========================================================================


def _format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable size string."""
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.2f}GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.0f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


def _error(context: str, reason: str) -> None:
    """Print an error message to stderr."""
    click.echo(f"Error [{context}]: {reason}", err=True)


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
    """List images available in the local Docker daemon."""
    try:
        images = list_images(name_filter=name_filter)
    except DockerError as exc:
        _error("docker list", str(exc))
        sys.exit(1)

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
        click.echo(json.dumps(output, indent=2))
    else:
        if not images:
            click.echo("No images found.")
            return

        # Table header
        header = f"{'REPOSITORY':<30} {'TAG':<15} {'IMAGE ID':<14} {'SIZE':<10} {'CREATED'}"
        click.echo(header)

        for img in images:
            if img.repo_tags:
                for tag in img.repo_tags:
                    parts = tag.rsplit(":", 1)
                    repo = parts[0] if len(parts) == 2 else tag
                    tag_name = parts[1] if len(parts) == 2 else "<none>"
                    short_id = img.id[:19] if len(img.id) > 19 else img.id
                    # Strip sha256: prefix for display
                    if short_id.startswith("sha256:"):
                        short_id = short_id[7:19]
                    size_str = _format_size(img.size)
                    click.echo(
                        f"{repo:<30} {tag_name:<15} {short_id:<14} {size_str:<10} {img.created}"
                    )
            else:
                short_id = img.id[:19] if len(img.id) > 19 else img.id
                if short_id.startswith("sha256:"):
                    short_id = short_id[7:19]
                size_str = _format_size(img.size)
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
        _error("docker export", str(exc))
        sys.exit(1)

    if as_json:
        result = {
            "image": image,
            "output": str(output),
        }
        if platform:
            result["platform"] = platform
        click.echo(json.dumps(result, indent=2))
    else:
        msg = f"Exported {image} to OCI layout at {output}"
        if platform:
            msg += f" (platform: {platform})"
        click.echo(msg)


# ===========================================================================
# docker push
# ===========================================================================


@docker.command("push")
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
    help="Destination registry reference (registry/repo[:tag]).",
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
    type=int,
    default=65536,
    metavar="BYTES",
    help="Chunk size in bytes for chunked uploads.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def push_cmd(ctx, image, dest, platform, force, chunked, chunk_size, as_json):
    """Push a Docker image to a remote OCI registry."""
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        result = push_image(
            image,
            dest,
            platform=platform,
            insecure=insecure,
            force=force,
            chunked=chunked,
            chunk_size=chunk_size,
        )
    except (DockerError, LayoutError, AuthError, BlobError, ManifestError) as exc:
        _error("docker push", str(exc))
        sys.exit(1)

    if as_json:
        output = {
            "image": image,
            "destination": dest,
            "manifests_pushed": len(result.manifests),
            "blobs_uploaded": sum(
                1 for m in result.manifests for b in m.blobs if b.uploaded
            ),
            "blobs_skipped": sum(
                1 for m in result.manifests for b in m.blobs if not b.uploaded
            ),
        }
        if platform:
            output["platform"] = platform
        click.echo(json.dumps(output, indent=2))
    else:
        total_manifests = len(result.manifests)
        total_uploaded = sum(
            1 for m in result.manifests for b in m.blobs if b.uploaded
        )
        total_skipped = sum(
            1 for m in result.manifests for b in m.blobs if not b.uploaded
        )
        click.echo(
            f"Pushed {image} to {dest}: "
            f"{total_manifests} manifest(s), "
            f"{total_uploaded} blob(s) uploaded, "
            f"{total_skipped} blob(s) skipped"
        )

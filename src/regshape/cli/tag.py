#!/usr/bin/env python3

"""
:mod:`regshape.cli.tag` - CLI commands for OCI tag operations
=============================================================

.. module:: regshape.cli.tag
   :platform: Unix, Windows
   :synopsis: Click command group providing ``list`` and ``delete``
              subcommands for OCI image tag operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys
from typing import Optional

import click
import requests

from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, TagError
from regshape.libs.refs import format_ref, parse_image_ref
from regshape.libs.tags import delete_tag, list_tags
from regshape.libs.transport import RegistryClient, TransportConfig


# ===========================================================================
# Public Click group
# ===========================================================================

@click.group()
def tag():
    """Manage OCI image tags (list, delete)."""
    pass


# ===========================================================================
# tag list
# ===========================================================================

@tag.command("list")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help=(
        "Repository reference — registry must be embedded "
        "(e.g. registry/repo or registry/repo:tag). "
        "Any tag or digest suffix is ignored for list operations."
    ),
)
@click.option(
    "--n",
    "page_size",
    type=int,
    default=None,
    metavar="COUNT",
    help="Maximum number of tags to return (pagination page size).",
)
@click.option(
    "--last",
    default=None,
    metavar="TAG",
    help=(
        "Return tags lexicographically after this value. "
        "Used together with --n to page through results."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output the full TagList as a JSON object instead of one tag per line.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Write output to this file instead of stdout.",
)
@click.pass_context
@track_scenario("tag list")
def list_tags(ctx, image_ref, page_size, last, as_json, output):
    """List tags for the repository identified by IMAGE_REF.

    IMAGE_REF must embed the registry (``registry/repo`` or
    ``registry/repo:tag``). Any tag or digest suffix is ignored — only the
    registry and repository namespace are used.  Credentials are resolved
    automatically from the credential store populated by ``auth login``.

    Use ``--n`` and ``--last`` to page through large tag lists.  Use
    ``--json`` to receive the full response object instead of one tag per
    line.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, _ = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        tag_list = list_tags(
            client=client,
            repo=repo,
            page_size=page_size,
            last=last,
        )
    except (AuthError, TagError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    if as_json:
        _write(output, json.dumps(tag_list.to_dict(), indent=2))
    else:
        _write(output, "\n".join(tag_list.tags))


# ===========================================================================
# tag delete
# ===========================================================================

@tag.command("delete")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help=(
        "Image reference with embedded registry and tag "
        "(e.g. registry/repo:tag). Digest references are rejected."
    ),
)
@click.pass_context
@track_scenario("tag delete")
def delete(ctx, image_ref):
    """Delete a tag from the repository identified by IMAGE_REF.

    IMAGE_REF must be a tag reference (``registry/repo:tag``). Digest
    references are rejected with exit code 2 — to delete a manifest by
    digest use ``manifest delete``.  Deleting a tag does **not** delete the
    underlying manifest, which remains addressable by digest.  Credentials
    are resolved automatically from the credential store populated by
    ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    if reference.startswith("sha256:") or reference.startswith("sha512:"):
        _error(
            image_ref,
            "tag delete requires a tag reference; "
            "use 'manifest delete' for digest references",
        )
        sys.exit(2)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        delete_tag(
            client=client,
            repo=repo,
            tag=reference,
        )
    except (AuthError, TagError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    click.echo(f"Deleted tag: {format_ref(registry, repo, reference)}")


# ===========================================================================
# Internal helpers — output and error
# ===========================================================================

def _write(output_path: Optional[str], content: str) -> None:
    """Write *content* to a file or stdout.

    :param output_path: File path, or ``None`` to write to stdout.
    :param content: Text to write.
    """
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
    else:
        click.echo(content)


def _error(reference: str, reason: str) -> None:
    """Print an error message to stderr, prefixed with the reference."""
    click.echo(f"Error [{reference}]: {reason}", err=True)

#!/usr/bin/env python3

"""
:mod:`regshape.cli.referrer` - CLI commands for OCI referrer operations
========================================================================

.. module:: regshape.cli.referrer
   :platform: Unix, Windows
   :synopsis: Click command group providing the ``list`` subcommand for
              OCI referrer operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click
import requests

from regshape.cli.formatting import emit_error, emit_json, emit_table, emit_text
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, ReferrerError
from regshape.libs.refs import parse_image_ref
from regshape.libs.referrers import list_referrers, list_referrers_all
from regshape.libs.transport import RegistryClient, TransportConfig


# ===========================================================================
# Public Click group
# ===========================================================================

@click.group()
def referrer():
    """Discover OCI referrers (SBOMs, signatures, attestations)."""
    pass


# ===========================================================================
# referrer list
# ===========================================================================

@referrer.command("list")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help=(
        "Image reference with embedded registry and digest "
        "(e.g. registry/repo@sha256:abc...). "
        "Tag-only references are rejected — a digest is required."
    ),
)
@click.option(
    "--artifact-type",
    "-t",
    default=None,
    metavar="TYPE",
    help=(
        "Filter referrers to this artifact type "
        "(e.g. application/vnd.example.sbom.v1)."
    ),
)
@click.option(
    "--all",
    "fetch_all",
    is_flag=True,
    default=False,
    help="Follow pagination and return all referrers (default: single page only).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output the full ReferrerList as a JSON object instead of one referrer per line.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Write output to this file instead of stdout.",
)
@click.pass_context
@track_scenario("referrer list")
def referrer_list(ctx, image_ref, artifact_type, fetch_all, as_json, output):
    """List referrers for the manifest identified by digest in IMAGE_REF.

    IMAGE_REF must embed the registry and use a digest reference
    (``registry/repo@sha256:...``). Tag-only references are rejected —
    use ``manifest get`` to resolve a tag to a digest first.

    Use ``--artifact-type`` to filter by artifact type.  Use ``--all``
    to follow pagination and retrieve every referrer.  Use ``--json``
    to receive the full Image Index object instead of one referrer per line.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        emit_error(image_ref, str(exc))

    # The referrers API requires a digest reference.
    if not reference.startswith("sha256:") and not reference.startswith("sha512:"):
        emit_error(
            image_ref,
            "referrer list requires a digest reference "
            "(registry/repo@sha256:...); "
            "use 'manifest get' to resolve a tag to a digest",
            exit_code=2,
        )

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        if fetch_all:
            result = list_referrers_all(
                client=client,
                repo=repo,
                digest=reference,
                artifact_type=artifact_type,
            )
        else:
            result = list_referrers(
                client=client,
                repo=repo,
                digest=reference,
                artifact_type=artifact_type,
            )
    except (AuthError, ReferrerError, requests.exceptions.RequestException) as exc:
        emit_error(image_ref, str(exc))

    if as_json:
        emit_json(result.to_dict(), output)
    else:
        if not result.manifests:
            return
        rows = [
            [d.digest, d.artifact_type or "", str(d.size)]
            for d in result.manifests
        ]
        if output:
            # For file output, use space-separated columns
            lines = [f"{r[0]} {r[1]} {r[2]}" for r in rows]
            emit_text("\n".join(lines), output)
        else:
            emit_table(rows, headers=["DIGEST", "ARTIFACT TYPE", "SIZE"])


# ===========================================================================
# Internal helpers — output and error
# ===========================================================================



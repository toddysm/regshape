#!/usr/bin/env python3

"""
:mod:`regshape.cli.referrer` - CLI commands for OCI referrer operations
========================================================================

.. module:: regshape.cli.referrer
   :platform: Unix, Windows
   :synopsis: Click command group providing the ``list`` subcommand for
              OCI referrer operations with automatic tag-to-digest resolution.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click
import requests

from regshape.cli.formatting import emit_error, emit_json, emit_table, emit_text
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, ManifestError, ReferrerError
from regshape.libs.manifests import head_manifest
from regshape.libs.models.mediatype import ALL_MANIFEST_MEDIA_TYPES
from regshape.libs.referrers import list_referrers, list_referrers_all
from regshape.libs.refs import parse_image_ref
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
        "Image reference with embedded registry and tag or digest "
        "(e.g. registry/repo:tag or registry/repo@sha256:abc...). "
        "Tags are resolved to a digest automatically."
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
def referrer_list(
    ctx: click.Context,
    image_ref: str,
    artifact_type: str | None,
    fetch_all: bool,
    as_json: bool,
    output: str | None,
) -> None:
    """List referrers for the manifest identified by IMAGE_REF.

    IMAGE_REF must embed the registry and may use a tag or digest reference.
    Tags are resolved with a manifest HEAD request before querying referrers;
    digest references are used directly without a manifest lookup. References
    without a tag or digest resolve the ``latest`` tag. Resolution failures,
    including a missing digest response header, exit with code 1.

    Use ``--artifact-type`` to filter by artifact type.  Use ``--all``
    to follow pagination and retrieve every referrer.  Use ``--json``
    to receive the full Image Index object instead of one referrer per line.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        emit_error(image_ref, str(exc))

    is_digest = "@" in image_ref
    if is_digest and not reference.startswith(("sha256:", "sha512:")):
        emit_error(
            image_ref,
            "digest references must use sha256 or sha512",
            exit_code=2,
        )

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        if not is_digest:
            digest, _, _ = head_manifest(
                client=client,
                repo=repo,
                reference=reference,
                accept=",".join(sorted(ALL_MANIFEST_MEDIA_TYPES)),
            )
            if not digest.strip():
                raise ManifestError(
                    f"Cannot resolve tag {reference!r}",
                    "HEAD response is missing Docker-Content-Digest",
                )
            reference = digest

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
    except (AuthError, ManifestError, ReferrerError, requests.exceptions.RequestException) as exc:
        emit_error(image_ref, str(exc))

    if as_json:
        emit_json(result.to_dict(), output)
    else:
        if not result.manifests:
            if output:
                # Ensure the output file is created even when there are no referrers
                emit_text("", output)
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


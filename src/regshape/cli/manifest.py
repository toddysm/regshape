#!/usr/bin/env python3

"""
:mod:`regshape.cli.manifest` - CLI commands for OCI manifest operations
========================================================================

.. module:: regshape.cli.manifest
   :platform: Unix, Windows
   :synopsis: Click command group providing ``get``, ``info``, ``descriptor``,
              ``put``, and ``delete`` subcommands for OCI image manifest
              operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys
from typing import Optional

import click
import requests

from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.manifests import delete_manifest, get_manifest, head_manifest, push_manifest
from regshape.libs.models.manifest import ImageIndex, ImageManifest, parse_manifest
from regshape.libs.models.mediatype import ALL_MANIFEST_MEDIA_TYPES, OCI_IMAGE_MANIFEST
from regshape.libs.refs import format_ref, parse_image_ref
from regshape.libs.transport import RegistryClient, TransportConfig

# ---------------------------------------------------------------------------
# Default Accept header value when the caller does not override it.
# Registries return the most specific type they support for the reference.
# ---------------------------------------------------------------------------
_DEFAULT_ACCEPT = ",".join(sorted(ALL_MANIFEST_MEDIA_TYPES))

# ---------------------------------------------------------------------------
# Part names accepted by ``manifest get --part``
# ---------------------------------------------------------------------------
_PARTS = ("config", "layers", "subject", "annotations")


# ===========================================================================
# Public Click group
# ===========================================================================

@click.group()
def manifest():
    """Manage OCI image manifests (get, info, descriptor, put, delete)."""
    pass


# ===========================================================================
# manifest get
# ===========================================================================

@manifest.command("get")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help="Image reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
)
@click.option(
    "--accept",
    default=None,
    metavar="MEDIA_TYPE",
    help=(
        "Set a specific Accept header instead of the default multi-type value. "
        "Useful for forcing a particular manifest format or for break-mode testing."
    ),
)
@click.option(
    "--part",
    type=click.Choice(_PARTS),
    default=None,
    help=(
        "Extract and print a single field from the parsed manifest model: "
        "config, layers, subject, or annotations."
    ),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Write output to this file instead of stdout.",
)
@click.option(
    "--raw",
    is_flag=True,
    default=False,
    help=(
        "Skip model parsing and print the raw response body as-is. "
        "Cannot be combined with --part."
    ),
)
@click.pass_context
@track_scenario("manifest get")
def get(ctx, image_ref, accept, part, output, raw):
    """Fetch the manifest for IMAGE_REF.

    IMAGE_REF must embed the registry (``registry/repo:tag`` or
    ``registry/repo@sha256:...``).  Credentials are resolved
    automatically from the credential store populated by ``auth login``.

    Use ``--part`` to extract a single field from the parsed manifest
    model instead of printing the full JSON.  Use ``--raw`` to bypass
    model parsing entirely and print the wire bytes from the registry.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    if raw and part:
        raise click.UsageError("--raw and --part are mutually exclusive")

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        body, _, _ = get_manifest(
            client=client,
            repo=repo,
            reference=reference,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    if raw:
        _write(output, body)
        return

    # Parse and optionally extract a specific field
    try:
        parsed = parse_manifest(body)
    except ManifestError as exc:
        _error(image_ref, f"Failed to parse manifest: {exc}")
        sys.exit(1)

    if part:
        exit_code, result = _extract_part(parsed, part)
        if exit_code != 0:
            _error(image_ref, result)
            sys.exit(exit_code)
        _write(output, result)
        return

    # Full manifest output
    manifest_dict = json.loads(parsed.to_json())
    _write(output, json.dumps(manifest_dict, indent=2))


# ===========================================================================
# manifest info
# ===========================================================================

@manifest.command("info")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help="Image reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
)
@click.option(
    "--accept",
    default=None,
    metavar="MEDIA_TYPE",
    help="Set a specific Accept header instead of the default multi-type value.",
)
@click.pass_context
@track_scenario("manifest info")
def info(ctx, image_ref, accept):
    """Print metadata for IMAGE_REF without downloading the manifest body.

    Issues a HEAD request and returns the digest, media type, and size
    from response headers.  Credentials are resolved automatically from
    the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        digest, media_type, size = head_manifest(
            client=client,
            repo=repo,
            reference=reference,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    click.echo(f"Digest:       {digest}")
    click.echo(f"Media Type:   {media_type}")
    click.echo(f"Size:         {size}")


# ===========================================================================
# manifest descriptor
# ===========================================================================

@manifest.command("descriptor")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help="Image reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
)
@click.option(
    "--accept",
    default=None,
    metavar="MEDIA_TYPE",
    help="Set a specific Accept header instead of the default multi-type value.",
)
@click.pass_context
@track_scenario("manifest descriptor")
def descriptor(ctx, image_ref, accept):
    """Return the OCI Descriptor for IMAGE_REF as JSON.

    Issues a HEAD request and returns a JSON object with the
    ``mediaType``, ``digest``, and ``size`` fields, matching the OCI
    Descriptor wire format.  The output can be used directly as a
    Descriptor object in another manifest.  Credentials are resolved
    automatically from the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        digest, media_type, size = head_manifest(
            client=client,
            repo=repo,
            reference=reference,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    click.echo(json.dumps({
        "mediaType": media_type,
        "digest": digest,
        "size": size,
    }, indent=2))


# ===========================================================================
# manifest put
# ===========================================================================

@manifest.command("put")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help="Image reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
)
@click.option(
    "--file",
    "-f",
    "manifest_file",
    type=click.Path(exists=True),
    default=None,
    help="Read manifest JSON from this file.",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help="Read manifest JSON from stdin.",
)
@click.option(
    "--content-type",
    default=None,
    metavar="MEDIA_TYPE",
    help=(
        "Override the Content-Type header. "
        "By default it is inferred from the mediaType field in the JSON body. "
        "Useful for break-mode testing."
    ),
)
@click.pass_context
@track_scenario("manifest put")
def put(ctx, image_ref, manifest_file, from_stdin, content_type):
    """Push a manifest to the registry as IMAGE_REF.

    Provide the manifest JSON via ``--file`` or ``--stdin`` (exactly one
    is required).  Credentials are resolved automatically from the
    credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    if manifest_file and from_stdin:
        raise click.UsageError("--file and --stdin are mutually exclusive")
    if not manifest_file and not from_stdin:
        raise click.UsageError("One of --file or --stdin is required")

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    # Read manifest body
    if manifest_file:
        with open(manifest_file, "rb") as fh:
            body = fh.read()
    else:
        body = sys.stdin.buffer.read()

    # Infer Content-Type from the JSON body unless overridden
    if content_type is None:
        try:
            obj = json.loads(body)
            content_type = obj.get("mediaType", OCI_IMAGE_MANIFEST)
        except (json.JSONDecodeError, AttributeError):
            content_type = OCI_IMAGE_MANIFEST

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        digest = push_manifest(
            client=client,
            repo=repo,
            reference=reference,
            body=body,
            content_type=content_type,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    click.echo(f"Pushed: {digest}")


# ===========================================================================
# manifest delete
# ===========================================================================

@manifest.command("delete")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help="Image reference — must be a digest reference with registry embedded (registry/repo@sha256:...).",
)
@click.pass_context
@track_scenario("manifest delete")
def delete(ctx, image_ref):
    """Delete the manifest identified by IMAGE_REF.

    IMAGE_REF must be a digest reference (``registry/repo@sha256:...``).
    The OCI Distribution Spec requires deletion by digest; tag references
    are rejected with exit code 2.  Credentials are resolved automatically
    from the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    if not reference.startswith("sha256:") and not reference.startswith("sha512:"):
        _error(
            image_ref,
            "manifest delete requires a digest reference (@sha256:...); "
            "tag references are not supported by the OCI spec for delete operations",
        )
        sys.exit(2)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        delete_manifest(
            client=client,
            repo=repo,
            digest=reference,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    click.echo(f"Deleted: {reference}")


# ===========================================================================
# Internal helpers — model extraction, output, error
# ===========================================================================

def _extract_part(
    parsed: ImageManifest | ImageIndex,
    part: str,
) -> tuple[int, str]:
    """Extract a named field from a parsed manifest or index.

    :returns: ``(exit_code, json_string_or_error_message)``
    """
    if part == "config":
        if isinstance(parsed, ImageIndex):
            return 2, "manifest is an Image Index; it has no config field"
        return 0, json.dumps(parsed.config.to_dict(), indent=2)

    if part == "layers":
        if isinstance(parsed, ImageIndex):
            return 2, "manifest is an Image Index; it has no layers field"
        return 0, json.dumps([l.to_dict() for l in parsed.layers], indent=2)

    if part == "subject":
        if parsed.subject is None:
            return 2, "manifest has no subject field"
        return 0, json.dumps(parsed.subject.to_dict(), indent=2)

    if part == "annotations":
        if parsed.annotations is None:
            return 2, "manifest has no annotations field"
        return 0, json.dumps(parsed.annotations, indent=2)

    # Should not reach here because Click validates the choice
    return 1, f"unknown part: {part!r}"


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
    """Print an error message to stderr, prefixed with the image reference."""
    click.echo(f"Error [{reference}]: {reason}", err=True)

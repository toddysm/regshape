#!/usr/bin/env python3

"""
:mod:`regshape.cli.manifest` - CLI commands for OCI manifest operations
========================================================================

.. module:: regshape.cli.manifest
   :platform: Unix, Windows
   :synopsis: Click command group providing ``get``, ``info``, ``descriptor``,
              ``put``, and ``delete`` subcommands for OCI artifact manifest
              operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys
from collections.abc import Callable
from functools import partial

import click
import requests

from regshape.cli.formatting import emit_error, emit_json, emit_text, format_key_value
from regshape.cli.options import artifact_option
from regshape.cli.progress import ProgressDisplay
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.output import flush_telemetry
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.manifests import (
    delete_manifest,
    get_manifest,
    head_manifest,
    push_manifest,
)
from regshape.libs.models.manifest import ImageIndex, ImageManifest, parse_manifest
from regshape.libs.models.mediatype import ALL_MANIFEST_MEDIA_TYPES, OCI_IMAGE_MANIFEST
from regshape.libs.refs import parse_artifact_ref
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


class _ManifestCommandFailure(Exception):
    """Carry an expected command diagnostic through display cleanup before exiting."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        """Retain the existing message/status without prematurely raising SystemExit."""
        super().__init__(message)
        self.exit_code = exit_code


# ===========================================================================
# Public Click group
# ===========================================================================

@click.group()
def manifest():
    """Manage OCI manifests (get, info, descriptor, put, delete)."""
    pass


# ===========================================================================
# manifest get
# ===========================================================================

@manifest.command("get")
@telemetry_options
@click.option("--no-progress", is_flag=True, default=False, help="Disable interactive transfer progress.")
@artifact_option(
    help_text="Artifact reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
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
        "Skip model parsing and print decoded response text. "
        "Cannot be combined with --part."
    ),
)
@click.pass_context
def get(
    ctx: click.Context, image_ref: str, accept: str | None,
    part: str | None, output: str | None, raw: bool, no_progress: bool = False,
) -> None:
    """Fetch the manifest for ARTIFACT.

    ARTIFACT must embed the registry (``registry/repo:tag`` or
    ``registry/repo@sha256:...``).  Credentials are resolved
    automatically from the credential store populated by ``auth login``.

    Use ``--part`` to extract a single field from the parsed manifest
    model instead of printing the full JSON.  Use ``--raw`` to bypass
    model parsing entirely and print decoded response text with the existing
    newline behavior. Progress is stderr-only and finishes after required output
    and scenario diagnostics succeed.
    """
    try:
        with ProgressDisplay(disabled=no_progress) as progress:
            _get_with_progress(ctx, image_ref, accept, part, output, raw, progress)
            flush_telemetry()
            progress.finish_manifest()
    except _ManifestCommandFailure as exc:
        emit_error(image_ref, str(exc), exc.exit_code)


@track_scenario("manifest get")
def _get_with_progress(
    ctx: click.Context, image_ref: str, accept: str | None,
    part: str | None, output: str | None, raw: bool, progress: ProgressDisplay,
) -> None:
    """Fetch, optionally parse/extract, and write inside the display's diagnostic lifetime.

    Expected failures retain their existing exit code/message for emission after
    cleanup. Unexpected output/transport-callback exceptions propagate unchanged.
    The outer command finalizes only after this scenario wrapper also returns.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    if raw and part:
        raise click.UsageError("--raw and --part are mutually exclusive")

    try:
        registry, repo, reference = parse_artifact_ref(image_ref)
    except ValueError as exc:
        raise _ManifestCommandFailure(str(exc)) from exc

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        body, _, _ = get_manifest(
            client=client,
            repo=repo,
            reference=reference,
            accept=accept or _DEFAULT_ACCEPT,
            progress_callback=progress.observe,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        raise _ManifestCommandFailure(str(exc)) from exc

    emit: Callable[[], None]
    if raw:
        emit = partial(emit_text, body, output)
    else:
        progress.processing_manifest()
        try:
            parsed = parse_manifest(body)
        except ManifestError as exc:
            raise _ManifestCommandFailure(f"Failed to parse manifest: {exc}") from exc
        if part:
            exit_code, result = _extract_part(parsed, part)
            if exit_code != 0:
                raise _ManifestCommandFailure(result, exit_code)
            emit = partial(emit_text, result, output)
        else:
            data = json.loads(parsed.to_json())
            emit = partial(emit_json, data, output)

    progress.writing_manifest()
    if output:
        emit()
    else:
        with progress.manifest_output():
            emit()


# ===========================================================================
# manifest info
# ===========================================================================

@manifest.command("info")
@telemetry_options
@artifact_option(
    help_text="Artifact reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
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
    """Print metadata for ARTIFACT without downloading the manifest body.

    Issues a HEAD request and returns the digest, media type, and size
    from response headers.  Credentials are resolved automatically from
    the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_artifact_ref(image_ref)
    except ValueError as exc:
        emit_error(image_ref, str(exc))

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        digest, media_type, size = head_manifest(
            client=client,
            repo=repo,
            reference=reference,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        emit_error(image_ref, str(exc))

    click.echo(format_key_value([
        ("Digest", digest),
        ("Media Type", media_type),
        ("Size", str(size)),
    ]))


# ===========================================================================
# manifest descriptor
# ===========================================================================

@manifest.command("descriptor")
@telemetry_options
@artifact_option(
    help_text="Artifact reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
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
    """Return the OCI Descriptor for ARTIFACT as JSON.

    Issues a HEAD request and returns a JSON object with the
    ``mediaType``, ``digest``, and ``size`` fields, matching the OCI
    Descriptor wire format.  The output can be used directly as a
    Descriptor object in another manifest.  Credentials are resolved
    automatically from the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_artifact_ref(image_ref)
    except ValueError as exc:
        emit_error(image_ref, str(exc))

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        digest, media_type, size = head_manifest(
            client=client,
            repo=repo,
            reference=reference,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        emit_error(image_ref, str(exc))

    emit_json({
        "mediaType": media_type,
        "digest": digest,
        "size": size,
    })


# ===========================================================================
# manifest put
# ===========================================================================

@manifest.command("put")
@telemetry_options
@artifact_option(
    help_text="Artifact reference — registry must be embedded (registry/repo:tag or registry/repo@sha256:...).",
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
    """Push a manifest to the registry as ARTIFACT.

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
        registry, repo, reference = parse_artifact_ref(image_ref)
    except ValueError as exc:
        emit_error(image_ref, str(exc))

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
        emit_error(image_ref, str(exc))

    click.echo(f"Pushed: {digest}")


# ===========================================================================
# manifest delete
# ===========================================================================

@manifest.command("delete")
@telemetry_options
@artifact_option(
    help_text="Artifact reference — must be a digest reference with registry embedded (registry/repo@sha256:...).",
)
@click.pass_context
@track_scenario("manifest delete")
def delete(ctx, image_ref):
    """Delete the manifest identified by ARTIFACT.

    ARTIFACT must be a digest reference (``registry/repo@sha256:...``).
    The OCI Distribution Spec requires deletion by digest; tag references
    are rejected with exit code 2.  Credentials are resolved automatically
    from the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = parse_artifact_ref(image_ref)
    except ValueError as exc:
        emit_error(image_ref, str(exc))

    if not reference.startswith("sha256:") and not reference.startswith("sha512:"):
        emit_error(
            image_ref,
            "manifest delete requires a digest reference (@sha256:...); "
            "tag references are not supported by the OCI spec for delete operations",
            exit_code=2,
        )

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        delete_manifest(
            client=client,
            repo=repo,
            digest=reference,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        emit_error(image_ref, str(exc))

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

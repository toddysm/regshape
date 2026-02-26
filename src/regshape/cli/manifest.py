#!/usr/bin/env python3

"""
:mod:`manifest` - CLI commands for OCI manifest operations
==========================================================

    module:: manifest
    :platform: Unix, Windows
    :synopsis: Click command group providing ``get``, ``head``, ``put``, and
               ``delete`` subcommands for OCI image manifest operations.
    moduleauthor:: ToddySM <toddysm@gmail.com>

.. note::
    HTTP requests are currently issued directly via ``requests`` + the
    existing ``libs/auth/`` helpers.  These will be replaced by
    ``RegistryClient`` calls once ``libs/transport/`` is implemented.
"""

import json
import sys
from typing import Optional

import click
import requests

from regshape.libs.auth import registryauth
from regshape.libs.auth.credentials import resolve_credentials
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.call_details import http_request
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.models.manifest import ImageIndex, ImageManifest, parse_manifest
from regshape.libs.models.mediatype import ALL_MANIFEST_MEDIA_TYPES

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
    """Manage OCI image manifests (get, head, put, delete)."""
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
    help="Image reference (repo:tag, registry/repo:tag, or repo@sha256:...).",
)
@click.option(
    "--registry",
    "-r",
    default=None,
    help="Registry hostname (e.g., acr.io). Required if not embedded in IMAGE_REF.",
)
@click.option(
    "--username",
    "-u",
    default=None,
    help="Username for authentication.",
)
@click.option(
    "--password",
    "-p",
    default=None,
    help="Password for authentication.",
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
def get(ctx, registry, username, password, image_ref, accept, part, output, raw):
    """Fetch the manifest for IMAGE_REF.

    IMAGE_REF may be a tag reference (``repo:tag`` or
    ``registry/repo:tag``) or a digest reference
    (``repo@sha256:...``).  When the registry is not embedded in
    IMAGE_REF the ``--registry`` option is used.

    Use ``--part`` to extract a single field from the parsed manifest
    model instead of printing the full JSON.  Use ``--raw`` to bypass
    model parsing entirely and print the wire bytes from the registry.
    """
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    if raw and part:
        raise click.UsageError("--raw and --part are mutually exclusive")

    try:
        registry, repo, reference = _parse_image_ref(image_ref, registry)
    except ValueError as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    try:
        body, content_type, digest = _fetch_manifest(
            registry=registry,
            repo=repo,
            reference=reference,
            insecure=insecure,
            username=username,
            password=password,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    if raw:
        _write(output, body)
        return

    # Parse and optionally extract a specific field
    try:
        parsed = parse_manifest(body)
    except ManifestError as exc:
        _error(output_json, image_ref, f"Failed to parse manifest: {exc}")
        sys.exit(1)

    if part:
        exit_code, result = _extract_part(parsed, part)
        if exit_code != 0:
            _error(output_json, image_ref, result)
            sys.exit(exit_code)
        if output_json:
            envelope = {
                "reference": image_ref,
                "digest": digest,
                "media_type": content_type,
                "part_name": part,
                "part": json.loads(result),
            }
            _write(output, json.dumps(envelope, indent=2))
        else:
            _write(output, result)
        return

    # Full manifest output
    manifest_dict = json.loads(parsed.to_json())
    if output_json:
        envelope = {
            "reference": image_ref,
            "digest": digest,
            "media_type": content_type,
            "manifest": manifest_dict,
        }
        _write(output, json.dumps(envelope, indent=2))
    else:
        _write(output, json.dumps(manifest_dict, indent=2))


# ===========================================================================
# manifest head
# ===========================================================================

@manifest.command("head")
@telemetry_options
@click.option(
    "--image-ref",
    "-i",
    required=True,
    metavar="IMAGE_REF",
    help="Image reference (repo:tag, registry/repo:tag, or repo@sha256:...).",
)
@click.option(
    "--registry",
    "-r",
    default=None,
    help="Registry hostname (e.g., acr.io). Required if not embedded in IMAGE_REF.",
)
@click.option(
    "--username",
    "-u",
    default=None,
    help="Username for authentication.",
)
@click.option(
    "--password",
    "-p",
    default=None,
    help="Password for authentication.",
)
@click.option(
    "--accept",
    default=None,
    metavar="MEDIA_TYPE",
    help="Set a specific Accept header instead of the default multi-type value.",
)
@click.pass_context
@track_scenario("manifest head")
def head(ctx, registry, username, password, image_ref, accept):
    """Check whether IMAGE_REF exists and print its metadata.

    Issues a HEAD request, which returns digest, media type, and size
    from response headers without downloading the manifest body.
    """
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = _parse_image_ref(image_ref, registry)
    except ValueError as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    try:
        digest, media_type, size = _head_manifest(
            registry=registry,
            repo=repo,
            reference=reference,
            insecure=insecure,
            username=username,
            password=password,
            accept=accept or _DEFAULT_ACCEPT,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    if output_json:
        click.echo(json.dumps({
            "reference": image_ref,
            "digest": digest,
            "media_type": media_type,
            "size": size,
        }, indent=2))
    else:
        click.echo(f"Digest:       {digest}")
        click.echo(f"Media Type:   {media_type}")
        click.echo(f"Size:         {size}")


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
    help="Image reference (repo:tag, registry/repo:tag, or repo@sha256:...).",
)
@click.option(
    "--registry",
    "-r",
    default=None,
    help="Registry hostname (e.g., acr.io). Required if not embedded in IMAGE_REF.",
)
@click.option(
    "--username",
    "-u",
    default=None,
    help="Username for authentication.",
)
@click.option(
    "--password",
    "-p",
    default=None,
    help="Password for authentication.",
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
def put(ctx, registry, username, password, image_ref, manifest_file, from_stdin, content_type):
    """Push a manifest to the registry as IMAGE_REF.

    Provide the manifest JSON via ``--file`` or ``--stdin`` (exactly one
    is required).
    """
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    if manifest_file and from_stdin:
        raise click.UsageError("--file and --stdin are mutually exclusive")
    if not manifest_file and not from_stdin:
        raise click.UsageError("One of --file or --stdin is required")

    try:
        registry, repo, reference = _parse_image_ref(image_ref, registry)
    except ValueError as exc:
        _error(output_json, image_ref, str(exc))
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
            content_type = obj.get("mediaType", "application/vnd.oci.image.manifest.v1+json")
        except (json.JSONDecodeError, AttributeError):
            content_type = "application/vnd.oci.image.manifest.v1+json"

    try:
        digest = _push_manifest(
            registry=registry,
            repo=repo,
            reference=reference,
            body=body,
            content_type=content_type,
            insecure=insecure,
            username=username,
            password=password,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    if output_json:
        click.echo(json.dumps({"reference": image_ref, "digest": digest}, indent=2))
    else:
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
    help="Image reference — must be a digest reference (repo@sha256:...).",
)
@click.option(
    "--registry",
    "-r",
    default=None,
    help="Registry hostname (e.g., acr.io). Required if not embedded in IMAGE_REF.",
)
@click.option(
    "--username",
    "-u",
    default=None,
    help="Username for authentication.",
)
@click.option(
    "--password",
    "-p",
    default=None,
    help="Password for authentication.",
)
@click.pass_context
@track_scenario("manifest delete")
def delete(ctx, registry, username, password, image_ref):
    """Delete the manifest identified by IMAGE_REF.

    IMAGE_REF must be a digest reference (``repo@sha256:...``).  The OCI
    Distribution Spec requires deletion by digest; tag references are
    rejected with exit code 2.
    """
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo, reference = _parse_image_ref(image_ref, registry)
    except ValueError as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    if not reference.startswith("sha256:") and not reference.startswith("sha512:"):
        _error(
            output_json,
            image_ref,
            "manifest delete requires a digest reference (@sha256:...); "
            "tag references are not supported by the OCI spec for delete operations",
        )
        sys.exit(2)

    try:
        _delete_manifest(
            registry=registry,
            repo=repo,
            digest=reference,
            insecure=insecure,
            username=username,
            password=password,
        )
    except (AuthError, ManifestError, requests.exceptions.RequestException) as exc:
        _error(output_json, image_ref, str(exc))
        sys.exit(1)

    if output_json:
        click.echo(json.dumps({"reference": image_ref, "digest": reference}, indent=2))
    else:
        click.echo(f"Deleted: {reference}")


# ===========================================================================
# Internal helpers — HTTP operations
# ===========================================================================

@track_time
def _fetch_manifest(
    registry: str,
    repo: str,
    reference: str,
    insecure: bool,
    username: Optional[str],
    password: Optional[str],
    accept: str,
) -> tuple[str, str, str]:
    """Issue an authenticated GET for the manifest.

    :returns: ``(body_str, content_type, digest)``
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/{repo}/manifests/{reference}"
    headers = {"Accept": accept}

    response = http_request(url, "GET", headers=headers, timeout=30)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {registry!r} returned 401 without WWW-Authenticate",
            )
        auth_value = registryauth.authenticate(www_auth, username, password)
        auth_scheme = www_auth.split(" ")[0]
        headers["Authorization"] = f"{auth_scheme} {auth_value}"
        response = http_request(url, "GET", headers=headers, timeout=30)

    _raise_for_manifest_error(response, registry, repo, reference)

    body = response.text
    content_type = response.headers.get("Content-Type", "")
    digest = response.headers.get("Docker-Content-Digest", "")
    return body, content_type, digest


@track_time
def _head_manifest(
    registry: str,
    repo: str,
    reference: str,
    insecure: bool,
    username: Optional[str],
    password: Optional[str],
    accept: str,
) -> tuple[str, str, int]:
    """Issue an authenticated HEAD for the manifest.

    :returns: ``(digest, content_type, size)``
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/{repo}/manifests/{reference}"
    headers = {"Accept": accept}

    response = http_request(url, "HEAD", headers=headers, timeout=30)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {registry!r} returned 401 without WWW-Authenticate",
            )
        auth_value = registryauth.authenticate(www_auth, username, password)
        auth_scheme = www_auth.split(" ")[0]
        headers["Authorization"] = f"{auth_scheme} {auth_value}"
        response = http_request(url, "HEAD", headers=headers, timeout=30)

    _raise_for_manifest_error(response, registry, repo, reference)

    digest = response.headers.get("Docker-Content-Digest", "")
    media_type = response.headers.get("Content-Type", "")
    try:
        size = int(response.headers.get("Content-Length", "0"))
    except ValueError:
        size = 0
    return digest, media_type, size


@track_time
def _push_manifest(
    registry: str,
    repo: str,
    reference: str,
    body: bytes,
    content_type: str,
    insecure: bool,
    username: Optional[str],
    password: Optional[str],
) -> str:
    """Issue an authenticated PUT for the manifest.

    :returns: The ``Docker-Content-Digest`` from the response.
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/{repo}/manifests/{reference}"
    headers = {"Content-Type": content_type}

    response = http_request(url, "PUT", headers=headers, data=body, timeout=30)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {registry!r} returned 401 without WWW-Authenticate",
            )
        auth_value = registryauth.authenticate(www_auth, username, password)
        auth_scheme = www_auth.split(" ")[0]
        headers["Authorization"] = f"{auth_scheme} {auth_value}"
        response = http_request(url, "PUT", headers=headers, data=body, timeout=30)

    _raise_for_manifest_error(response, registry, repo, reference)

    return response.headers.get("Docker-Content-Digest", "")


@track_time
def _delete_manifest(
    registry: str,
    repo: str,
    digest: str,
    insecure: bool,
    username: Optional[str],
    password: Optional[str],
) -> None:
    """Issue an authenticated DELETE for the manifest.

    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/{repo}/manifests/{digest}"

    response = http_request(url, "DELETE", timeout=30)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {registry!r} returned 401 without WWW-Authenticate",
            )
        auth_value = registryauth.authenticate(www_auth, username, password)
        auth_scheme = www_auth.split(" ")[0]
        auth_headers = {"Authorization": f"{auth_scheme} {auth_value}"}
        response = http_request(url, "DELETE", headers=auth_headers, timeout=30)

    _raise_for_manifest_error(response, registry, repo, digest)


# ===========================================================================
# Internal helpers — model extraction, reference parsing, error formatting
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


def _parse_image_ref(
    image_ref: str,
    context_registry: Optional[str],
) -> tuple[str, str, str]:
    """Parse an image reference into ``(registry, repository, reference)``.

    Supports the following formats::

        myimage:tag
        myrepo/myimage:tag
        registry.io/myimage:tag
        registry.io/myrepo/myimage:tag
        myimage@sha256:<hex>
        registry.io/myimage@sha256:<hex>

    :param image_ref: The image reference string from the CLI argument.
    :param context_registry: Registry from the ``--registry`` per-command option.
    :returns: A ``(registry, repo, reference)`` triple where ``reference``
        is the tag or digest string (without the ``@`` prefix).
    :raises ValueError: If neither the image ref nor context provides a
        registry.
    """
    # Separate digest (@) from tag (:) — digest takes priority
    if "@" in image_ref:
        path_part, digest_part = image_ref.rsplit("@", 1)
        ref = digest_part
        tag_sep = "@"
    elif ":" in image_ref:
        # Could be "registry:port/repo:tag" so split on the last ":"
        last_colon = image_ref.rfind(":")
        # Make sure the colon is not part of a port in the registry hostname
        # (simple check: if the part after the last colon contains a slash, it's
        # a host:port situation — split differently)
        before_colon = image_ref[:last_colon]
        after_colon = image_ref[last_colon + 1:]
        if "/" in after_colon:
            # "registry:5000/repo/image" with no tag — use "latest"
            path_part = image_ref
            ref = "latest"
        else:
            path_part = before_colon
            ref = after_colon
        tag_sep = ":"
    else:
        path_part = image_ref
        ref = "latest"
        tag_sep = None  # noqa: F841

    # Determine whether the first component of path_part is a registry hostname
    parts = path_part.split("/")
    first = parts[0]
    is_registry = (
        "." in first
        or ":" in first
        or first == "localhost"
    )

    if is_registry:
        registry = first
        repo = "/".join(parts[1:])
    else:
        if not context_registry:
            raise ValueError(
                f"Cannot determine registry from {image_ref!r}: "
                "embed the registry in the reference or use the --registry option"
            )
        registry = context_registry
        repo = path_part

    if not repo:
        raise ValueError(
            f"Cannot determine repository from {image_ref!r}"
        )

    return registry, repo, ref


def _raise_for_manifest_error(
    response: requests.Response,
    registry: str,
    repo: str,
    reference: str,
) -> None:
    """Raise a :class:`ManifestError` for non-2xx responses.

    Attempts to parse the OCI error JSON body for a descriptive message.

    :raises ManifestError: For all non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    # Try to parse OCI error body
    detail = ""
    try:
        err_body = response.json()
        errors = err_body.get("errors", [])
        if errors:
            first = errors[0]
            code = first.get("code", "")
            msg = first.get("message", "")
            detail = f"{code}: {msg}"
    except Exception:
        detail = response.text[:200]

    if response.status_code == 404:
        raise ManifestError(
            f"Manifest not found: {registry}/{repo}:{reference}",
            detail or f"HTTP 404",
        )
    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    raise ManifestError(
        f"Registry error for {registry}/{repo}:{reference}",
        detail or f"HTTP {response.status_code}",
    )


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


def _error(output_json: bool, reference: str, reason: str) -> None:
    """Print an error message in the appropriate format to stderr."""
    if output_json:
        click.echo(
            json.dumps({"status": "error", "reference": reference, "message": reason}),
            err=True,
        )
    else:
        click.echo(f"Error: {reason}", err=True)

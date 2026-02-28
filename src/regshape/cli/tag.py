#!/usr/bin/env python3

"""
:mod:`regshape.cli.tag` - CLI commands for OCI tag operations
=============================================================

.. module:: regshape.cli.tag
   :platform: Unix, Windows
   :synopsis: Click command group providing ``list`` and ``delete``
              subcommands for OCI image tag operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

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
from regshape.libs.errors import AuthError, TagError
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.models.tags import TagList


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
        registry, repo, _ = _parse_image_ref(image_ref)
    except ValueError as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    username, password = resolve_credentials(registry, None, None)

    try:
        tag_list = _fetch_tag_list(
            registry=registry,
            repo=repo,
            insecure=insecure,
            username=username,
            password=password,
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
        registry, repo, reference = _parse_image_ref(image_ref)
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

    username, password = resolve_credentials(registry, None, None)

    try:
        _delete_tag(
            registry=registry,
            repo=repo,
            tag=reference,
            insecure=insecure,
            username=username,
            password=password,
        )
    except (AuthError, TagError, requests.exceptions.RequestException) as exc:
        _error(image_ref, str(exc))
        sys.exit(1)

    click.echo(f"Deleted tag: {_format_ref(registry, repo, reference)}")


# ===========================================================================
# Internal helpers — HTTP operations
# ===========================================================================

@track_time
def _fetch_tag_list(
    registry: str,
    repo: str,
    insecure: bool,
    username: Optional[str],
    password: Optional[str],
    page_size: Optional[int],
    last: Optional[str],
) -> TagList:
    """Issue an authenticated GET for the tag list.

    :returns: A :class:`~regshape.libs.models.tags.TagList` instance.
    :raises AuthError: On authentication failure.
    :raises TagError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/{repo}/tags/list"

    params: dict = {}
    if page_size is not None:
        params["n"] = page_size
    if last is not None:
        params["last"] = last

    response = http_request(url, "GET", params=params if params else None, timeout=30)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {registry!r} returned 401 without WWW-Authenticate",
            )
        auth_scheme = www_auth.split(" ", 1)[0]
        if auth_scheme.lower() == "basic" and (username is None or password is None):
            raise AuthError(
                "Authentication failed",
                "Registry requested Basic authentication but no credentials are available",
            )
        normalized_www_auth, normalized_scheme = _normalize_www_authenticate(www_auth)
        auth_value = registryauth.authenticate(normalized_www_auth, username, password)
        headers = {"Authorization": f"{normalized_scheme} {auth_value}"}
        response = http_request(
            url, "GET", headers=headers, params=params if params else None, timeout=30
        )

    _raise_for_list_error(response, registry, repo)

    try:
        return TagList.from_json(response.text)
    except TagError:
        raise
    except Exception as exc:
        raise TagError(
            f"Failed to parse tag-list response from {registry}/{repo}",
            str(exc),
        ) from exc


@track_time
def _delete_tag(
    registry: str,
    repo: str,
    tag: str,
    insecure: bool,
    username: Optional[str],
    password: Optional[str],
) -> None:
    """Issue an authenticated DELETE for a tag.

    The OCI Distribution Spec routes tag deletion through the manifests
    endpoint: ``DELETE /v2/<name>/manifests/<tag>``.

    :raises AuthError: On authentication failure.
    :raises TagError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/{repo}/manifests/{tag}"

    response = http_request(url, "DELETE", timeout=30)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {registry!r} returned 401 without WWW-Authenticate",
            )
        auth_scheme = www_auth.split(" ", 1)[0]
        if auth_scheme.lower() == "basic" and (username is None or password is None):
            raise AuthError(
                "Authentication failed",
                "Registry requested Basic authentication but no credentials are available",
            )
        normalized_www_auth, normalized_scheme = _normalize_www_authenticate(www_auth)
        auth_value = registryauth.authenticate(normalized_www_auth, username, password)
        auth_headers = {"Authorization": f"{normalized_scheme} {auth_value}"}
        response = http_request(url, "DELETE", headers=auth_headers, timeout=30)

    _raise_for_delete_error(response, registry, repo, tag)


# ===========================================================================
# Internal helpers — utilities
# ===========================================================================

def _raise_for_list_error(
    response: requests.Response,
    registry: str,
    repo: str,
) -> None:
    """Raise a :class:`TagError` or :class:`AuthError` for non-2xx tag-list responses.

    Maps status codes to messages appropriate for a list operation:

    * 401 → :class:`AuthError`
    * 404 → "Repository not found"
    * other non-2xx → generic registry error

    :raises AuthError: On 401.
    :raises TagError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = OciErrorResponse.from_response(response).first_detail() or response.text[:200]

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise TagError(
            f"Repository not found: {registry}/{repo}",
            detail or "HTTP 404",
        )
    raise TagError(
        f"Registry error for {registry}/{repo}",
        detail or f"HTTP {response.status_code}",
    )


def _raise_for_delete_error(
    response: requests.Response,
    registry: str,
    repo: str,
    tag: str,
) -> None:
    """Raise a :class:`TagError` or :class:`AuthError` for non-2xx tag-delete responses.

    Maps status codes to messages appropriate for a delete operation:

    * 401 → :class:`AuthError`
    * 404 → "Tag not found"
    * 400 / 405 → "Tag deletion is not supported"
    * other non-2xx → generic registry error

    :raises AuthError: On 401.
    :raises TagError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = OciErrorResponse.from_response(response).first_detail() or response.text[:200]

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise TagError(
            f"Tag not found: {_format_ref(registry, repo, tag)}",
            detail or "HTTP 404",
        )
    if response.status_code in (400, 405):
        raise TagError(
            f"Tag deletion is not supported by this registry",
            detail or f"HTTP {response.status_code}",
        )
    raise TagError(
        f"Registry error for {_format_ref(registry, repo, tag)}",
        detail or f"HTTP {response.status_code}",
    )


def _normalize_www_authenticate(www_auth: str) -> tuple[str, str]:
    """Normalize a WWW-Authenticate header value.

    Returns a ``(normalized_www_auth, normalized_scheme)`` tuple.

    - Capitalizes ``basic``/``bearer`` scheme names.
    - Strips whitespace from each comma-separated parameter.

    :param www_auth: Raw ``WWW-Authenticate`` header value.
    :returns: ``(normalized_www_auth, normalized_scheme)``.
    """
    scheme, sep, params = www_auth.partition(" ")
    normalized_scheme = (
        scheme.capitalize() if scheme.lower() in ("basic", "bearer") else scheme
    )
    if sep and params:
        cleaned_params = ",".join(part.strip() for part in params.split(","))
        normalized_www_auth = f"{normalized_scheme} {cleaned_params}"
    else:
        normalized_www_auth = normalized_scheme
    return normalized_www_auth, normalized_scheme


def _parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Parse an image reference into ``(registry, repository, reference)``.

    Supported formats::

        registry.io/myimage:tag
        registry.io/myrepo/myimage:tag
        registry.io/myimage@sha256:<hex>
        registry.io/myrepo/myimage        (reference defaults to "latest")

    :param image_ref: The image reference string from the ``--image-ref`` flag.
    :returns: A ``(registry, repo, reference)`` triple.
    :raises ValueError: If the registry cannot be determined.
    """
    if "@" in image_ref:
        path_part, digest_part = image_ref.rsplit("@", 1)
        ref = digest_part
    elif ":" in image_ref:
        last_colon = image_ref.rfind(":")
        before_colon = image_ref[:last_colon]
        after_colon = image_ref[last_colon + 1:]
        if "/" in after_colon:
            path_part = image_ref
            ref = "latest"
        else:
            path_part = before_colon
            ref = after_colon
    else:
        path_part = image_ref
        ref = "latest"

    parts = path_part.split("/")
    first = parts[0]
    is_registry = "." in first or ":" in first or first == "localhost"

    if is_registry:
        registry = first
        repo = "/".join(parts[1:])
    else:
        raise ValueError(
            f"Cannot determine registry from {image_ref!r}: "
            "embed the registry in --image-ref (e.g. acr.io/repo:tag)"
        )

    if not repo:
        raise ValueError(
            f"Cannot determine repository from {image_ref!r}"
        )

    return registry, repo, ref


def _format_ref(registry: str, repo: str, reference: str) -> str:
    """Return a canonical OCI reference string.

    Uses ``@`` for digests, ``:`` for tags.

    :param registry: Registry hostname.
    :param repo: Repository name.
    :param reference: Tag or digest.
    :returns: Canonical reference string.
    """
    sep = "@" if (reference.startswith("sha256:") or reference.startswith("sha512:")) else ":"
    return f"{registry}/{repo}{sep}{reference}"


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

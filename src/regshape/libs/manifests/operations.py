#!/usr/bin/env python3

"""
:mod:`regshape.libs.manifests.operations` - OCI manifest operations
====================================================================

.. module:: regshape.libs.manifests.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for GET, HEAD, PUT, and DELETE
              manifest operations against OCI Distribution-compliant
              registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting (``sys.exit``, ``click.echo``) is the caller's
responsibility.
"""

import requests

from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.refs import format_ref
from regshape.libs.transport import RegistryClient


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def get_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
) -> tuple[str, str, str]:
    """Fetch the manifest for *repo*/*reference*.

    Issues a GET request to ``/v2/{repo}/manifests/{reference}``.  The
    401→auth→retry cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param reference: Tag or digest (e.g. ``latest`` or
                      ``sha256:abc...``).
    :param accept: Value for the ``Accept`` header.
    :returns: ``(body_str, content_type, digest)``
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/manifests/{reference}"
    response = client.get(path, headers={"Accept": accept})
    _raise_for_manifest_error(response, client.config.registry, repo, reference)
    body = response.text
    content_type = response.headers.get("Content-Type", "")
    digest = response.headers.get("Docker-Content-Digest", "")
    return body, content_type, digest


@track_time
def head_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
) -> tuple[str, str, int]:
    """Return metadata for *repo*/*reference* without downloading the body.

    Issues a HEAD request to ``/v2/{repo}/manifests/{reference}``.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param reference: Tag or digest.
    :param accept: Value for the ``Accept`` header.
    :returns: ``(digest, media_type, size)``
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/manifests/{reference}"
    response = client.head(path, headers={"Accept": accept})
    _raise_for_manifest_error(response, client.config.registry, repo, reference)
    digest = response.headers.get("Docker-Content-Digest", "")
    media_type = response.headers.get("Content-Type", "")
    try:
        size = int(response.headers.get("Content-Length", "0"))
    except ValueError:
        size = 0
    return digest, media_type, size


@track_time
def push_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    body: bytes,
    content_type: str,
) -> str:
    """Push a manifest to *repo* as *reference*.

    Issues a PUT request to ``/v2/{repo}/manifests/{reference}``.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param reference: Tag or digest to create/overwrite.
    :param body: Raw manifest JSON bytes.
    :param content_type: Value for the ``Content-Type`` header.
    :returns: The ``Docker-Content-Digest`` from the response headers.
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/manifests/{reference}"
    response = client.put(path, headers={"Content-Type": content_type}, data=body)
    _raise_for_manifest_error(response, client.config.registry, repo, reference)
    return response.headers.get("Docker-Content-Digest", "")


@track_time
def delete_manifest(
    client: RegistryClient,
    repo: str,
    digest: str,
) -> None:
    """Delete the manifest identified by *digest* from *repo*.

    Issues a DELETE request to ``/v2/{repo}/manifests/{digest}``.  The OCI
    Distribution Spec requires deletion by digest, not by tag.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param digest: Manifest digest (e.g. ``sha256:abc...``).
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/manifests/{digest}"
    response = client.delete(path)
    _raise_for_manifest_error(response, client.config.registry, repo, digest)


# ===========================================================================
# Private error helper
# ===========================================================================


def _raise_for_manifest_error(
    response: requests.Response,
    registry: str,
    repo: str,
    reference: str,
) -> None:
    """Raise on non-2xx responses.

    Maps HTTP status codes to :class:`~regshape.libs.errors.ManifestError`
    or :class:`~regshape.libs.errors.AuthError` with a descriptive message
    parsed from the OCI error JSON body.

    :raises AuthError: For 401.
    :raises ManifestError: For all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )
    ref_str = format_ref(registry, repo, reference)

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise ManifestError(
            f"Manifest not found: {ref_str}",
            detail or "HTTP 404",
        )
    raise ManifestError(
        f"Registry error for {ref_str}",
        detail or f"HTTP {response.status_code}",
    )

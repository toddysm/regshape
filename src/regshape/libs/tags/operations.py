#!/usr/bin/env python3

"""
:mod:`regshape.libs.tags.operations` - OCI tag operations
==========================================================

.. module:: regshape.libs.tags.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for listing and deleting OCI image
              tags against OCI Distribution-compliant registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting is the caller's responsibility.
"""

from typing import Optional

import requests

from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, TagError
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.models.tags import TagList
from regshape.libs.refs import format_ref
from regshape.libs.transport import RegistryClient


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def list_tags(
    client: RegistryClient,
    repo: str,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> TagList:
    """Fetch the tag list for *repo*.

    Issues a GET request to ``/v2/{repo}/tags/list``.  The 401→auth→retry
    cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param page_size: Maximum number of tags to return (OCI ``n`` query
                      parameter).  ``None`` omits the parameter.
    :param last: Lexicographic cursor for pagination (OCI ``last`` query
                 parameter).  ``None`` omits the parameter.
    :returns: A :class:`~regshape.libs.models.tags.TagList` instance.
    :raises AuthError: On authentication failure.
    :raises TagError: On a non-2xx registry response or parse failure.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/tags/list"
    params: dict = {}
    if page_size is not None:
        params["n"] = page_size
    if last is not None:
        params["last"] = last

    response = client.get(path, params=params if params else None)
    _raise_for_list_error(response, client.config.registry, repo)

    try:
        return TagList.from_json(response.text)
    except TagError:
        raise
    except Exception as exc:
        raise TagError(
            f"Failed to parse tag-list response from "
            f"{client.config.registry}/{repo}",
            str(exc),
        ) from exc


@track_time
def delete_tag(
    client: RegistryClient,
    repo: str,
    tag: str,
) -> None:
    """Delete *tag* from *repo*.

    The OCI Distribution Spec routes tag deletion through the manifests
    endpoint: ``DELETE /v2/{repo}/manifests/{tag}``.  The 401→auth→retry
    cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param tag: Tag name to delete (must not be a digest).
    :raises AuthError: On authentication failure.
    :raises TagError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/manifests/{tag}"
    response = client.delete(path)
    _raise_for_delete_error(response, client.config.registry, repo, tag)


# ===========================================================================
# Private error helpers
# ===========================================================================


def _raise_for_list_error(
    response: requests.Response,
    registry: str,
    repo: str,
) -> None:
    """Raise on non-2xx responses for a tag-list operation.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.TagError` "Repository not found"
    * other → :class:`~regshape.libs.errors.TagError` generic

    :raises AuthError: On 401.
    :raises TagError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )

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
    """Raise on non-2xx responses for a tag-delete operation.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.TagError` "Tag not found"
    * 400 / 405 → :class:`~regshape.libs.errors.TagError` "not supported"
    * other → :class:`~regshape.libs.errors.TagError` generic

    :raises AuthError: On 401.
    :raises TagError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )
    ref_str = format_ref(registry, repo, tag)

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise TagError(
            f"Tag not found: {ref_str}",
            detail or "HTTP 404",
        )
    if response.status_code in (400, 405):
        raise TagError(
            "Tag deletion is not supported by this registry",
            detail or f"HTTP {response.status_code}",
        )
    raise TagError(
        f"Registry error for {ref_str}",
        detail or f"HTTP {response.status_code}",
    )

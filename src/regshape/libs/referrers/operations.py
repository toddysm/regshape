#!/usr/bin/env python3

"""
:mod:`regshape.libs.referrers.operations` - OCI referrer operations
====================================================================

.. module:: regshape.libs.referrers.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for listing OCI referrers against
              OCI Distribution-compliant registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting is the caller's responsibility.
"""

import re
from typing import Optional

import requests

from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, ReferrerError
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.models.referrer import ReferrerList
from regshape.libs.transport import RegistryClient


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def list_referrers(
    client: RegistryClient,
    repo: str,
    digest: str,
    artifact_type: Optional[str] = None,
) -> ReferrerList:
    """Fetch the referrer list for the manifest identified by *digest*.

    Issues a GET request to ``/v2/{repo}/referrers/{digest}``.  The
    401→auth→retry cycle is handled transparently by *client*.

    When *artifact_type* is provided, the ``artifactType`` query parameter
    is sent to request server-side filtering.  If the registry does not
    support server-side filtering (no ``OCI-Filters-Applied`` response
    header), client-side filtering is applied transparently.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param digest: Manifest digest in ``algorithm:hex`` form
        (e.g. ``sha256:abc123...``).
    :param artifact_type: Optional artifact type to filter on.
        ``None`` omits the parameter.
    :returns: A :class:`~regshape.libs.models.referrer.ReferrerList` instance.
    :raises AuthError: On authentication failure.
    :raises ReferrerError: On a non-2xx registry response or parse failure.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/referrers/{digest}"
    params: dict = {}
    if artifact_type is not None:
        params["artifactType"] = artifact_type

    response = client.get(path, params=params if params else None)
    _raise_for_list_error(response, client.config.registry, repo, digest)

    try:
        result = ReferrerList.from_json(response.text)
    except ReferrerError:
        raise
    except Exception as exc:
        raise ReferrerError(
            f"Failed to parse referrers response from "
            f"{client.config.registry}/{repo}@{digest}",
            str(exc),
        ) from exc

    # Client-side filtering when the registry did not apply server-side
    # filtering.
    if artifact_type is not None:
        filters_applied = response.headers.get("OCI-Filters-Applied", "")
        if "artifactType" not in filters_applied:
            result = result.filter_by_artifact_type(artifact_type)

    return result


@track_scenario("referrer list all")
def list_referrers_all(
    client: RegistryClient,
    repo: str,
    digest: str,
    artifact_type: Optional[str] = None,
) -> ReferrerList:
    """Fetch all pages of the referrer list and return them merged.

    Follows ``Link: rel="next"`` response headers until all pages have been
    retrieved, then returns a single
    :class:`~regshape.libs.models.referrer.ReferrerList` whose ``manifests``
    list is the ordered concatenation of every page.

    Client-side ``artifactType`` filtering is applied once on the final
    merged result after all pages have been collected, ensuring pages
    fetched via bare ``Link``-header URLs are also filtered.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param digest: Manifest digest in ``algorithm:hex`` form.
    :param artifact_type: Optional artifact type to filter on.
    :returns: A single :class:`~regshape.libs.models.referrer.ReferrerList`
        containing all referrers.
    :raises AuthError: On authentication failure.
    :raises ReferrerError: On a non-2xx registry response or parse failure.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    # Fetch the first page via list_referrers (carries @track_time).
    page = list_referrers(client, repo, digest, artifact_type)
    accumulated = ReferrerList(manifests=list(page.manifests))

    # Follow Link headers for subsequent pages.
    while True:
        next_url = _parse_next_url(dict(client.last_response.headers))
        if next_url is None:
            break

        response = client.get(next_url)
        _raise_for_list_error(response, client.config.registry, repo, digest)

        try:
            page = ReferrerList.from_json(response.text)
        except ReferrerError:
            raise
        except Exception as exc:
            raise ReferrerError(
                f"Failed to parse referrers response from "
                f"{client.config.registry}/{repo}@{digest}",
                str(exc),
            ) from exc

        accumulated = accumulated.merge(page)

    # Client-side filtering on the final merged result when the server did
    # not apply server-side filtering.  The first page is already filtered
    # by list_referrers(), but subsequent pages fetched via bare GET are not.
    if artifact_type is not None:
        accumulated = accumulated.filter_by_artifact_type(artifact_type)

    return accumulated


# ===========================================================================
# Private error helpers
# ===========================================================================


def _raise_for_list_error(
    response: requests.Response,
    registry: str,
    repo: str,
    digest: str,
) -> None:
    """Raise on non-2xx responses for a referrers operation.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.ReferrerError` "Manifest not found"
    * other → :class:`~regshape.libs.errors.ReferrerError` generic

    :raises AuthError: On 401.
    :raises ReferrerError: On all other non-2xx status codes.
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
        raise ReferrerError(
            f"Manifest not found: {registry}/{repo}@{digest}",
            detail or "HTTP 404",
        )
    raise ReferrerError(
        f"Registry error for {registry}/{repo}@{digest}",
        detail or f"HTTP {response.status_code}",
    )


def _parse_next_url(headers: dict) -> Optional[str]:
    """Parse the OCI ``Link`` response header and return the next-page URL.

    The OCI pagination ``Link`` header format is::

        Link: </v2/<name>/referrers/<digest>?...>; rel="next"

    Extracts the URL inside ``<...>`` and checks for ``rel="next"``.

    :param headers: Response headers dict.
    :returns: The relative URL string for the next page, or ``None`` if
        there is no next page.
    """
    link_header = headers.get("Link") or headers.get("link") or ""
    if not link_header:
        return None

    for segment in link_header.split(","):
        segment = segment.strip()
        url_match = re.search(r"<([^>]+)>", segment)
        if not url_match:
            continue
        if not re.search(r'rel=["\']?next["\']?', segment):
            continue
        return url_match.group(1)

    return None

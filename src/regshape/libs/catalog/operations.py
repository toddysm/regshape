#!/usr/bin/env python3

"""
:mod:`regshape.libs.catalog.operations` - OCI catalog operations
=================================================================

.. module:: regshape.libs.catalog.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for fetching the OCI repository catalog
              from OCI Distribution-compliant registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting is the caller's responsibility.
"""

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, CatalogError, CatalogNotSupportedError
from regshape.libs.models.catalog import RepositoryCatalog
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.transport import RegistryClient


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def list_catalog(
    client: RegistryClient,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> RepositoryCatalog:
    """Fetch a single page of the repository catalog.

    Issues a ``GET /v2/_catalog`` request.  The 401→auth→retry cycle is
    handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param page_size: Maximum number of repositories to return (OCI ``n``
        query parameter).  ``None`` omits the parameter.
    :param last: Pagination cursor — return repositories lexicographically
        after this value (OCI ``last`` query parameter).  ``None`` omits the
        parameter.
    :returns: A :class:`~regshape.libs.models.catalog.RepositoryCatalog`
        instance for the requested page.
    :raises AuthError: On authentication or authorisation failure.
    :raises CatalogNotSupportedError: If the registry returns ``404`` or
        ``405``, indicating the catalog endpoint is not implemented.
    :raises CatalogError: On any other non-2xx response or parse failure.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    params: dict = {}
    if page_size is not None:
        params["n"] = page_size
    if last is not None:
        params["last"] = last

    response = client.get("/v2/_catalog", params=params if params else None)
    _raise_for_catalog_error(response, client.config.registry)

    try:
        return RepositoryCatalog.from_json(response.text)
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(
            f"Failed to parse catalog response from {client.config.registry}",
            str(exc),
        ) from exc


@track_scenario("catalog list all")
def list_catalog_all(
    client: RegistryClient,
    page_size: Optional[int] = None,
) -> RepositoryCatalog:
    """Fetch all pages of the repository catalog and return them merged.

    Follows ``Link: rel="next"`` response headers until all pages have been
    retrieved, then returns a single
    :class:`~regshape.libs.models.catalog.RepositoryCatalog` whose
    ``repositories`` list is the ordered concatenation of every page.

    :param client: Authenticated transport client for the target registry.
    :param page_size: ``n`` parameter passed to each GET request; controls
        page granularity, not the total result size.  ``None`` lets the
        registry choose its default page size.
    :returns: A single :class:`~regshape.libs.models.catalog.RepositoryCatalog`
        containing all repositories.
    :raises AuthError: On authentication or authorisation failure.
    :raises CatalogNotSupportedError: If the registry does not implement the
        catalog endpoint.
    :raises CatalogError: On any other non-2xx response or parse failure.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    accumulated: list[str] = []
    cursor: Optional[str] = None

    while True:
        # Call list_catalog to get the page
        page = list_catalog(client, page_size=page_size, last=cursor)
        accumulated.extend(page.repositories)

        # Inspect the Link header from client.last_response for next cursor
        cursor = _parse_next_cursor(dict(client.last_response.headers))
        if cursor is None:
            break

    return RepositoryCatalog(repositories=accumulated)


# ===========================================================================
# Private helpers
# ===========================================================================


def _raise_for_catalog_error(
    response: requests.Response,
    registry: str,
) -> None:
    """Raise on non-2xx responses for a catalog operation.

    Status-to-exception mapping:

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 403 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.CatalogNotSupportedError`
    * 405 → :class:`~regshape.libs.errors.CatalogNotSupportedError`
    * other → :class:`~regshape.libs.errors.CatalogError`

    :raises AuthError: On 401 or 403.
    :raises CatalogNotSupportedError: On 404 or 405.
    :raises CatalogError: On all other non-2xx status codes.
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
    if response.status_code == 403:
        raise AuthError(
            f"Authorisation denied for {registry}",
            detail or "HTTP 403",
        )
    if response.status_code in (404, 405):
        raise CatalogNotSupportedError(
            f"Registry does not support the catalog API: {registry}",
            detail or f"HTTP {response.status_code}",
        )
    raise CatalogError(
        f"Registry error for {registry}",
        detail or f"HTTP {response.status_code}",
    )


def _parse_next_cursor(headers: dict) -> Optional[str]:
    """Parse the OCI ``Link`` response header and return the next-page cursor.

    The OCI pagination ``Link`` header format is::

        Link: </v2/_catalog?last=myrepo/myimage&n=100>; rel="next"

    Extracts the URL inside ``<...>``, parses its query string, and returns
    the value of the ``last`` parameter.

    :param headers: Response headers dict (case-insensitive lookup is handled
        by iterating both the provided key and its title-case variant).
    :returns: The ``last`` cursor string for the next page, or ``None`` if
        there is no next page (absent header, no ``rel="next"`` relation, or
        missing ``last`` parameter).
    """
    # requests CaseInsensitiveDict passes through as a plain dict here, so
    # try both the original key and a title-case fallback.
    link_header = headers.get("Link") or headers.get("link") or ""
    if not link_header:
        return None

    # The Link header may carry multiple relations separated by commas.
    # Find the one with rel="next".
    for segment in link_header.split(","):
        segment = segment.strip()
        # Extract the URL inside angle brackets.
        url_match = re.search(r"<([^>]+)>", segment)
        if not url_match:
            continue
        # Check this relation is rel="next".
        if not re.search(r'rel=["\']?next["\']?', segment):
            continue
        url = url_match.group(1)
        qs = parse_qs(urlparse(url).query)
        last_values = qs.get("last")
        if last_values:
            return last_values[0]

    return None

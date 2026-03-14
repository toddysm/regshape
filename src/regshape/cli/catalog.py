#!/usr/bin/env python3

"""
:mod:`regshape.cli.catalog` - CLI commands for OCI catalog operations
======================================================================

.. module:: regshape.cli.catalog
   :platform: Unix, Windows
   :synopsis: Click command group providing a ``list`` subcommand for the
              OCI ``/v2/_catalog`` endpoint.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click
import requests

from regshape.cli.formatting import emit_error, emit_json, emit_list
from regshape.libs.catalog import list_catalog, list_catalog_all
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, CatalogError, CatalogNotSupportedError
from regshape.libs.transport import RegistryClient, TransportConfig


# ===========================================================================
# Public Click group
# ===========================================================================

@click.group()
def catalog():
    """List repositories hosted on an OCI registry."""
    pass


# ===========================================================================
# catalog list
# ===========================================================================

@catalog.command("list")
@telemetry_options
@click.option(
    "--registry",
    "-r",
    required=True,
    metavar="REGISTRY",
    help="Registry hostname (e.g. registry.example.com or acr.io).",
)
@click.option(
    "--n",
    "page_size",
    type=int,
    default=None,
    metavar="COUNT",
    help="Maximum number of repositories to return per page (OCI n parameter).",
)
@click.option(
    "--last",
    default=None,
    metavar="REPO",
    help=(
        "Return repositories lexicographically after this value. "
        "Used together with --n to page through results."
    ),
)
@click.option(
    "--all",
    "fetch_all",
    is_flag=True,
    default=False,
    help="Fetch all pages and return the merged result. Incompatible with --last.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output the full RepositoryCatalog as a JSON object instead of one repository per line.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Write output to this file instead of stdout.",
)
@click.pass_context
@track_scenario("catalog list")
def catalog_list(ctx, registry, page_size, last, fetch_all, as_json, output):
    """List repositories hosted on REGISTRY.

    REGISTRY must be a bare hostname (e.g. ``acr.io`` or
    ``registry.example.com:5000``). Credentials are resolved automatically
    from the credential store populated by ``auth login``.

    Use ``--n`` and ``--last`` to page manually through large catalogs. Use
    ``--all`` to fetch every page automatically and receive a single merged
    list. ``--all`` and ``--last`` are mutually exclusive.

    Use ``--json`` to receive the full response object instead of one
    repository name per line.

    Exit codes: 0=success, 1=HTTP/transport error, 2=usage error,
    3=registry does not support the catalog API.
    """
    if fetch_all and last:
        emit_error(registry, "--all and --last are mutually exclusive", exit_code=2)

    insecure = ctx.obj.get("insecure", False) if ctx.obj else False
    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        if fetch_all:
            result = list_catalog_all(client, page_size=page_size)
        else:
            result = list_catalog(client, page_size=page_size, last=last)
    except CatalogNotSupportedError as exc:
        emit_error(registry, str(exc), exit_code=3)
    except (AuthError, CatalogError, requests.exceptions.RequestException) as exc:
        emit_error(registry, str(exc))

    if as_json:
        emit_json(result.to_dict(), output)
    else:
        emit_list(result.repositories, output)


# ===========================================================================
# Internal helpers — output and error
# ===========================================================================



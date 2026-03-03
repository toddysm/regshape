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

import json
import sys
from typing import Optional

import click
import requests

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
        _error(registry, "--all and --last are mutually exclusive")
        sys.exit(2)

    insecure = ctx.obj.get("insecure", False) if ctx.obj else False
    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        if fetch_all:
            result = list_catalog_all(client, page_size=page_size)
        else:
            result = list_catalog(client, page_size=page_size, last=last)
    except CatalogNotSupportedError as exc:
        _error(registry, str(exc))
        sys.exit(3)
    except (AuthError, CatalogError, requests.exceptions.RequestException) as exc:
        _error(registry, str(exc))
        sys.exit(1)

    if as_json:
        _write(output, json.dumps(result.to_dict(), indent=2))
    else:
        _write(output, "\n".join(result.repositories))


# ===========================================================================
# Internal helpers — output and error
# ===========================================================================

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

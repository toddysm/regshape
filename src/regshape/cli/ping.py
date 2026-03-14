#!/usr/bin/env python3

"""
:mod:`regshape.cli.ping` - CLI command for OCI registry ping
==============================================================

.. module:: regshape.cli.ping
   :platform: Unix, Windows
   :synopsis: Top-level Click command that pings an OCI registry via
              ``GET /v2/`` to verify connectivity and API support.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import sys

import click
import requests

from regshape.cli.formatting import emit_json
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, PingError
from regshape.libs.ping import ping as ping_registry
from regshape.libs.transport import RegistryClient, TransportConfig


# ===========================================================================
# ping command (top-level, not a group)
# ===========================================================================

@click.command()
@telemetry_options
@click.option(
    "--registry",
    "-r",
    required=True,
    metavar="REGISTRY",
    help="Registry hostname (e.g. ghcr.io, localhost:5000).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
@track_scenario("ping")
def ping(ctx, registry, as_json):
    """Ping an OCI registry to verify connectivity and API support.

    Issues ``GET /v2/`` against the target REGISTRY and reports whether
    the registry is reachable, the API version it advertises, and the
    round-trip latency.  Credentials are resolved automatically from
    the credential store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        result = ping_registry(client)
    except AuthError as exc:
        # A 401/403 during token negotiation means the registry *is*
        # reachable but requires credentials.  Report success with a hint.
        if as_json:
            emit_json({
                "registry": registry,
                "reachable": True,
                "api_version": None,
                "latency_ms": None,
                "note": "Registry requires authentication",
                "error": str(exc),
            })
        else:
            click.echo(f"Registry {registry} is reachable")
            click.echo("  Note: Registry requires authentication. "
                       "Run 'regshape auth login' to configure credentials.")
        return
    except PingError as exc:
        detail = str(exc.__cause__) if getattr(exc, "__cause__", None) is not None else str(exc)
        _error(registry, detail, as_json)
        sys.exit(1)
    except requests.exceptions.RequestException as exc:
        _error(registry, str(exc), as_json)
        sys.exit(1)

    if not result.reachable:
        _error(registry, f"HTTP {result.status_code}", as_json)
        sys.exit(1)

    if as_json:
        output = result.to_dict()
        output["registry"] = registry
        emit_json(output)
    else:
        click.echo(f"Registry {registry} is reachable")
        if result.api_version:
            click.echo(f"  API Version: {result.api_version}")
        click.echo(f"  Latency:     {result.latency_ms:.0f}ms")


# ===========================================================================
# Helpers
# ===========================================================================


def _error(registry: str, detail: str, as_json: bool = False) -> None:
    """Print an error message to stderr."""
    if as_json:
        emit_json({
            "registry": registry,
            "reachable": False,
            "error": detail,
        }, err=True)
    else:
        click.echo(f"Error: Registry {registry} is not reachable: {detail}", err=True)

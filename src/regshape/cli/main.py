#!/usr/bin/env python3

"""
:mod: `main` - Top-level CLI entry point
=========================================

    module:: main
    :platform: Unix, Windows
    :synopsis: Top-level Click command group for regshape. Parses global options,
               resolves credentials, constructs context, and registers all
               subcommand groups.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click

from regshape.cli.auth import auth
from regshape.cli.blob import blob
from regshape.cli.catalog import catalog
from regshape.cli.docker import docker
from regshape.cli.layout import layout
from regshape.cli.manifest import manifest
from regshape.cli.ping import ping
from regshape.cli.referrer import referrer
from regshape.cli.tag import tag


@click.group()
@click.option("--insecure", is_flag=True, default=False, help="Allow HTTP (no TLS).")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Verbose output.")
@click.option("--break", "break_mode", is_flag=True, default=False, help="Enable break mode.")
@click.option(
    "--break-rules",
    type=click.Path(exists=True),
    default=None,
    help="Path to break mode rules file.",
)
@click.option(
    "--log-file",
    type=click.Path(),
    default=None,
    help="Path for request/response log output.",
)
@click.pass_context
def regshape(
    ctx,
    insecure,
    verbose,
    break_mode,
    break_rules,
    log_file,
):
    """RegShape — OCI registry manipulation tool."""
    ctx.ensure_object(dict)

    ctx.obj["insecure"] = insecure
    ctx.obj["verbose"] = verbose
    ctx.obj["break_mode"] = break_mode
    ctx.obj["break_rules"] = break_rules
    ctx.obj["log_file"] = log_file

    # RegistryClient will be constructed lazily by subcommands that need it,
    # once the transport layer (libs/transport/) is implemented.


# ---------------------------------------------------------------------------
# Register subcommand groups
# ---------------------------------------------------------------------------

regshape.add_command(auth)
regshape.add_command(blob)
regshape.add_command(catalog)
regshape.add_command(docker)
regshape.add_command(layout)
regshape.add_command(manifest)
regshape.add_command(ping)
regshape.add_command(referrer)
regshape.add_command(tag)


if __name__ == "__main__":
    regshape()

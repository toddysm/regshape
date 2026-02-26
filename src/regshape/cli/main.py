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
from regshape.cli.manifest import manifest
from regshape.libs.auth.credentials import resolve_credentials


@click.group()
@click.option("--registry", "-r", default=None, help="Registry URL.")
@click.option("--username", "-u", default=None, help="Username for authentication.")
@click.option("--password", "-p", default=None, help="Password for authentication.")
@click.option("--insecure", is_flag=True, default=False, help="Allow HTTP (no TLS).")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
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
    registry,
    username,
    password,
    insecure,
    output_json,
    verbose,
    break_mode,
    break_rules,
    log_file,
):
    """RegShape — OCI registry manipulation tool."""
    ctx.ensure_object(dict)

    # Resolve credentials using the full priority chain when a registry is known.
    # Commands that don't need a registry (e.g., auth login) manage credentials
    # themselves using the registry argument passed to that command.
    resolved_username, resolved_password = resolve_credentials(
        registry or "",
        username=username,
        password=password,
    )

    ctx.obj["registry"] = registry
    ctx.obj["username"] = resolved_username
    ctx.obj["password"] = resolved_password
    ctx.obj["insecure"] = insecure
    ctx.obj["output_json"] = output_json
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
regshape.add_command(manifest)


if __name__ == "__main__":
    regshape()

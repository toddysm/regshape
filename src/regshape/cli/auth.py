#!/usr/bin/env python3

"""
:mod:`regshape.cli.auth` - CLI commands for authentication
===========================================================

.. module:: regshape.cli.auth
   :platform: Unix, Windows
   :synopsis: Click command group providing ``login`` and ``logout`` commands
              for OCI registry authentication.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click
import requests

from regshape.cli.formatting import emit_error
from regshape.cli.telemetry import telemetry_options
from regshape.libs.auth import login as registry_login
from regshape.libs.auth.credentials import erase_credentials, resolve_credentials
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, CredentialStoreError


@click.group()
def auth():
    """Manage credentials for OCI registries."""
    pass


@auth.command("login")
@telemetry_options
@click.option(
    "--registry",
    "-r",
    required=True,
    help="Registry hostname (e.g., registry.example.com).",
)
@click.option("--username", "-u", default=None, help="Username (prompted if omitted).")
@click.option(
    "--password",
    "-p",
    default=None,
    help="Password (prompted if omitted).",
)
@click.option(
    "--password-stdin",
    is_flag=True,
    default=False,
    help="Read the password from stdin.",
)
@click.option(
    "--docker-config",
    type=click.Path(),
    default=None,
    help="Alternate Docker config file path.",
)
@click.pass_context
@track_scenario("auth login")
def login(ctx, registry, username, password, password_stdin, docker_config):
    """Authenticate against a registry and persist credentials.

    Credentials can be supplied via --username / --password flags, read from
    stdin with --password-stdin (useful for tokens piped from a secrets
    manager), or resolved automatically from the Docker credential store or
    ~/.docker/config.json.  If both flags are omitted and no stored credentials
    are found, the command prompts interactively.

    The library verifies and persists the resolved credentials, including
    authentication challenge handling. This command owns only credential
    input, error rendering and the success message.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    # --- Resolve password ---------------------------------------------------
    if password_stdin and password is not None:
        raise click.UsageError("--password and --password-stdin are mutually exclusive")
    if password_stdin:
        password = click.get_text_stream("stdin").read().strip()

    # Priority: explicit flag > stored credentials > prompt
    resolved_username, resolved_password = resolve_credentials(
        registry,
        username=username,
        password=password,
        docker_config_path=docker_config,
    )

    # If still missing after resolution, prompt interactively
    if resolved_username is None:
        resolved_username = click.prompt(f"Username for {registry}")
    if resolved_password is None:
        resolved_password = click.prompt(
            f"Password for {registry}", hide_input=True
        )

    try:
        registry_login(
            registry,
            resolved_username,
            resolved_password,
            insecure=insecure,
            docker_config_path=docker_config,
        )
    except CredentialStoreError as e:
        emit_error(registry, f"Could not store credentials: {e.error}")
    except (AuthError, requests.exceptions.RequestException) as e:
        emit_error(registry, str(e))

    # --- Success output ------------------------------------------------------
    click.echo("Login succeeded.")


@auth.command("logout")
@telemetry_options
@click.option(
    "--registry",
    "-r",
    required=True,
    help="Registry hostname (e.g., registry.example.com).",
)
@click.option(
    "--docker-config",
    type=click.Path(),
    default=None,
    help="Alternate Docker config file path.",
)
@click.pass_context
def logout(ctx, registry, docker_config):
    """Remove stored credentials for a registry."""
    try:
        found = erase_credentials(registry, docker_config_path=docker_config)
    except AuthError as e:
        emit_error(registry, str(e))

    if found:
        click.echo(f"Removing login credentials for {registry}.")
    else:
        click.echo(f"Not logged in to {registry}.")



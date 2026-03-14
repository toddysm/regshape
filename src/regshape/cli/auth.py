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
from regshape.libs.auth.credentials import erase_credentials, resolve_credentials, store_credentials
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError
from regshape.libs.transport.client import RegistryClient, TransportConfig


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

    Verification is performed by issuing ``GET /v2/`` through
    :class:`RegistryClient`, which handles the full Bearer challenge /
    401-retry cycle (including the OAuth2 refresh-token exchange for
    registries like ACR).
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

    # --- Verify against registry --------------------------------------------
    try:
        _verify_credentials(registry, resolved_username, resolved_password, insecure=insecure)
    except AuthError as e:
        emit_error(registry, str(e))
    except requests.exceptions.RequestException as e:
        emit_error(registry, str(e))

    # --- Persist credentials ------------------------------------------------
    try:
        store_credentials(
            registry,
            resolved_username,
            resolved_password,
            docker_config_path=docker_config,
        )
    except AuthError as e:
        emit_error(registry, f"Could not store credentials: {e}")

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@track_time
def _verify_credentials(registry: str, username: str, password: str, insecure: bool = False) -> None:
    """
    Verify *username* and *password* against *registry* by issuing
    ``GET /v2/`` through :class:`RegistryClient`.

    All authentication logic (401 challenge handling, WWW-Authenticate
    parsing, Bearer token exchange including the OAuth2 refresh-token
    fallback) is implemented inside the transport layer so that every
    auth flow in the CLI goes through the same code path.

    :param registry: Registry hostname.
    :param username: Username to verify.
    :param password: Password to verify.
    :param insecure: When ``True``, use HTTP instead of HTTPS.
    :raises AuthError: If authentication is rejected.
    :raises requests.exceptions.RequestException: On connection errors.
    """
    config = TransportConfig(
        registry=registry,
        insecure=insecure,
        username=username,
        password=password,
        timeout=10,
    )
    client = RegistryClient(config)
    response = client.get("/v2/")

    if response.status_code == 200:
        return

    raise AuthError(
        "Login failed",
        f"authentication rejected (status {response.status_code})",
    )



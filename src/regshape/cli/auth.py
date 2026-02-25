#!/usr/bin/env python3

"""
:mod: `auth` - CLI commands for authentication
================================================

    module:: auth
    :platform: Unix, Windows
    :synopsis: Click command group providing ``login`` and ``logout`` commands
               for OCI registry authentication.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys

import click
import requests

from regshape.libs.auth import registryauth
from regshape.libs.auth.credentials import erase_credentials, resolve_credentials, store_credentials
from regshape.libs.decorators import get_telemetry_config, redact_headers
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError


@click.group()
def auth():
    """Manage credentials for OCI registries."""
    pass


@auth.command("login")
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

    Verification is performed by issuing a direct HTTP GET to ``/v2/`` using
    the ``requests`` client so that the full Bearer challenge/401-retry cycle
    is executed automatically (required for Docker Hub and similar token-based
    registries). This will migrate to ``RegistryClient`` once the transport
    layer is available.
    """
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False
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
        _error(output_json, registry, str(e))
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        _error(output_json, registry, str(e))
        sys.exit(1)

    # --- Persist credentials ------------------------------------------------
    try:
        store_credentials(
            registry,
            resolved_username,
            resolved_password,
            docker_config_path=docker_config,
        )
    except AuthError as e:
        _error(output_json, registry, f"Could not store credentials: {e}")
        sys.exit(1)

    # --- Success output ------------------------------------------------------
    if output_json:
        click.echo(json.dumps({"status": "success", "registry": registry}))
    else:
        click.echo("Login succeeded.")


@auth.command("logout")
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
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False

    try:
        found = erase_credentials(registry, docker_config_path=docker_config)
    except AuthError as e:
        _error(output_json, registry, str(e))
        sys.exit(1)

    if output_json:
        click.echo(json.dumps({"status": "success", "registry": registry}))
    elif found:
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
    ``GET /v2/`` and completing the full Bearer challenge cycle.

    Uses the ``requests`` library directly with the existing
    :func:`~regshape.libs.auth.registryauth.authenticate` helper so that the
    ``RegistryClient`` (not yet constructed at CLI setup time) is not required.

    The URL scheme defaults to HTTPS but switches to HTTP when *insecure* is
    ``True`` (set via the global ``--insecure`` flag).

    :param registry: Registry hostname.
    :param username: Username to verify.
    :param password: Password to verify.
    :param insecure: When ``True``, use HTTP instead of HTTPS.
    :raises AuthError: If authentication is rejected.
    :raises requests.exceptions.RequestException: On connection errors.
    """
    scheme = "http" if insecure else "https"
    url = f"{scheme}://{registry}/v2/"
    telemetry = get_telemetry_config()

    # First request — expect a challenge or immediate success
    response = requests.get(url, timeout=10)
    _debug_http(telemetry, "GET", url, {}, response)

    if response.status_code == 200:
        return  # No auth required (unusual but valid)

    if response.status_code == 401:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError("Login failed", "Registry returned 401 without WWW-Authenticate header")

        auth_value = registryauth.authenticate(www_auth, username, password)
        auth_scheme = www_auth.split(" ")[0]
        auth_headers = {"Authorization": f"{auth_scheme} {auth_value}"}

        retry = requests.get(url, headers=auth_headers, timeout=10)
        # auth_scheme comes from the server's WWW-Authenticate header (not
        # derived from the password), so this dict contains no tainted data.
        _debug_http(telemetry, "GET", url, {"Authorization": f"{auth_scheme} <redacted>"}, retry)
        if retry.status_code == 200:
            return
        raise AuthError(
            "Login failed",
            f"authentication rejected (status {retry.status_code})",
        )

    raise AuthError("Login failed", f"unexpected status {response.status_code}")


def _error(output_json: bool, registry: str, reason: str) -> None:
    """Print an error message in the appropriate format."""
    if output_json:
        click.echo(
            json.dumps({"status": "error", "registry": registry, "message": reason}),
            err=True,
        )
    else:
        click.echo(f"Error for {registry}: {reason}", err=True)


def _debug_http(telemetry, method: str, url: str, req_headers: dict, response) -> None:
    """Print request/response details to stderr when ``--debug-calls`` is active.

    Follows the same general output format as the ``@debug_call`` decorator so
    that auth calls appear consistently alongside future ``RegistryClient``
    calls, but deliberately redacts sensitive ``Authorization`` header values
    for CLI authentication flows.

    :param telemetry: Active :class:`~regshape.libs.decorators.TelemetryConfig`.
    :param method: HTTP method string (e.g., ``"GET"``).
    :param url: Full request URL.
    :param req_headers: Request headers dict (``Authorization`` values are redacted).
    :param response: ``requests.Response`` object.
    """
    if not telemetry.debug_calls_enabled:
        return
    out = telemetry.output
    # Callers are responsible for sanitizing request headers before passing
    # them here (no tainted data should reach this function). Response headers
    # are sanitized here since callers pass the raw response object.
    safe_resp = redact_headers(dict(response.headers))
    print(f"[CALL] {method} {url}", file=out)
    print("[REQUEST HEADERS]", file=out)
    for key, value in req_headers.items():
        print(f"  {key}: {value}", file=out)
    print(f"[RESPONSE HEADERS] {response.status_code}", file=out)
    for key, value in safe_resp.items():
        print(f"  {key}: {value}", file=out)

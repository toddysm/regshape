#!/usr/bin/env python3

"""
:mod: `credentials` - Credential resolution for OCI registries
==============================================================

    module:: credentials
    :platform: Unix, Windows
    :synopsis: Resolves credentials for an OCI registry using a priority chain:
               explicit flags → Docker credHelpers → docker config auths → anonymous.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import base64
import json
import logging
import os

from typing import Optional, Tuple

from regshape.libs.auth import dockerconfig, dockercredstore
from regshape.libs.errors import AuthError

log = logging.getLogger(__name__)


def resolve_credentials(
        registry: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        docker_config_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve credentials for *registry* using the following priority chain:

    1. Explicit *username* / *password* arguments (highest priority).
    2. Docker ``credHelpers`` entry for the registry — calls the credential
       helper via :mod:`dockercredstore`.
    3. Base64-encoded entry in the ``auths`` section of the Docker config file
       loaded by :mod:`dockerconfig`.
    4. Anonymous — returns ``(None, None)`` (lowest priority).

    :param registry: The registry hostname (e.g., ``registry.example.com``).
    :type registry: str
    :param username: Explicit username, or ``None`` to use stored credentials.
    :type username: str, optional
    :param password: Explicit password, or ``None`` to use stored credentials.
    :type password: str, optional
    :param docker_config_path: Alternate path to the Docker config file.
    :type docker_config_path: str, optional
    :returns: A ``(username, password)`` tuple. Either value may be ``None``
              when using anonymous access.
    :rtype: tuple[str | None, str | None]
    """

    # Priority 1 — explicit flags
    if username is not None and password is not None:
        log.debug("Using explicit credentials for %s", registry)
        return username, password

    # Priority 2 — Docker credHelpers
    cred_helper = _get_cred_helper(registry, docker_config_path)
    if cred_helper:
        log.debug("Using credential helper '%s' for %s", cred_helper, registry)
        try:
            creds = dockercredstore.get(store=cred_helper, registry=registry)
            stored_username = creds.get('Username')
            stored_password = creds.get('Secret')
            if stored_username and stored_password:
                return stored_username, stored_password
        except AuthError as e:
            log.debug("Credential helper lookup failed for %s: %s", registry, e)

    # Priority 3 — docker config auths section
    config = dockerconfig.load_config(docker_config_path)
    if config:
        stored = _get_auth_from_config(registry, config)
        if stored:
            log.debug("Using docker config auths entry for %s", registry)
            return stored

    # Priority 4 — anonymous
    log.debug("No credentials found for %s; using anonymous access", registry)
    return None, None


def store_credentials(
        registry: str,
        username: str,
        password: str,
        docker_config_path: Optional[str] = None,
) -> None:
    """
    Persist *username* and *password* for *registry*.

    If the Docker config file has a ``credHelpers`` entry for the registry, the
    credentials are stored via the named credential helper. Otherwise they are
    written as a Base64-encoded entry in the ``auths`` section of
    ``~/.docker/config.json`` (or the path given by *docker_config_path*).

    :param registry: The registry hostname.
    :type registry: str
    :param username: Username to store.
    :type username: str
    :param password: Password (or access token) to store.
    :type password: str
    :param docker_config_path: Alternate path to the Docker config file.
    :type docker_config_path: str, optional
    :raises AuthError: If storing credentials fails.
    """
    cred_helper = _get_cred_helper(registry, docker_config_path)
    if cred_helper:
        log.debug("Storing credentials via helper '%s' for %s", cred_helper, registry)
        dockercredstore.store(
            store=cred_helper,
            registry=registry,
            credentials={'Username': username, 'Secret': password},
        )
        return

    # Fall back to docker config auths
    config_file = dockerconfig.get_config_file(docker_config_path)
    if config_file is None:
        # Create a minimal config file at the default location
        config_file = os.path.join(dockerconfig.home_dir(),
                                   dockerconfig.DOCKER_CONFIG_FILENAME)
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        config = {}
    else:
        config = dockerconfig.load_config(docker_config_path) or {}

    auth_token = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("utf-8")

    config.setdefault("auths", {})[registry] = {"auth": auth_token}

    try:
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)
        log.debug("Stored credentials for %s in %s", registry, config_file)
    except OSError as e:
        log.error("Failed to write docker config: %s", e)
        raise AuthError("Could not store credentials", str(e))


def erase_credentials(
        registry: str,
        docker_config_path: Optional[str] = None,
) -> bool:
    """
    Remove stored credentials for *registry*.

    :param registry: The registry hostname.
    :type registry: str
    :param docker_config_path: Alternate path to the Docker config file.
    :type docker_config_path: str, optional
    :returns: ``True`` if credentials were found and removed, ``False`` if no
              credentials were stored for the registry.
    :rtype: bool
    :raises AuthError: If erasing credentials fails.
    """
    cred_helper = _get_cred_helper(registry, docker_config_path)
    if cred_helper:
        log.debug("Erasing credentials via helper '%s' for %s", cred_helper, registry)
        try:
            dockercredstore.erase(store=cred_helper, registry=registry)
            return True
        except AuthError as e:
            log.debug("Credential helper erase failed for %s: %s", registry, e)
            raise

    # Fall back to docker config auths
    config_file = dockerconfig.get_config_file(docker_config_path)
    if config_file is None:
        return False

    config = dockerconfig.load_config(docker_config_path) or {}
    auths = config.get("auths", {})
    if registry not in auths:
        return False

    del auths[registry]
    config["auths"] = auths

    try:
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)
        log.debug("Erased credentials for %s from %s", registry, config_file)
    except OSError as e:
        log.error("Failed to write docker config: %s", e)
        raise AuthError("Could not erase credentials", str(e))

    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_cred_helper(
        registry: str,
        docker_config_path: Optional[str] = None,
) -> Optional[str]:
    """
    Return the credential helper name configured for *registry* in the Docker
    config ``credHelpers`` map, or ``None`` if no helper is configured.

    :param registry: The registry hostname.
    :type registry: str
    :param docker_config_path: Alternate Docker config path.
    :type docker_config_path: str, optional
    :returns: Helper name (e.g., ``"desktop"``) or ``None``.
    :rtype: str | None
    """
    config = dockerconfig.load_config(docker_config_path)
    if not config:
        return None
    return config.get("credHelpers", {}).get(registry)


def _get_auth_from_config(
        registry: str,
        config: dict,
) -> Optional[Tuple[str, str]]:
    """
    Extract and decode Base64 credentials from the ``auths`` section of a
    Docker config dict.

    :param registry: The registry hostname.
    :type registry: str
    :param config: Parsed Docker config dictionary.
    :type config: dict
    :returns: ``(username, password)`` tuple or ``None``.
    :rtype: tuple[str, str] | None
    """
    auths = config.get("auths", {})
    entry = auths.get(registry)
    if not entry:
        return None

    auth_b64 = entry.get("auth")
    if not auth_b64:
        # Some entries store username/password directly
        username = entry.get("username") or entry.get("Username")
        password = entry.get("password") or entry.get("Secret")
        if username and password:
            return username, password
        return None

    try:
        decoded = base64.b64decode(auth_b64).decode("utf-8")
        username, _, password = decoded.partition(":")
        if username and password:
            return username, password
    except Exception as e:
        log.debug("Failed to decode auth entry for %s: %s", registry, e)

    return None

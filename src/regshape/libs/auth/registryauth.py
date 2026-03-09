#!/usr/bin/env python3

"""
:mod: `registryauth` - Module for sign into OCI registries
===========================================================

    module:: registryauth
    :platform: Unix, Windows
    :synopsis: Module to retrieve authentication information for an OCI registry.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import base64
import json
import logging
import requests

from regshape.libs.errors import AuthError
from typing import Optional

log = logging.getLogger(__name__)

def _parse_auth_header(auth_header: str) -> dict:
    """
    Parses the authentication header and returns a dictionary of the parameters.
    It can parse any authentication header used in `www-authenticate` headers.
    See https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/WWW-Authenticate 
    for more.
    
    :param auth_header: The authentication header
    :type auth_header: str
    :return: The authentication header as a dictionary
    :rtype: dict
    """
    scheme = auth_header.split(" ")[0]
    params_str = auth_header[len(scheme):].strip()

    # Split on commas that are outside quoted strings so that values
    # containing commas (e.g. scope="repository:repo:pull,push") are
    # kept intact.
    params = _split_auth_params(params_str)
    result = {}
    for param in params:
        param = param.strip()
        if '=' not in param:
            continue
        key, value = param.split('=', 1)
        result[key.strip()] = value.strip().strip('"')
    result['scheme'] = scheme

    return result


def _split_auth_params(params_str: str) -> list:
    """Split a WWW-Authenticate parameter string on commas outside quotes."""
    parts = []
    current = []
    in_quotes = False
    for ch in params_str:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == ',' and not in_quotes:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))
    return parts

def _get_basic_auth(username: str, password: str) -> str:
    """
    Returns the basic authentication credentials as Base64 string.
    :param username: The username to use for authentication
    :type username: str
    :param password: The password to use for authentication
    :type password: str
    :return: The basic authentication credentials as Base64 string
    :rtype: str
    """
    auth_string = f"{username}:{password}"
    return base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

def _get_auth_token(
        auth_header: str, 
        username: Optional[str] = None, 
        password: Optional[str] = None
        ) -> str:
    """
    Signs into an OCI registry using the token authentication method. See
    https://distribution.github.io/distribution/spec/auth/token/ for more.

    :param auth_header: The parsed authentication header dictionary
    :type auth_header: dict
    :param username: The username to use for authentication
    :type username: str
    :param password: The password to use for authentication
    :type password: str
    :return: The bearer token string
    :rtype: str
    """

    # Ensure realm is present
    try:
        if not auth_header['realm']:
            log.error("Missing authentication realm")
            raise AuthError("Missing authentication realm")
        else:
            realm = auth_header['realm']
    except KeyError:
        log.error("Missing authentication realm")
        raise AuthError("Missing authentication realm")

    # Ensure service is present
    param_keys = []

    try:
        if auth_header['service']:
            param_keys.append('service')
        else:
            log.error("Missing authentication header parameter: `service`")
            raise AuthError("Missing authentication header parameter: `service`")        
    except KeyError:
        log.error("Missing authentication header parameter: `service`")
        raise AuthError("Missing authentication header parameter: `service``")

    # Check scope is present (optional)
    try:
        if auth_header['scope']:
            param_keys.append('scope')
    except KeyError:
        log.debug("Missing authentication header parameter: `scope`")
        pass

    # Get the parameters from the authentication request
    query_params = {key: auth_header[key] for key in param_keys}

    # Make a request to the `realm` with `service` and `scope` endpoint to get the token
    try:
        if username and password:
            response = requests.get(realm, params=query_params, auth=(username, password))
        else:
            response = requests.get(realm, params=query_params)
    except requests.exceptions.ConnectionError as e:
        log.error(e)
        raise AuthError("Token request failed", f"Unable to connect to {realm}")
    except requests.exceptions.Timeout as e:
        log.error(e)
        raise AuthError("Token request failed", f"Connection to {realm} timed out")
    except requests.exceptions.RequestException as e:
        log.error(e)
        raise AuthError("Token request failed", f"Request to {realm} failed: {e}")

    # If the standard GET failed with 401 and we have a password, try an
    # OAuth2 refresh-token exchange via POST.  This is needed for registries
    # like ACR where `az acr login` stores a refresh token (JWT) in the
    # Docker credential store instead of a plain username/password pair.
    if response.status_code == 401 and password:
        response = _try_refresh_token_exchange(realm, query_params, password)

    # Parse the response
    if response.status_code != 200:
        log.error(f"Token request failed with status {response.status_code}")
        raise AuthError("Token request failed", f"Status code: {response.status_code}")

    token_response = json.loads(response.text)

    # OCI spec allows either 'token' or 'access_token'; prefer 'access_token'
    token = token_response.get('access_token') or token_response.get('token')
    if not token:
        log.error("Token response missing both 'access_token' and 'token' fields")
        raise AuthError("Token response missing token field")

    return token


def _try_refresh_token_exchange(
        realm: str,
        query_params: dict,
        refresh_token: str,
) -> requests.Response:
    """Attempt an OAuth2 ``refresh_token`` grant against *realm*.

    Registries like Azure Container Registry store a refresh token (JWT) in
    the Docker credential store (via ``az acr login``).  These tokens cannot
    be exchanged with a standard GET + Basic-Auth request; instead the token
    endpoint expects a POST with ``grant_type=refresh_token``.

    :param realm: Token endpoint URL from the ``WWW-Authenticate`` header.
    :param query_params: ``service`` and optional ``scope`` parameters.
    :param refresh_token: The refresh token (password value from the
        credential store).
    :returns: The :class:`requests.Response` from the POST.
    :raises AuthError: On connection or transport errors.
    """
    post_data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    }
    post_data.update(query_params)

    log.debug("Attempting OAuth2 refresh-token exchange at %s", realm)
    try:
        return requests.post(realm, data=post_data)
    except requests.exceptions.ConnectionError as e:
        log.error(e)
        raise AuthError("Token request failed", f"Unable to connect to {realm}")
    except requests.exceptions.Timeout as e:
        log.error(e)
        raise AuthError("Token request failed", f"Connection to {realm} timed out")
    except requests.exceptions.RequestException as e:
        log.error(e)
        raise AuthError("Token request failed", f"Request to {realm} failed: {e}")

def authenticate(
        auth_header: str,
        username: Optional[str] = None,
        password: Optional[str] = None
        ) -> str:
    """
    Authenticates the user based on the authentication header. The authentication 
    header

    :param auth_header: The authentication header
    :type auth_header: str
    :return: The authentication string to use in the request
    :rtype: str
    """
    auth_header = _parse_auth_header(auth_header)
    scheme = auth_header['scheme'].lower()
    if scheme == 'basic':
        return _get_basic_auth(username, password)
    elif scheme == 'bearer':
        return _get_auth_token(auth_header, username, password)
    else:
        log.error(f"Unknown authentication method: {auth_header['scheme']}")
        raise AuthError(f"Unknown authentication method: {auth_header['scheme']}")

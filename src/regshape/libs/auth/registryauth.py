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

from regshape.libs.decorators.telemetry import executiontime_decorator
from regshape.libs.errors import AuthError
from typing import Optional
from urllib.parse import parse_qs, urlparse

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
    params = auth_header[len(scheme):].strip()

    params = params.split(",")
    params = [param.replace('"', '') for param in params]
    params = [param.split('=') for param in params]
    auth_header = dict(params)
    auth_header['scheme'] = scheme

    return auth_header

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

    :param url: The URL to the registry resource
    :type url: str
    :param username: The username to use for authentication
    :type username: str
    :param password: The password to use for authentication
    :type password: str
    :return: The authentication token
    :rtype: dict
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
    if username and password:
        # Make a request with authentication
        response = requests.get(realm, params=query_params, auth=(f'{username}', f'{password}'))
    else:
        # Make an anonymous request
        response = requests.get(realm, params=query_params)

    # Parse the response
    token = json.loads(response.text)

    return token

@executiontime_decorator
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
    if auth_header['scheme'] == 'Basic':
        return _get_basic_auth(username, password)
    elif auth_header['scheme'] == 'Bearer':
        return _get_auth_token(auth_header, username, password)
    else:
        log.error(f"Unknown authentication method: {auth_header['scheme']}")
        raise AuthError(f"Unknown authentication method: {auth_header['scheme']}")

#!/usr/bin/env python3

"""
:mod: `registryauth` - Module for sign into OCI registries
===========================================================

    module:: registryauth
    :platform: Unix, Windows
    :synopsis: Module for sign into OCI registries.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import requests

from typing import Optional
from urllib.parse import parse_qs, urlparse

def signin(
        url: str, 
        username: Optional[str] = None, 
        password: Optional[str] = None
        ) -> str:
    """
    Sign into an OCI registry.

    :param url: The hostname of the registry
    :type url: str
    :param username: The username to use for authentication
    :type username: str
    :param password: The password to use for authentication
    :type password: str
    """
    print (url, username, password)
    # Make a request to the /v2 endpoint to get the challenge
    response = requests.get(url)
    return_code = response.status_code
    if return_code != 401:
        auth_header = response.headers["www-authenticate"]
    # Parse the www-authenticate header to get the `realm`, `service`, and `scope` parameters
    if auth_header is None:
        raise Exception("No authentication header found")
    elif auth_header.startswith("Bearer "):
        auth_header = auth_header[len("Bearer "):]
        parts = auth_header.split(",")
        parts = [part.replace('"', '') for part in parts]
        parts = [part.split('=') for part in parts]
        parts = dict(parts)
    
    params = {
        'service': parts['service'],
        'scope': parts['scope']
    }

    # Make a request to the `realm` with `service` and `scope` endpoint to get the token
    response = requests.get(parts['realm'], params=params, auth=('username', 'password'))
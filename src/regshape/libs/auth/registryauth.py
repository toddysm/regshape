#!/usr/bin/env python3

"""
:mod: `registryauth` - Module for sign into OCI registries
===========================================================

    module:: registryauth
    :platform: Unix, Windows
    :synopsis: Module for sign into OCI registries.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import base64
import requests
import json

from typing import Optional
from urllib.parse import parse_qs, urlparse

def get_basic_auth(username: str, password: str):
    """
    Returns the basic authentication credentials as Base64 string.
    :param username: The username to use for authentication
    :type username: str
    :param password: The password to use for authentication
    :type password: str
    :return: The basic authentication credentials as Base64 string
    """
    auth_string = f"{username}:{password}"
    return base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

def get_auth_token(
        url: str, 
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
    """
    print (url, username, password)
    # Make a request to the /v2 endpoint to get the challenge
    response = requests.get(url)
    return_code = response.status_code
    if return_code == 401:
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
    
    # TODO: `scope` may be missing from the parts, validate this

    params = {
        'service': parts['service'],
        'scope': parts['scope']
    }

    # Make a request to the `realm` with `service` and `scope` endpoint to get the token
    response = requests.get(parts['realm'], params=params, auth=(f'{username}', f'{password}'))
    # parse the response
    body = json.loads(response.text)
    # get the auth token
    token = body['token']

    headers = {
        'Authorization': f'Bearer {token}',
    }
    response = requests.get(url, headers=headers)
    response.text
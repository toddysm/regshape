#!/usr/bin/env python3

"""
:mod: `dockercredstore` - Module for working with Docker credential store
=========================================================================

    module:: dockerconfig
    :platform: Unix, Windows
    :synopsis: Module for working with Docker credential store. Allows for 
                listing, storing, retrieving, and erasing credentials.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import logging

from subprocess import PIPE, STDOUT, Popen

log = logging.getLogger(__name__)

def list(store='desktop'):
    """
    Lists the credentials stored in the credential store.
    :param store: The type of the credential store. 
        (default is ``desktop``, the default wrapper)
    :type store: str
    :returns: The list of credentials
    :rtype: dict
    """
    credstore_cmd = [f"docker-credential-{store}", "list"]
    p = Popen(credstore_cmd, stdout=PIPE, stdin=PIPE, stderr=STDOUT)
    credentials_json, _ = p.communicate()
    return json.loads(credentials_json.decode('utf-8'))

def get(store='desktop', registry=None):
    """
    Lists the credentials stored in the credential store.
    :param store: The type of the credential store. 
        (default is ``desktop``, the default wrapper)
    :type store: str
    :param registry: The registry for which to obtain the credentials. Must be
        the full DNS name. (default is ``None``)
    :type store: str
    :returns: The credentials
    :rtype: dict
    """
    credstore_cmd = [f"docker-credential-{store}", "get"]
    p = Popen(credstore_cmd, stdout=PIPE, stdin=PIPE, stderr=STDOUT)
    credentials_json, _ = p.communicate(input=registry.encode('utf-8'))
    return json.loads(credentials_json.decode('utf-8'))

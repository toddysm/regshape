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

from regshape.libs.errors import *
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
    :raises AuthError: If Docker credential helpers are not configured
    """
    credstore_cmd = [f"docker-credential-{store}", "list"]
    try:
        p = Popen(credstore_cmd, stdout=PIPE, stdin=PIPE, stderr=STDOUT)
        credentials_json, _ = p.communicate()
        credential_list = json.loads(credentials_json.decode('utf-8'))
    except FileNotFoundError as e:
        log.error(e)
        raise AuthError("Error while listing credentials", f"docker-credential-{store} cannot be found")
    except json.JSONDecodeError as e:
        log.error(e)
        raise AuthError("Error while listing credentials", f"docker-credential-{store} returned invalid JSON")
    except Exception as e:
        log.error(e)
        raise AuthError("Error while listing credentials", f"docker-credential-{store} returned an unknown error")
    return credential_list

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
    :raises AuthError: If Docker credential helpers are not configured
    """
    credstore_cmd = [f"docker-credential-{store}", "get"]
    try:
        p = Popen(credstore_cmd, stdout=PIPE, stdin=PIPE, stderr=STDOUT)
        credentials_json, _ = p.communicate(input=registry.encode('utf-8'))
        credentials = json.loads(credentials_json.decode('utf-8'))
    except FileNotFoundError as e:
        log.error(e)
        raise AuthError("Error while getting credentials", f"docker-credential-{store} cannot be found")
    except json.JSONDecodeError as e:
        log.error(e)
        raise AuthError("Error while getting credentials", f"docker-credential-{store} returned invalid JSON")
    except Exception as e:
        log.error(e)
        raise AuthError("Error while getting credentials", f"docker-credential-{store} returned an unknown error")
    return credentials

def erase(store='desktop', registry=None):
    """
    Erases the credentials stored in the credential store.
    :param store: The type of the credential store. 
        (default is ``desktop``, the default wrapper)
    :type store: str
    :param registry: The registry for which to obtain the credentials. Must be
        the full DNS name. (default is ``None``)
    :type store: str
    :raises AuthError: If Docker credential helpers are not configured
    """
    credstore_cmd = [f"docker-credential-{store}", "erase"]
    try:
        p = Popen(credstore_cmd, stdout=PIPE, stdin=PIPE, stderr=STDOUT)
        p.communicate(input=registry.encode('utf-8'))
    except FileNotFoundError as e:
        log.error(e)
        raise AuthError("Error while erasing credentials", f"docker-credential-{store} cannot be found")
    except Exception as e:
        log.error(e)
        raise AuthError("Error while erasing credentials", f"docker-credential-{store} returned an unknown error")

def store(store='desktop', registry=None, credentials=None):
    """
    stores the credentials in the credential store.
    :param store: The type of the credential store. 
        (default is ``desktop``, the default wrapper)
    :type store: str
    :param registry: The registry for which to obtain the credentials. Must be
        the full DNS name. (default is ``None``)
    :type store: str
    :param credentials: The credentials to store. Uses ``Username`` and ``Secret``
        as keys.
    :type credentials: dict
    :raises AuthError: If Docker credential helpers are not configured
    """
    credstore_cmd = [f"docker-credential-{store}", "store"]
    try:
        p = Popen(credstore_cmd, stdout=PIPE, stdin=PIPE, stderr=STDOUT)
        p.communicate(input=registry.encode('utf-8'))
        credentials_json = json.dumps(credentials)
        p.communicate(input=credentials_json.encode('utf-8'))
    except FileNotFoundError as e:
        log.error(e)
        raise AuthError("Error while storing credentials", f"docker-credential-{store} cannot be found")
    except Exception as e:
        log.error(e)
        raise AuthError("Error while storing credentials", f"docker-credential-{store} returned an unknown error")

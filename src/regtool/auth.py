#!/usr/bin/env python3

"""
:mod: `auth` - Module for OCI registry authentication 
=====================================================

    module:: auth
    :platform: Unix, Windows
    :synopsis: Module implementing the authentication with OCI registries
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import logging

from regtool.errors import *

log = logging.getLogger(__name__)

def login(username, password=None, email=None, registry=None,
              reauth=False, dockercfg_path=None):
    """
    Authenticate with an OCI registry. Similar to the ``docker login`` command.
    If Docker credential store is used, credentials information is stored in the
    Docker credential store. If not, the credentials are stores in the following
    environment variables: ``REGTOOL_REGISTRY``, ``REGTOOL_USERNAME``, and 
    ``REGTOOL_PASSWORD``

    :param username: The registry user name 
        (default is None)
    :type username: str
    :param password: The registry password
        (default is None)
    :type password: str
    :param email: The email for the registry account
        (default is None)
    :type email: str
    :param registry: The hostname of the registry
        (default is None)
    :type registry: str
    :param reauth: Whether or not to refresh existing authentication with the 
        OCI registry
        (default is False)
    :type reauth: bool
    :param dockercfg_path: Path to Docker config file
        (default ``$HOME/.docker/config.json`` if present)
    :returns: The response from the login request
    :rtype: dict
    :raises ExceptionType: 
    """

def logout(registry=None, dockercfg_path=None):
    """
    Logsout the user and removes the stored credentials. If Docker credential 
    store is used, the credentials are removed from the credential store. If not,
    the environment variables ``REGTOOL_REGISTRY``, ``REGTOOL_USERNAME``, and 
    ``REGTOOL_PASSWORD`` are cleared.

    NOTE: It is recommended to logout after every session if Docker credential store
    is not used.
    """
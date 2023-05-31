#!/usr/bin/env python3

"""
:mod: `dockerconfig` - Module for reading Docker configuration
==============================================================

    module:: dockerconfig
    :platform: Unix, Windows
    :synopsis: Module reading Docker configuration from the environment or the
                configuration file.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import logging
import os

from ..constants import IS_WINDOWS_PLATFORM

DOCKER_CONFIG_FILENAME = os.path.join('.docker', 'config.json')

log = logging.getLogger(__name__)

def home_dir():
    """
    Return the user's home directory using ``%USERPROFILE%`` on Windows or 
    ``$HOME/getuid`` on Posix
    :returns: The aboslute path to the user's home directory
    :rtype: string
    """
    if IS_WINDOWS_PLATFORM:
        return os.environ.get('USERPROFILE')
    else:
        return os.path.expanduser('~')

def config_path_from_env():
    """
    Return the value of the ``DOCKER_CONFIG`` environment variable
    :returns: The ``DOCKER_CONFIG`` environment variable. None if it is not set
    :rtype: string
    """
    config_dir = os.environ.get('DOCKER_CONFIG')
    if not config_dir:
        return None
    return os.path.join(config_dir, os.path.basename(DOCKER_CONFIG_FILENAME))

def get_config_file(config_path=None):
    """
    Return the path to the Docker configuration file if found. The search order
    is: 1.) specified alternate path 2.) ``DOCKER_CONFIG`` environment variable
    3.) default Docker ``config.json`` location
    :param config_path: Alternate path to Docker configuration file to look at 
        (default is None)
    :type config_path: str
    :returns: The path to the Docker configuration file. None if it is not found
    :rtype: string
    """
    paths = list(filter(None, [
        config_path,
        config_path_from_env(),
        os.path.join(home_dir(), DOCKER_CONFIG_FILENAME)
    ]))

    for path in paths:
        log.debug(f"Trying path: {path}")

        if os.path.exists(path):
            log.debug(f"Found Docker configuration file at path: {path}")
            return path

    log.debug("Docker configuration file not found")

    return None

def load_config(config_path=None):
    """
    Loads the Docker configuration file if found. The search order
    is: 1.) specified alternate path 2.) ``DOCKER_CONFIG`` environment variable
    3.) default Docker ``config.json`` location
    :param config_path: Alternate path to Docker configuration file to look at 
        (default is None)
    :type config_path: str
    :returns: The Docker configuration. None if it is not found
    :rtype: dict
    :raises OSError: If reading the file fails
    :raises ValueError: If the file cannot be parsed
    """
    config_file = get_config_file(config_path)

    if not config_file:
        return None

    try:
        with open(config_file) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.debug(e)

    log.debug("Unable to load Docker configuration.")
    return None

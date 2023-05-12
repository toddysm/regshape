#!/usr/bin/env python
#-*- coding: utf-8 -*-

"""
:mod: `twit2gml` - Twitter Network export to GML
================================================

    module:: twit2gml
    :platform: Unix, Windows
    :synopsis: Main module for exporting Twitter network information to GML
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

"""
Authenticate with an OCI registry. Similar to the ``docker login`` command.

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
    (default ``$HOME/.docker/config.json`` if present,
    otherwise ``$HOME/.dockercfg``)
:returns: The response from the login request
:rtype: dict
:raises ExceptionType: 
"""
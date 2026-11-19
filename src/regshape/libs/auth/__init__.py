#!/usr/bin/env python3

"""Registry authentication helpers, including verification and persisted login."""

from regshape.libs.auth.credentials import login, verify_credentials

__all__ = [
    'credentials',
    'dockerconfig',
    'dockercredstore',
    'login',
    'registryauth',
    'verify_credentials',
]
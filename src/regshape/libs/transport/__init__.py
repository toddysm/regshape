#!/usr/bin/env python3

"""
:mod:`regshape.libs.transport` - HTTP transport layer for OCI registry communication
=====================================================================================

.. module:: regshape.libs.transport
   :platform: Unix, Windows
   :synopsis: Provides :class:`RegistryClient` and :class:`TransportConfig` —
              the single HTTP chokepoint through which all registry traffic
              flows. Handles credential resolution, WWW-Authenticate challenge
              parsing, and the 401 → authenticate → retry cycle so that domain
              operation modules and CLI commands never deal with raw HTTP auth.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.transport.client import RegistryClient, TransportConfig
from regshape.libs.transport.models import RegistryRequest, RegistryResponse

__all__ = [
    "RegistryClient",
    "TransportConfig",
    "RegistryRequest", 
    "RegistryResponse",
]

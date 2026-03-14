#!/usr/bin/env python3

"""
:mod:`regshape.libs.ping` - Domain operations for OCI registry ping
=====================================================================

.. module:: regshape.libs.ping
   :platform: Unix, Windows
   :synopsis: Library-level functions for OCI registry ping operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.ping.operations import (
    PingResult,
    ping,
)

__all__ = [
    "PingResult",
    "ping",
]

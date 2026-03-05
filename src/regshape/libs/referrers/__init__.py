#!/usr/bin/env python3

"""
:mod:`regshape.libs.referrers` - Domain operations for OCI referrers
=====================================================================

.. module:: regshape.libs.referrers
   :platform: Unix, Windows
   :synopsis: Library-level functions for OCI referrer operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.referrers.operations import (
    list_referrers,
    list_referrers_all,
)

__all__ = [
    "list_referrers",
    "list_referrers_all",
]

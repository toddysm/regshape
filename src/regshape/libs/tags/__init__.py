#!/usr/bin/env python3

"""
:mod:`regshape.libs.tags` - Domain operations for OCI tags
===========================================================

.. module:: regshape.libs.tags
   :platform: Unix, Windows
   :synopsis: Library-level functions for OCI tag operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.tags.operations import (
    delete_tag,
    list_tags,
)

__all__ = [
    "delete_tag",
    "list_tags",
]

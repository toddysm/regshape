#!/usr/bin/env python3

"""
:mod:`regshape.libs.tags` - Domain operations for OCI tags
===========================================================

.. module:: regshape.libs.tags
   :platform: Unix, Windows
   :synopsis: Library-level OCI tag operations and reference preflight.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.tags.operations import (
    delete_tag,
    list_tags,
    list_tags_all,
    validate_tag_reference,
)

__all__ = [
    "delete_tag",
    "list_tags",
    "list_tags_all",
    "validate_tag_reference",
]

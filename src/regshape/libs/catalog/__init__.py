#!/usr/bin/env python3

"""
:mod:`regshape.libs.catalog` - Domain operations for OCI repository catalog
============================================================================

.. module:: regshape.libs.catalog
   :platform: Unix, Windows
   :synopsis: Library-level functions for OCI repository-catalog operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.catalog.operations import (
    list_catalog,
    list_catalog_all,
)

__all__ = [
    "list_catalog",
    "list_catalog_all",
]

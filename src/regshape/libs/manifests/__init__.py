#!/usr/bin/env python3

"""
:mod:`regshape.libs.manifests` - Domain operations for OCI manifests
=====================================================================

.. module:: regshape.libs.manifests
   :platform: Unix, Windows
   :synopsis: Library-level functions for OCI manifest operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.manifests.operations import (
    delete_manifest,
    get_manifest,
    head_manifest,
    push_manifest,
)

__all__ = [
    "delete_manifest",
    "get_manifest",
    "head_manifest",
    "push_manifest",
]

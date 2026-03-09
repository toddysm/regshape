#!/usr/bin/env python3

"""
:mod:`regshape.libs.layout` - OCI Image Layout filesystem operations
=====================================================================

.. module:: regshape.libs.layout
   :platform: Unix, Windows
   :synopsis: Library-level functions for creating and managing OCI Image
              Layout directories on the local filesystem.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.layout.operations import (
    add_blob,
    add_manifest,
    generate_config,
    generate_manifest,
    init_layout,
    read_blob,
    read_index,
    read_stage,
    stage_layer,
    update_config,
    update_layer_annotations,
    update_manifest_annotations,
    validate_layout,
)

__all__ = [
    # High-level staged workflow
    "stage_layer",
    "generate_config",
    "generate_manifest",
    "read_stage",
    # Post-generation updates
    "update_layer_annotations",
    "update_config",
    "update_manifest_annotations",
    # Initialisation
    "init_layout",
    # Low-level primitives
    "add_blob",
    "add_manifest",
    # Readers / validators
    "read_blob",
    "read_index",
    "validate_layout",
]

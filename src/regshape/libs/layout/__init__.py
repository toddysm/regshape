#!/usr/bin/env python3

"""
:mod:`regshape.libs.layout` - OCI Image Layout filesystem operations
=====================================================================

.. module:: regshape.libs.layout
   :platform: Unix, Windows
   :synopsis: Library-level functions for creating and managing OCI Image
              Layout directories on the local filesystem, including layer
              compression preparation, media-type selection and config-update
              manifest staleness reporting and bounded read-only inspection.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.errors import LayoutInspectionError
from regshape.libs.layout.inspection import inspect_layout
from regshape.libs.layout.inspection_models import (
    DescriptorView,
    IndexNode,
    InspectionDiagnostic,
    InspectionLimits,
    LayoutInspectionResult,
    LayoutNode,
    ManifestNode,
    OpaqueNode,
    ReferrerListEvidence,
    SourceLocation,
    UnknownNode,
)
from regshape.libs.layout.operations import (
    add_blob,
    add_manifest,
    generate_config,
    generate_manifest,
    init_layout,
    prepare_layer_content,
    push_layout,
    read_blob,
    read_index,
    read_stage,
    stage_layer,
    update_config,
    update_config_with_result,
    update_layer_annotations,
    update_manifest_annotations,
    validate_layout,
    BlobPushReport,
    ConfigUpdateResult,
    ManifestPushReport,
    PushResult,
)

__all__ = [
    # High-level staged workflow
    "prepare_layer_content",
    "stage_layer",
    "generate_config",
    "generate_manifest",
    "read_stage",
    # Post-generation updates
    "update_layer_annotations",
    "update_config",
    "update_config_with_result",
    "ConfigUpdateResult",
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
    "inspect_layout",
    "InspectionLimits",
    "LayoutInspectionResult",
    "LayoutInspectionError",
    "LayoutNode",
    "IndexNode",
    "ManifestNode",
    "OpaqueNode",
    "UnknownNode",
    "DescriptorView",
    "SourceLocation",
    "InspectionDiagnostic",
    "ReferrerListEvidence",
    # Push
    "push_layout",
    "BlobPushReport",
    "ManifestPushReport",
    "PushResult",
]

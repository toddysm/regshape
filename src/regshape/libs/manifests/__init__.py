#!/usr/bin/env python3

"""
:mod:`regshape.libs.manifests` - Domain operations for OCI manifests
=====================================================================

.. module:: regshape.libs.manifests
   :platform: Unix, Windows
   :synopsis: OCI manifest operations and optional immutable receive observations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.manifests.download import ManifestDownload, download_manifest
from regshape.libs.manifests.operations import (
    delete_manifest,
    get_manifest,
    head_manifest,
    push_manifest,
)
from regshape.libs.progress import (
    ManifestKind,
    ManifestPhase,
    ManifestProgress,
    ManifestProgressCallback,
)

__all__ = [
    "ManifestDownload",
    "ManifestKind",
    "ManifestPhase",
    "ManifestProgress",
    "ManifestProgressCallback",
    "delete_manifest",
    "download_manifest",
    "get_manifest",
    "head_manifest",
    "push_manifest",
]

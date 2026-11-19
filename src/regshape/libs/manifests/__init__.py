#!/usr/bin/env python3

"""
:mod:`regshape.libs.manifests` - Domain operations for OCI manifests
=====================================================================

.. module:: regshape.libs.manifests
   :platform: Unix, Windows
   :synopsis: OCI manifest operations, digest resolution, defaults and observations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.manifests.download import ManifestDownload, download_manifest
from regshape.libs.manifests.operations import (
    DEFAULT_MANIFEST_ACCEPT,
    delete_manifest,
    get_manifest,
    head_manifest,
    push_manifest,
    resolve_digest,
    validate_manifest_digest,
)
from regshape.libs.progress import (
    ManifestKind,
    ManifestPhase,
    ManifestProgress,
    ManifestProgressCallback,
)

__all__ = [
    "DEFAULT_MANIFEST_ACCEPT",
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
    "resolve_digest",
    "validate_manifest_digest",
]

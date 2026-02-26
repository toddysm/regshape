#!/usr/bin/env python3

"""
:mod:`regshape.libs.models.mediatype` - OCI and Docker media type constants
===========================================================================

.. module:: regshape.libs.models.mediatype
   :platform: Unix, Windows
   :synopsis: String constants for OCI Distribution Spec and Docker V2 media types
.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

# ---------------------------------------------------------------------------
# OCI Image manifest types
# ---------------------------------------------------------------------------

OCI_IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
"""OCI Image Manifest v1."""

OCI_IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"
"""OCI Image Index (multi-arch manifest list) v1."""

OCI_IMAGE_CONFIG = "application/vnd.oci.image.config.v1+json"
"""OCI Image Configuration v1."""

# ---------------------------------------------------------------------------
# OCI layer media types
# ---------------------------------------------------------------------------

OCI_IMAGE_LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"
"""Uncompressed OCI image layer (tar)."""

OCI_IMAGE_LAYER_TAR_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
"""Gzip-compressed OCI image layer."""

OCI_IMAGE_LAYER_TAR_ZSTD = "application/vnd.oci.image.layer.v1.tar+zstd"
"""Zstd-compressed OCI image layer."""

# ---------------------------------------------------------------------------
# OCI special types
# ---------------------------------------------------------------------------

OCI_EMPTY = "application/vnd.oci.empty.v1+json"
"""OCI empty descriptor (used as a config placeholder in artifact manifests)."""

# ---------------------------------------------------------------------------
# Docker V2 media types
# ---------------------------------------------------------------------------

DOCKER_MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
"""Docker Image Manifest V2 Schema 2."""

DOCKER_MANIFEST_LIST_V2 = "application/vnd.docker.distribution.manifest.list.v2+json"
"""Docker Manifest List V2 (multi-arch)."""

# ---------------------------------------------------------------------------
# Convenience sets (used for dispatch and validation)
# ---------------------------------------------------------------------------

MANIFEST_MEDIA_TYPES: frozenset[str] = frozenset({
    OCI_IMAGE_MANIFEST,
    DOCKER_MANIFEST_V2,
})
"""All media types that map to :class:`~regshape.libs.models.manifest.ImageManifest`."""

INDEX_MEDIA_TYPES: frozenset[str] = frozenset({
    OCI_IMAGE_INDEX,
    DOCKER_MANIFEST_LIST_V2,
})
"""All media types that map to :class:`~regshape.libs.models.manifest.ImageIndex`."""

ALL_MANIFEST_MEDIA_TYPES: frozenset[str] = MANIFEST_MEDIA_TYPES | INDEX_MEDIA_TYPES
"""Union of all known manifest media types."""

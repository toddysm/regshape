#!/usr/bin/env python3

"""
:mod:`regshape.libs.blobs` - Domain operations for OCI blobs
=============================================================

.. module:: regshape.libs.blobs
   :platform: Unix, Windows
   :synopsis: Library-level functions for OCI blob operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.libs.blobs.download import (
    BlobBatchResult,
    BlobDownload,
    DownloadOptions,
    DownloadStats,
    DownloadStatus,
)
from regshape.libs.blobs.operations import (
    delete_blob,
    get_blob,
    head_blob,
    mount_blob,
    mount_blob_result,
    upload_blob,
    upload_blob_chunked,
    upload_blob_chunked_result,
    upload_blob_stream,
    upload_blob_stream_result,
)
from regshape.libs.models.blob import BlobMountResult, BlobUploadResult
from regshape.libs.progress import (
    ProgressCallback,
    TransferDirection,
    TransferPhase,
    TransferProgress,
)

# isort: split
# Batch imports artifact operations, which require the exports above.
from regshape.libs.blobs.batch import get_blobs

__all__ = [
    "BlobBatchResult",
    "BlobDownload",
    "BlobMountResult",
    "BlobUploadResult",
    "DownloadOptions",
    "DownloadStats",
    "DownloadStatus",
    "ProgressCallback",
    "TransferDirection",
    "TransferPhase",
    "TransferProgress",
    "delete_blob",
    "get_blob",
    "get_blobs",
    "head_blob",
    "mount_blob",
    "mount_blob_result",
    "upload_blob",
    "upload_blob_chunked",
    "upload_blob_chunked_result",
    "upload_blob_stream",
    "upload_blob_stream_result",
]

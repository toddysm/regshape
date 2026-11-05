#!/usr/bin/env python3

"""
:mod:`regshape.libs.layout.operations` - OCI Image Layout filesystem operations
================================================================================

.. module:: regshape.libs.layout.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for creating and managing OCI Image Layout
              directories on the local filesystem, with registry push
              orchestration for completed layouts.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Opt-in recursive validation delegates to the strict artifact graph layer;
legacy shallow validation and staged authoring retain their existing behavior.
Push always snapshots the complete directory/archive graph before registry work;
native Linux/Darwin storage requirements apply to that strict path.
"""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Union, cast, final
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from regshape.libs.artifacts.graph import (
    REF_NAME,
    Edge,
    GraphObject,
    parse_document,
    strict_json,
)
from regshape.libs.artifacts.models import ArtifactLimits, digest_parts
from regshape.libs.artifacts.operations import (
    ArchiveSnapshot,
    DirectorySnapshot,
    snapshot_layout,
    validate_tag,
)
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import BlobError, LayoutError, ManifestError
from regshape.libs.manifests.download import _validate_inputs
from regshape.libs.models.descriptor import Descriptor
from regshape.libs.models.manifest import ImageIndex, ImageManifest, parse_manifest
from regshape.libs.models.mediatype import OCI_IMAGE_INDEX
from regshape.libs.progress import (
    ProgressCallback,
    TransferDirection,
    TransferPhase,
    TransferProgress,
)
from regshape.libs.refs import parse_image_ref
from regshape.libs.transport import RegistryClient

# ===========================================================================
# Internal constants
# ===========================================================================

_OCI_LAYOUT_VERSION = "1.0.0"
_OCI_LAYOUT_FILE = "oci-layout"
_INDEX_FILE = "index.json"
_BLOBS_DIR = "blobs"
_STAGE_FILE = ".regshape-stage.json"


# ===========================================================================
# Private helpers
# ===========================================================================

def _lp(path: Union[str, Path]) -> Path:
    """Normalise *path* to a :class:`~pathlib.Path`."""
    return Path(path)


def _oci_layout_file(layout: Path) -> Path:
    return layout / _OCI_LAYOUT_FILE


def _index_file(layout: Path) -> Path:
    return layout / _INDEX_FILE


def _blob_path(layout: Path, digest: str) -> Path:
    alg, hex_digest = digest.split(":", 1)
    return layout / _BLOBS_DIR / alg / hex_digest


def _validate_is_layout(layout: Path) -> None:
    """Raise :class:`~regshape.libs.errors.LayoutError` if *layout* is not
    an initialised OCI Image Layout."""
    if not _oci_layout_file(layout).exists():
        raise LayoutError(
            f"{layout} is not an OCI Image Layout",
            "missing oci-layout file",
        )


def _write_atomically(target: Path, content: bytes) -> None:
    """Write *content* to *target* using an atomic temp-file rename.

    Creates the parent directory if necessary.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_index(layout: Path) -> ImageIndex:
    """Internal helper: read and parse *layout*/index.json."""
    index_file = _index_file(layout)
    try:
        raw = index_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise LayoutError(
            f"index.json not found at {layout}",
            str(exc),
        ) from exc
    try:
        parsed = parse_manifest(raw)
    except Exception as exc:
        raise LayoutError(
            f"index.json at {layout} is not a valid OCI Image Index",
            str(exc),
        ) from exc
    if not isinstance(parsed, ImageIndex):
        raise LayoutError(
            f"index.json at {layout} is not a valid OCI Image Index",
            f"got {type(parsed).__name__}",
        )
    return parsed


def _write_index(layout: Path, index: ImageIndex) -> None:
    """Serialise *index* to *layout*/index.json atomically (human-readable)."""
    content = json.dumps(json.loads(index.to_json()), indent=2).encode("utf-8")
    _write_atomically(_index_file(layout), content)


def _stage_file(layout: Path) -> Path:
    return layout / _STAGE_FILE


def _read_stage_raw(layout: Path) -> dict:
    """Read and parse the staging file; raise :class:`~regshape.libs.errors.LayoutError`
    if it is missing or malformed."""
    sf = _stage_file(layout)
    if not sf.exists():
        raise LayoutError(
            f"Staging file not found at {layout}",
            "run 'layout init' to initialise the layout",
        )
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LayoutError(
            f"Staging file at {layout} is malformed",
            str(exc),
        ) from exc


def _write_stage(layout: Path, stage: dict) -> None:
    """Write *stage* to the staging file atomically."""
    content = json.dumps(stage, indent=2).encode("utf-8")
    _write_atomically(_stage_file(layout), content)


def _hash_blob_stream(source: BinaryIO) -> str:
    """Consume a borrowed source in 64 KiB chunks and return its SHA-256 digest."""
    digest = hashlib.sha256()
    while chunk := source.read(65536):
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _check_blob_integrity(layout: Path, digest: str, context: str) -> None:
    """Require a readable referenced blob and verify SHA-256 without buffering it.

    Other algorithms retain the existing readability-only validation behavior.
    """
    blob = _blob_path(layout, digest)
    if not blob.exists():
        raise LayoutError(
            f"blob {digest} referenced by {context} does not exist",
            f"expected at {blob}",
        )
    with blob.open("rb") as source:
        actual = _hash_blob_stream(source)
    alg, _ = digest.split(":", 1)
    if alg == "sha256" and actual != digest:
        raise LayoutError(
            f"digest mismatch for blob {digest}",
            f"computed {actual}",
        )


# ===========================================================================
# Public operations
# ===========================================================================


def init_layout(path: Union[str, Path]) -> None:
    """Initialise a new, empty OCI Image Layout at *path*.

    Creates *path* (and any missing parents) if it does not exist, writes the
    ``oci-layout`` marker, creates ``blobs/sha256/``, and writes an empty
    ``index.json``.

    :param path: Filesystem path for the layout root.
    :raises LayoutError: If *path* already contains an ``oci-layout`` marker.
    :raises OSError: On filesystem permission or I/O errors.
    """
    layout = _lp(path)
    marker = _oci_layout_file(layout)

    if marker.exists():
        raise LayoutError(
            f"{layout} is already an OCI Image Layout",
            "oci-layout file already exists",
        )

    # Create blob directory tree
    (layout / _BLOBS_DIR / "sha256").mkdir(parents=True, exist_ok=True)

    # Write oci-layout marker
    marker_bytes = json.dumps(
        {"imageLayoutVersion": _OCI_LAYOUT_VERSION}
    ).encode("utf-8")
    _write_atomically(marker, marker_bytes)

    # Write empty index.json
    empty_index = ImageIndex(
        schema_version=2,
        media_type=OCI_IMAGE_INDEX,
        manifests=[],
    )
    _write_index(layout, empty_index)

    # Write initial staging file
    _write_stage(layout, {
        "schema_version": 1,
        "layers": [],
        "config": None,
        "manifest": None,
    })


def add_blob(layout_path: Union[str, Path], content: bytes) -> tuple[str, int]:
    """Write *content* to the blob store and return ``(digest, size)``.

    The blob is stored content-addressed under ``blobs/sha256/<hex>``.  If a
    blob with the same digest already exists and its on-disk size matches, the
    write is skipped (idempotent).

    :param layout_path: Root of an initialised OCI Image Layout.
    :param content: Raw bytes to store (a layer tarball, config JSON, etc.).
    :returns: ``(digest, size)`` where *digest* is ``"sha256:<hex>"`` and
        *size* is the byte length of *content*.
    :raises LayoutError: If *layout_path* is not an initialised layout.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)

    hex_digest = hashlib.sha256(content).hexdigest()
    digest = f"sha256:{hex_digest}"
    size = len(content)

    blob = _blob_path(layout, digest)
    if blob.exists() and blob.stat().st_size == size:
        return digest, size

    _write_atomically(blob, content)
    return digest, size


def add_manifest(
    layout_path: Union[str, Path],
    manifest_bytes: bytes,
    media_type: str,
    ref_name: Union[str, None] = None,
    annotations: Union[dict[str, str], None] = None,
) -> Descriptor:
    """Write a manifest blob and register it in ``index.json``.

    The manifest is stored as a blob, then a
    :class:`~regshape.libs.models.descriptor.Descriptor` for it is appended
    to the ``manifests`` array of ``index.json``.

    :param layout_path: Root of an initialised OCI Image Layout.
    :param manifest_bytes: Serialised manifest JSON bytes.
    :param media_type: Manifest media type (e.g.
        ``application/vnd.oci.image.manifest.v1+json``).
    :param ref_name: Optional human-readable reference name added as the
        ``org.opencontainers.image.ref.name`` annotation on the index
        descriptor.
    :param annotations: Additional annotations merged onto the index descriptor
        (merged after *ref_name*, so they can override it if needed).
    :returns: The :class:`~regshape.libs.models.descriptor.Descriptor` appended
        to ``index.json``.
    :raises LayoutError: If *layout_path* is not an initialised layout, or if
        ``index.json`` is malformed.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)

    digest, size = add_blob(layout, manifest_bytes)

    # Build merged annotations for the index entry
    merged: dict[str, str] = {}
    if ref_name is not None:
        merged["org.opencontainers.image.ref.name"] = ref_name
    if annotations:
        merged.update(annotations)
    desc_annotations = merged if merged else None

    descriptor = Descriptor(
        media_type=media_type,
        digest=digest,
        size=size,
        annotations=desc_annotations,
    )

    # Read → modify → write index.json
    index = _read_index(layout)
    index.manifests.append(descriptor)
    _write_index(layout, index)

    return descriptor


def read_index(layout_path: Union[str, Path]) -> ImageIndex:
    """Parse and return the ``index.json`` of an OCI Image Layout.

    :param layout_path: Root of an initialised OCI Image Layout.
    :returns: The :class:`~regshape.libs.models.manifest.ImageIndex` parsed
        from ``index.json``.
    :raises LayoutError: If *layout_path* is not an initialised layout, or
        ``index.json`` is missing or malformed.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)
    return _read_index(layout)


def read_blob(layout_path: Union[str, Path], digest: str) -> bytes:
    """Read and return the raw bytes of a blob by digest.

    The on-disk content is verified against *digest* before returning.

    :param layout_path: Root of an initialised OCI Image Layout.
    :param digest: Content digest in ``"<alg>:<hex>"`` form (e.g.
        ``"sha256:abc..."``).
    :returns: Raw blob bytes.
    :raises LayoutError: If the blob is not found, the digest format is
        invalid, the algorithm is unsupported, or the digest does not match.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)

    try:
        alg, _ = digest.split(":", 1)
    except ValueError as exc:
        raise LayoutError(
            f"Invalid digest format: {digest!r}",
            str(exc),
        ) from exc

    if alg != "sha256":
        raise LayoutError(
            f"Unsupported digest algorithm: {alg!r}",
            "only sha256 is supported",
        )

    blob = _blob_path(layout, digest)
    try:
        content = blob.read_bytes()
    except OSError as exc:
        raise LayoutError(
            f"Blob {digest} not found in {layout}",
            str(exc),
        ) from exc

    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual != digest:
        raise LayoutError(
            f"Digest mismatch for {digest}",
            f"computed {actual}",
        )

    return content


def validate_layout(
    layout_path: Union[str, Path], *, recursive: bool = False,
    limits: ArtifactLimits | None = None,
) -> None:
    """Validate the structural and content integrity of an OCI Image Layout.

    Checks that the ``oci-layout`` marker is present and well-formed,
    ``index.json`` is a valid ``ImageIndex``, every manifest blob referenced
    by ``index.json`` exists and its digest matches, and for each manifest
    blob that is a parseable OCI manifest, all config/layer blobs it
    references also exist and their digests match.

    :param layout_path: Layout directory, or gzip-tar file in recursive mode.
    :param recursive: Verify the complete subject/index/config/layer closure with
        strict JSON, size/digest checks and a private directory/archive snapshot.
    :param limits: Strict-mode policy; supplying limits without recursive=True
        raises ValueError rather than silently ignoring them.
    :raises ArtifactError: For strict graph, metadata or resource-budget failures.
    :raises LayoutError: Describing the first structural or integrity violation
        found.
    """
    if recursive:
        policy = limits if limits is not None else ArtifactLimits()
        if not isinstance(policy, ArtifactLimits):
            raise ValueError("limits must be ArtifactLimits")
        with snapshot_layout(layout_path, limits=policy):
            pass
        return
    if limits is not None:
        raise ValueError("limits require recursive=True")
    layout = _lp(layout_path)
    if not layout.is_dir():
        raise LayoutError("Layout validation requires a directory")

    # 1. Verify oci-layout marker
    marker = _oci_layout_file(layout)
    if not marker.exists():
        raise LayoutError(
            f"{layout} is not an OCI Image Layout",
            "missing oci-layout file",
        )
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        version = marker_data.get("imageLayoutVersion")
        if version != _OCI_LAYOUT_VERSION:
            raise LayoutError(
                f"Invalid imageLayoutVersion in oci-layout: {version!r}",
                f"expected {_OCI_LAYOUT_VERSION!r}",
            )
    except (json.JSONDecodeError, OSError) as exc:
        raise LayoutError("oci-layout marker is malformed", str(exc)) from exc

    # 2. Read and validate index.json
    index = _read_index(layout)

    # 3. Check each manifest entry and the blobs it references
    for entry in index.manifests:
        _check_blob_integrity(layout, entry.digest, context="index.json")

        # 4. Deep-check referenced blobs inside parseable manifests
        blob_bytes = _blob_path(layout, entry.digest).read_bytes()
        try:
            manifest = parse_manifest(blob_bytes.decode("utf-8"))
        except Exception:
            # Binary or non-JSON artifact — skip deep inspection
            continue

        if isinstance(manifest, ImageManifest):
            _check_blob_integrity(
                layout, manifest.config.digest,
                context=f"config in manifest {entry.digest}",
            )
            for i, layer in enumerate(manifest.layers):
                _check_blob_integrity(
                    layout, layer.digest,
                    context=f"layer[{i}] in manifest {entry.digest}",
                )
        elif isinstance(manifest, ImageIndex):
            for sub in manifest.manifests:
                _check_blob_integrity(
                    layout, sub.digest,
                    context=f"nested manifest entry in index {entry.digest}",
                )


# ===========================================================================
# Staged workflow operations
# ===========================================================================


def stage_layer(
    layout_path: Union[str, Path],
    content: bytes,
    media_type: str,
    annotations: Union[dict[str, str], None] = None,
) -> Descriptor:
    """Write *content* as a layer blob and append its descriptor to the staging file.

    Compression is the caller's responsibility — *content* must already be in
    the form declared by *media_type*.

    :param layout_path: Root of an initialised OCI Image Layout.
    :param content: Raw layer bytes (compressed or uncompressed, as needed).
    :param media_type: Layer media type.
    :param annotations: Optional annotations stored on the layer descriptor.
    :returns: :class:`~regshape.libs.models.descriptor.Descriptor` appended to
        the staging file.
    :raises LayoutError: If *layout_path* is not initialised or the staging
        file is missing/malformed.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)
    digest, size = add_blob(layout, content)
    layer_entry: dict = {
        "digest": digest,
        "size": size,
        "media_type": media_type,
        "annotations": annotations or {},
    }
    stage = _read_stage_raw(layout)
    stage["layers"].append(layer_entry)
    _write_stage(layout, stage)
    return Descriptor(
        media_type=media_type,
        digest=digest,
        size=size,
        annotations=annotations if annotations else None,
    )


def generate_config(
    layout_path: Union[str, Path],
    architecture: str = "amd64",
    os_name: str = "linux",
    media_type: str = "application/vnd.oci.image.config.v1+json",
    annotations: Union[dict[str, str], None] = None,
) -> Descriptor:
    """Generate an OCI Image Config JSON from the staged layers.

    Reads staged layer digests to build ``rootfs.diff_ids``, writes the config
    blob, and records the config descriptor in the staging file.

    :param layout_path: Root of an initialised OCI Image Layout.
    :param architecture: Target CPU architecture (default ``"amd64"``).
    :param os_name: Target OS (default ``"linux"``).
    :param media_type: Config media type.
    :param annotations: Optional labels embedded in ``config.Labels``.
    :returns: :class:`~regshape.libs.models.descriptor.Descriptor` of the
        stored config blob.
    :raises LayoutError: If no layers have been staged, or the layout is not
        initialised.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)
    stage = _read_stage_raw(layout)
    if not stage["layers"]:
        raise LayoutError(
            "No layers staged",
            "run stage_layer first",
        )
    diff_ids = [layer["digest"] for layer in stage["layers"]]
    config_obj: dict = {
        "architecture": architecture,
        "os": os_name,
        "rootfs": {
            "type": "layers",
            "diff_ids": diff_ids,
        },
    }
    if annotations:
        config_obj["config"] = {"Labels": annotations}
    config_bytes = json.dumps(
        config_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest, size = add_blob(layout, config_bytes)
    stage["config"] = {"digest": digest, "size": size, "media_type": media_type}
    _write_stage(layout, stage)
    return Descriptor(media_type=media_type, digest=digest, size=size, annotations=annotations or None)


def generate_manifest(
    layout_path: Union[str, Path],
    ref_name: Union[str, None] = None,
    media_type: str = "application/vnd.oci.image.manifest.v1+json",
    annotations: Union[dict[str, str], None] = None,
) -> Descriptor:
    """Generate an OCI Image Manifest and register it in ``index.json``.

    Reads the staged config descriptor and layer descriptors, builds the
    manifest JSON (including any layer-level annotations), writes the manifest
    blob, and appends its descriptor to ``index.json``.

    :param layout_path: Root of an initialised OCI Image Layout.
    :param ref_name: Optional reference name added as
        ``org.opencontainers.image.ref.name`` in ``index.json``.
    :param media_type: Manifest media type.
    :param annotations: Optional annotations embedded in the manifest JSON.
    :returns: :class:`~regshape.libs.models.descriptor.Descriptor` as
        registered in ``index.json``.
    :raises LayoutError: If the config or layers have not been staged.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    _validate_is_layout(layout)
    stage = _read_stage_raw(layout)
    if not stage["layers"]:
        raise LayoutError(
            "No layers staged",
            "run stage_layer first",
        )
    if stage["config"] is None:
        raise LayoutError(
            "Config not yet generated",
            "run generate_config first",
        )
    cfg = stage["config"]
    layers_json = []
    for layer in stage["layers"]:
        layer_dict: dict = {
            "mediaType": layer["media_type"],
            "digest": layer["digest"],
            "size": layer["size"],
        }
        if layer.get("annotations"):
            layer_dict["annotations"] = layer["annotations"]
        layers_json.append(layer_dict)
    manifest_obj: dict = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "config": {
            "mediaType": cfg["media_type"],
            "digest": cfg["digest"],
            "size": cfg["size"],
        },
        "layers": layers_json,
    }
    if annotations:
        manifest_obj["annotations"] = annotations
    manifest_bytes = json.dumps(
        manifest_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    descriptor = add_manifest(layout, manifest_bytes, media_type, ref_name=ref_name)
    stage["manifest"] = {
        "digest": descriptor.digest,
        "size": descriptor.size,
        "media_type": descriptor.media_type,
        "annotations": annotations or {},
    }
    _write_stage(layout, stage)
    return descriptor


def read_stage(layout_path: Union[str, Path]) -> dict:
    """Return the current staging state from ``.regshape-stage.json``.

    :param layout_path: Root of an initialised OCI layout.
    :returns: ``dict`` with keys ``schema_version``, ``layers``, ``config``,
        ``manifest``.
    :raises LayoutError: If the staging file is missing or malformed.
    """
    layout = _lp(layout_path)
    return _read_stage_raw(layout)


def update_layer_annotations(
    layout_path: Union[str, Path],
    layer_index: int,
    annotations: dict[str, str],
    replace: bool = False,
) -> Descriptor:
    """Merge or replace annotations on a staged layer descriptor.

    The blob file is not changed — only the descriptor metadata in the staging
    file is updated.

    :param layout_path: Root of an initialised OCI layout.
    :param layer_index: 0-based index into the staged ``layers`` array.
    :param annotations: Annotations to merge in (or replace if *replace* is
        ``True``).
    :param replace: If ``True``, overwrite all existing annotations.
    :returns: Updated :class:`~regshape.libs.models.descriptor.Descriptor`
        for that layer.
    :raises LayoutError: If the staging file is missing or *layer_index* is
        out of range.
    """
    layout = _lp(layout_path)
    stage = _read_stage_raw(layout)
    layers = stage["layers"]
    if layer_index < 0 or layer_index >= len(layers):
        raise LayoutError(
            f"Layer index {layer_index} is out of range",
            f"staged layer count: {len(layers)}",
        )
    layer = layers[layer_index]
    if replace:
        layer["annotations"] = dict(annotations)
    else:
        existing: dict[str, str] = layer.get("annotations") or {}
        existing.update(annotations)
        layer["annotations"] = existing
    _write_stage(layout, stage)
    ann = layer["annotations"] or None
    return Descriptor(
        media_type=layer["media_type"],
        digest=layer["digest"],
        size=layer["size"],
        annotations=ann if ann else None,
    )


def update_config(
    layout_path: Union[str, Path],
    architecture: Union[str, None] = None,
    os_name: Union[str, None] = None,
    annotations: Union[dict[str, str], None] = None,
    replace_annotations: bool = False,
) -> Descriptor:
    """Re-generate the OCI Image Config with updated fields.

    Reads the existing config blob, applies the requested changes, writes the
    new blob, deletes the old blob to prevent orphaned files, and updates the
    staging file.

    :param layout_path: Root of an initialised OCI layout.
    :param architecture: New CPU architecture; ``None`` keeps the existing value.
    :param os_name: New OS; ``None`` keeps the existing value.
    :param annotations: Labels to merge into ``config.Labels``; ``None`` keeps
        all existing labels unchanged.
    :param replace_annotations: If ``True``, replace all existing
        ``config.Labels`` with *annotations*.
    :returns: New :class:`~regshape.libs.models.descriptor.Descriptor` for the
        config blob.
    :raises LayoutError: If the config has not yet been generated.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    stage = _read_stage_raw(layout)
    if stage["config"] is None:
        raise LayoutError(
            "Config not yet generated",
            "run generate_config first",
        )
    old_digest = stage["config"]["digest"]
    config_bytes = read_blob(layout, old_digest)
    config_obj = json.loads(config_bytes)
    if architecture is not None:
        config_obj["architecture"] = architecture
    if os_name is not None:
        config_obj["os"] = os_name
    if annotations is not None:
        existing_labels: dict[str, str] = config_obj.get("config", {}).get("Labels") or {}
        new_labels = dict(annotations) if replace_annotations else {**existing_labels, **annotations}
        if new_labels:
            config_obj.setdefault("config", {})["Labels"] = new_labels
        elif "config" in config_obj and "Labels" in config_obj["config"]:
            del config_obj["config"]["Labels"]
    new_bytes = json.dumps(
        config_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if new_bytes == config_bytes:
        cfg = stage["config"]
        return Descriptor(
            media_type=cfg["media_type"],
            digest=cfg["digest"],
            size=cfg["size"],
        )
    new_digest, new_size = add_blob(layout, new_bytes)
    _blob_path(layout, old_digest).unlink(missing_ok=True)
    cfg_media_type = stage["config"]["media_type"]
    stage["config"] = {"digest": new_digest, "size": new_size, "media_type": cfg_media_type}
    _write_stage(layout, stage)
    return Descriptor(media_type=cfg_media_type, digest=new_digest, size=new_size)


def update_manifest_annotations(
    layout_path: Union[str, Path],
    annotations: dict[str, str],
    replace: bool = False,
) -> Descriptor:
    """Re-generate the manifest with updated ``manifest.annotations``.

    Reads the existing manifest blob, merges or replaces annotations, writes
    the new manifest blob, deletes the old blob to prevent orphaned files,
    updates ``index.json``, and updates the staging file.

    :param layout_path: Root of an initialised OCI layout.
    :param annotations: Annotations to merge in (or replace if *replace* is
        ``True``).
    :param replace: If ``True``, replace all existing ``manifest.annotations``.
    :returns: New :class:`~regshape.libs.models.descriptor.Descriptor` as
        registered in ``index.json``.
    :raises LayoutError: If the manifest has not yet been generated.
    :raises OSError: On I/O errors.
    """
    layout = _lp(layout_path)
    stage = _read_stage_raw(layout)
    if stage["manifest"] is None:
        raise LayoutError(
            "Manifest not yet generated",
            "run generate_manifest first",
        )
    old_digest = stage["manifest"]["digest"]
    manifest_bytes = read_blob(layout, old_digest)
    manifest_obj = json.loads(manifest_bytes)
    existing_ann: dict[str, str] = manifest_obj.get("annotations") or {}
    if replace:
        manifest_obj["annotations"] = dict(annotations)
    else:
        existing_ann.update(annotations)
        manifest_obj["annotations"] = existing_ann
    new_bytes = json.dumps(
        manifest_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    new_digest, new_size = add_blob(layout, new_bytes)
    _blob_path(layout, old_digest).unlink(missing_ok=True)
    # Update index.json: replace old descriptor, preserve ref_name annotation
    index = _read_index(layout)
    new_manifests: list[Descriptor] = []
    updated_desc: Union[Descriptor, None] = None
    for desc in index.manifests:
        if desc.digest == old_digest:
            updated = Descriptor(
                media_type=desc.media_type,
                digest=new_digest,
                size=new_size,
                annotations=desc.annotations,  # keep index-level annotations (ref.name)
            )
            new_manifests.append(updated)
            updated_desc = updated
        else:
            new_manifests.append(desc)
    index.manifests = new_manifests
    _write_index(layout, index)
    stage["manifest"]["digest"] = new_digest
    stage["manifest"]["size"] = new_size
    stage["manifest"]["annotations"] = manifest_obj.get("annotations") or {}
    _write_stage(layout, stage)
    return updated_desc or Descriptor(
        media_type=stage["manifest"]["media_type"],
        digest=new_digest,
        size=new_size,
    )


# ===========================================================================
# Push operations
# ===========================================================================


@dataclass
class BlobPushReport:
    """Report for a single blob push action."""

    digest: str
    size: int
    media_type: str
    action: str  # "uploaded", "skipped"


@dataclass
class ManifestPushReport:
    """One document publication; indexes have no blobs and aliases are separate reports."""

    digest: str
    reference: str
    media_type: str
    blobs: list[BlobPushReport] = field(default_factory=list)
    status: str = "pushed"


@dataclass
class PushResult:
    """Acknowledged publications and compatible per-reference blob accounting.

    manifests_pushed counts document PUTs, including required root aliases.
    Blob uploads/bytes are unique; skips also include reused per-manifest references.
    No partial result is returned on failure and remote content is never rolled back.
    """

    layout_path: str
    destination: str
    manifests: list[ManifestPushReport] = field(default_factory=list)
    manifests_pushed: int = 0
    blobs_uploaded: int = 0
    blobs_skipped: int = 0
    bytes_uploaded: int = 0


def _collect_blob_descriptors(manifest: ImageManifest) -> list[Descriptor]:
    """Return all blob descriptors (layers + config) from a manifest."""
    blobs: list[Descriptor] = []
    blobs.extend(manifest.layers)
    blobs.append(manifest.config)
    return blobs


@dataclass(frozen=True)
class PushPublication:
    """Exact document/reference operation with legacy layer-then-config report edges."""

    document: GraphObject
    reference: str
    blobs: tuple[Edge, ...]
    root_reference: bool = False


def parse_push_destination(destination: str) -> tuple[str, str, str | None]:
    """Resolve registry/repository/optional tag without credentials or URL normalization.

    Digest destinations are rejected. An omitted tag returns None, not latest;
    callers that historically default to latest (Docker push) can do so explicitly.
    Invalid input raises ValueError without echoing untrusted reference text.
    """
    if not isinstance(destination, str) or any(
        char in destination for char in "@?#%\\"
    ) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in destination):
        raise ValueError("Push destination must be registry/repository[:tag], not a digest or URL")
    try:
        registry, repo, reference = parse_image_ref(destination)
        authority = urlsplit("//" + registry)
        if (
            not authority.hostname or authority.username is not None
            or authority.password is not None or authority.path
        ):
            raise ValueError("Invalid registry authority")
        _ = authority.port
        _validate_inputs(repo, reference, "application/json", None, None, 1)
    except ValueError as error:
        raise ValueError("Invalid push destination registry, repository or ASCII tag") from error
    tag = reference if ":" in destination.rsplit("/", 1)[-1] else None
    return registry, repo, tag


@final
class LayoutPushPlan:
    """Own a verified private snapshot and its deterministic publication plan.

    Enter before constructing a credentialed client. The context must outlive
    execution or dry-run rendering; no consumer may reopen the original source.
    Publications retain bytes in the snapshot, not serialized model copies.
    Only Linux/Darwin native storage is supported, under the artifact layer's
    trusted-private namespace contract (not hostile same-user/root isolation).
    """

    def __init__(
        self, layout_path: str | Path, tag_override: str | None = None,
        *, limits: ArtifactLimits | None = None,
    ) -> None:
        """Retain source and validate policy/tag without credentials or network effects."""
        self.layout_path = str(layout_path)
        self.limits = limits if limits is not None else ArtifactLimits()
        if not isinstance(self.limits, ArtifactLimits):
            raise ValueError("limits must be ArtifactLimits")  # noqa: TRY004 - shared public policy contract
        self.tag_override = validate_tag(tag_override) if tag_override is not None else None
        self.publications: tuple[PushPublication, ...] = ()
        self.blobs: tuple[GraphObject, ...] = ()
        self.snapshot: DirectorySnapshot | ArchiveSnapshot | None = None

    def __enter__(self) -> "LayoutPushPlan":
        """Snapshot and resolve every reference before exposing an executable plan."""
        if self.snapshot is not None:
            raise RuntimeError("Push plan is already active")
        snapshot = snapshot_layout(self.layout_path, limits=self.limits)
        snapshot.__enter__()
        self.snapshot = snapshot
        try:
            self._prepare()
        except BaseException as primary:
            self.__exit__(type(primary), primary, primary.__traceback__)
            raise
        return self

    def _prepare(self) -> None:
        """Parse bounded original wrapper metadata and schedule dependency-first PUTs."""
        snapshot = self.snapshot
        if snapshot is None:
            raise RuntimeError("Push plan is not active")
        wrapper = strict_json(snapshot.wrapper_bytes, self.limits, self.limits.max_manifest_bytes)
        roots = snapshot.graph.roots
        if not roots:
            raise LayoutError("index.json contains no manifests")
        if self.tag_override is not None and len(roots) != 1:
            raise LayoutError(
                f"tag override supplied but index.json has {len(roots)} manifests",
                "omit the tag or push a single-root layout",
            )
        references = []
        for root, original in zip(roots, wrapper["manifests"]):
            annotations = original.get("annotations", {})
            source_tag = validate_tag(annotations[REF_NAME]) if REF_NAME in annotations else None
            references.append(self.tag_override or source_tag or root.digest)

        documents: dict[str, tuple[GraphObject, tuple[Edge, ...]]] = {}
        required: set[str] = set()
        blob_objects = {obj.digest: obj for obj in snapshot.graph.objects if obj.kind == "blob"}
        for obj in snapshot.graph.objects:
            if obj.kind == "blob":
                continue
            raw = cast(bytes, snapshot.store.verify(obj.digest, obj.size, cap=self.limits.max_manifest_bytes))
            parsed = parse_document(raw, self.limits, media_type=obj.media_type)
            required.update(edge.digest for edge in parsed.children if edge.document)
            # Preserve the old flat report's layer/config ordering, including repeated edges.
            blobs = tuple(edge for edge in parsed.children if edge.role == "layer")
            blobs += tuple(edge for edge in parsed.children if edge.role == "config")
            documents[obj.digest] = (obj, blobs)

        publications = []
        seen = set()
        root_digests = {root.digest for root in roots}
        for digest, (obj, blobs) in documents.items():
            if digest not in root_digests or digest in required:
                publications.append(PushPublication(obj, digest, blobs))
                seen.add((digest, digest))
        for root, reference in zip(roots, references):
            key = (root.digest, reference)
            if key not in seen:
                obj, blobs = documents[root.digest]
                publications.append(PushPublication(obj, reference, blobs, True))
                seen.add(key)
        self.publications = tuple(publications)
        # Preserve ordinary flat layout's first-occurrence layer/config callback ordering.
        blob_order = dict.fromkeys(blob.digest for item in publications for blob in item.blobs)
        self.blobs = tuple(blob_objects[digest] for digest in blob_order)

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Release only private snapshot storage, preserving the consumer's primary error."""
        snapshot, self.snapshot = self.snapshot, None
        if snapshot is not None:
            snapshot.__exit__(exc_type, exc, traceback)
        return False


def execute_layout_push(
    plan: LayoutPushPlan, client: RegistryClient, repo: str, *,
    force: bool = False, chunked: bool = False, chunk_size: int = 65536,
    progress_callback=None, transfer_callback: ProgressCallback | None = None,
) -> PushResult:
    """Execute an active local plan, borrowing the client and preserving raw bytes.

    Upload unique blobs first; only a BlobError with status_code 404 means
    missing. Document dependencies are acknowledged by digest before parents,
    then final root references are published. Legacy callbacks retain their
    names/arguments; typed progress has one logical row per unique blob.
    Observer errors propagate unchanged. Remote partial state is never removed;
    a transport failure during a final root PUT raises chained ManifestError
    explicitly reporting outcome uncertainty.
    """
    from regshape.libs.blobs import head_blob, upload_blob_chunked, upload_blob_stream
    from regshape.libs.manifests import push_manifest

    snapshot = plan.snapshot
    if snapshot is None:
        raise RuntimeError("Push plan must be used inside its context")
    if transfer_callback is not None and not callable(transfer_callback):
        raise TypeError("Transfer callback must be callable")
    if progress_callback is not None and not callable(progress_callback):
        raise TypeError("Progress callback must be callable")
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    for publication in plan.publications:
        _validate_inputs(
            repo, publication.reference, publication.document.media_type,
            publication.document.digest, None, plan.limits.max_manifest_bytes,
        )
    result = PushResult(plan.layout_path, f"{client.config.registry}/{repo}")
    actions = {}
    for blob in plan.blobs:
        exists = False
        if not force:
            try:
                head_blob(client, repo, blob.digest)
                exists = True
            except BlobError as error:
                if error.status_code != 404:
                    raise
        if exists:
            actions[blob.digest] = "skipped"
            if progress_callback is not None:
                progress_callback("blob_skip", digest=blob.digest, size=blob.size)
            if transfer_callback is not None:
                transfer_callback(TransferProgress(
                    uuid4().hex, blob.digest, TransferDirection.UPLOAD,
                    TransferPhase.SKIPPED, 0, blob.size,
                ))
            continue
        if progress_callback is not None:
            progress_callback("blob_start", digest=blob.digest, size=blob.size,
                              media_type=blob.media_type)
        algorithm, encoded = digest_parts(blob.digest)
        with (
            snapshot.store.directory.open_child("blobs") as blobs,
            blobs.open_child(algorithm) as directory,
            directory.read_file(encoded) as source,
        ):
            if chunked:
                upload_blob_chunked(
                    client, repo, source, blob.digest, chunk_size=chunk_size,
                    total_size=blob.size, progress_callback=transfer_callback,
                )
            else:
                upload_blob_stream(
                    client, repo, source, blob.digest,
                    total_size=blob.size, progress_callback=transfer_callback,
                )
        actions[blob.digest] = "uploaded"
        result.blobs_uploaded += 1
        result.bytes_uploaded += blob.size
        if progress_callback is not None:
            progress_callback("blob_done", digest=blob.digest, size=blob.size)

    reported = set()
    for publication in plan.publications:
        obj = publication.document
        raw = cast(bytes, snapshot.store.verify(obj.digest, obj.size, cap=plan.limits.max_manifest_bytes))
        try:
            push_manifest(
                client, repo, publication.reference, raw, obj.media_type,
                expected_digest=obj.digest,
            )
        except requests.exceptions.RequestException as error:
            if publication.root_reference:
                raise ManifestError(
                    "Root publication outcome uncertain; verify the destination before retrying",
                    "the registry may have committed the exact payload; no rollback was attempted",
                ) from error
            raise
        report = ManifestPushReport(obj.digest, publication.reference, obj.media_type)
        for edge in publication.blobs:
            repeated = edge.digest in reported
            action = "skipped" if repeated else actions[edge.digest]
            report.blobs.append(BlobPushReport(edge.digest, edge.size, edge.media_type, action))
            if action == "skipped":
                result.blobs_skipped += 1
            if repeated and progress_callback is not None:
                progress_callback("blob_skip", digest=edge.digest, size=edge.size)
            reported.add(edge.digest)
        result.manifests.append(report)
        result.manifests_pushed += 1
        if progress_callback is not None:
            progress_callback("manifest_done", digest=obj.digest, reference=publication.reference)
    return result


@track_scenario("layout push")
def push_layout(
    layout_path: Union[str, Path],
    client: RegistryClient,
    repo: str,
    tag_override: Union[str, None] = None,
    force: bool = False,
    chunked: bool = False,
    chunk_size: int = 65536,
    progress_callback=None,
    *,
    transfer_callback: ProgressCallback | None = None,
    limits: ArtifactLimits | None = None,
) -> PushResult:
    """Snapshot and push a complete OCI directory or supported gzip-tar graph.

    Strict verification and publication-tag validation precede all client calls.
    Upload each unique blob, then publish original documents dependency-first,
    with root aliases last. Never upload the generated local wrapper.

    :param layout_path: Directory or supported archive, copied into private storage.
    :param client: An authenticated
        :class:`~regshape.libs.transport.RegistryClient`.
    :param repo: Target repository name (e.g. ``"myrepo/myimage"``).
    :param tag_override: If provided, overrides ``ref.name`` annotation
        for a single top-level root (possibly an index with many descendants).
        Present source tags are validated even with an override.
    :param force: If ``True``, skip ``HEAD`` existence checks and upload
        all blobs unconditionally.
    :param chunked: If ``True``, use the chunked upload protocol.
    :param chunk_size: Chunk size in bytes (used when *chunked* is ``True``).
    :param progress_callback: Optional callable invoked as
        ``progress_callback(event, **kwargs)`` for UI feedback.  Events:
        ``"blob_start"``, ``"blob_skip"``, ``"blob_done"``,
        ``"manifest_done"``.
    :param transfer_callback: Optional typed per-blob observer, independent of
        the legacy callback. Both upload protocols report actual logical bytes;
        existing blobs emit one skipped event per unique digest. Descriptor
        sizes are trusted totals. Observer exceptions propagate unchanged.
    :param limits: Independent artifact graph/archive safety budgets; defaults
        to ArtifactLimits. Linux/Darwin native storage only; Windows fails closed.
    :returns: A :class:`PushResult` with per-manifest reports and summary
        statistics.
    :raises LayoutError: If the layout is invalid or incomplete.
    :raises regshape.libs.errors.ArtifactError: On strict graph/storage/tag failures.
    :raises regshape.libs.errors.AuthError: On authentication failure.
    :raises regshape.libs.errors.BlobError: On blob upload failure.
    :raises regshape.libs.errors.ManifestError: On manifest push failure.
    :raises TypeError: If the typed observer is not callable.
    """
    if transfer_callback is not None and not callable(transfer_callback):
        raise TypeError("Transfer callback must be callable")
    with LayoutPushPlan(layout_path, tag_override, limits=limits) as plan:
        return execute_layout_push(
            plan, client, repo, force=force, chunked=chunked, chunk_size=chunk_size,
            progress_callback=progress_callback, transfer_callback=transfer_callback,
        )

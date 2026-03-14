#!/usr/bin/env python3

"""
:mod:`regshape.libs.docker.operations` - Docker Desktop integration operations
===============================================================================

.. module:: regshape.libs.docker.operations
   :platform: Unix, Windows
   :synopsis: Functions for listing local Docker images, exporting them as OCI
              Image Layouts, and pushing them to remote OCI registries. Uses the
              Docker Engine API via the ``docker`` Python SDK.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import gzip
import hashlib
import io
import json
import logging
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import docker as docker_sdk
from docker.errors import APIError, DockerException, ImageNotFound

from regshape.libs.errors import DockerError, LayoutError
from regshape.libs.layout.operations import (
    PushResult,
    add_blob,
    init_layout,
    push_layout,
)
from regshape.libs.models.descriptor import Descriptor, Platform
from regshape.libs.models.manifest import ImageIndex, ImageManifest
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Data models
# ===========================================================================


@dataclass
class DockerImageInfo:
    """Summary of a local Docker image.

    :param id: Short image ID (sha256 prefix).
    :param repo_tags: Repository tags (e.g. ``["nginx:latest"]``).
    :param repo_digests: Repository digests (e.g. ``["nginx@sha256:abc..."]``).
    :param size: Image size in bytes.
    :param created: ISO 8601 creation timestamp.
    :param architecture: CPU architecture (e.g. ``amd64``).
    :param os: Operating system (e.g. ``linux``).
    """

    id: str
    repo_tags: list[str]
    repo_digests: list[str]
    size: int
    created: str
    architecture: str
    os: str


# ===========================================================================
# Private helpers
# ===========================================================================

_GZIP_MAGIC = b"\x1f\x8b"


def _get_docker_client() -> docker_sdk.DockerClient:
    """Create a Docker client from environment; raise DockerError on failure."""
    try:
        return docker_sdk.from_env()
    except DockerException as exc:
        raise DockerError(
            "Cannot connect to Docker daemon. Is Docker Desktop running?",
            str(exc),
        ) from exc


def _compress_gzip(data: bytes) -> bytes:
    """Gzip-compress *data* with deterministic (zero) mtime."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(data)
    return buf.getvalue()


def _is_gzipped(data: bytes) -> bool:
    """Return True if *data* starts with the gzip magic bytes."""
    return data[:2] == _GZIP_MAGIC


def _ensure_gzip(data: bytes) -> bytes:
    """Return gzip-compressed *data*; compress if not already gzipped."""
    if _is_gzipped(data):
        return data
    return _compress_gzip(data)


def _parse_platform_string(platform_str: str) -> tuple[str, str]:
    """Parse ``os/architecture`` into ``(os, architecture)``.

    :raises DockerError: If the format is invalid.
    """
    parts = platform_str.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise DockerError(
            f"Invalid platform format: {platform_str!r}",
            "expected 'os/architecture' (e.g. 'linux/amd64')",
        )
    return parts[0], parts[1]


def _extract_docker_save_tar(image_ref: str, client: docker_sdk.DockerClient) -> tuple[list[dict], tarfile.TarFile, bytes]:
    """Save a Docker image and extract its tar contents.

    :returns: Tuple of (parsed ``manifest.json`` list of dicts, TarFile object, raw tar bytes).
    :raises DockerError: On image-not-found or API errors.
    """
    try:
        image = client.images.get(image_ref)
    except ImageNotFound:
        raise DockerError(
            f"Image {image_ref!r} not found in local Docker store",
            "run 'regshape docker list' to see available images",
        )
    except APIError as exc:
        raise DockerError(
            f"Docker API error while fetching image {image_ref!r}",
            str(exc),
        ) from exc

    try:
        chunks = []
        for chunk in image.save(named=True):
            chunks.append(chunk)
        tar_bytes = b"".join(chunks)
    except APIError as exc:
        raise DockerError(
            f"Docker API error while saving image {image_ref!r}",
            str(exc),
        ) from exc

    tar_buffer = io.BytesIO(tar_bytes)
    try:
        tar = tarfile.open(fileobj=tar_buffer, mode="r")
    except tarfile.TarError as exc:
        raise DockerError(
            f"Failed to read docker save tar for {image_ref!r}",
            str(exc),
        ) from exc

    # Parse manifest.json from the Docker save tar
    try:
        manifest_member = tar.getmember("manifest.json")
        manifest_fh = tar.extractfile(manifest_member)
        if manifest_fh is None:
            raise DockerError(
                "manifest.json in docker save tar is empty",
                f"image: {image_ref}",
            )
        docker_manifests = json.loads(manifest_fh.read())
    except (KeyError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise DockerError(
            f"Failed to parse manifest.json from docker save tar for {image_ref!r}",
            str(exc),
        ) from exc

    return docker_manifests, tar, tar_bytes


def _read_tar_member(tar: tarfile.TarFile, member_path: str) -> bytes:
    """Read a member from a TarFile, raising DockerError on failure."""
    try:
        member = tar.getmember(member_path)
        fh = tar.extractfile(member)
        if fh is None:
            raise DockerError(
                f"Tar member {member_path!r} is not a regular file",
                "cannot extract",
            )
        return fh.read()
    except (KeyError, tarfile.TarError) as exc:
        raise DockerError(
            f"Failed to read {member_path!r} from docker save tar",
            str(exc),
        ) from exc


def _docker_config_to_oci(config_data: bytes) -> bytes:
    """Convert a Docker config JSON to OCI Image Config format.

    Strips Docker-proprietary top-level fields while preserving OCI-relevant
    fields (architecture, os, rootfs, config, history, etc.).

    :param config_data: Raw Docker config JSON bytes.
    :returns: OCI config JSON bytes.
    """
    config = json.loads(config_data)

    # Fields defined by the OCI Image Config spec
    oci_config: dict = {}

    # Preserve standard OCI fields
    for key in ("architecture", "os", "os.version", "os.features",
                "variant", "config", "rootfs", "history", "created", "author"):
        if key in config:
            oci_config[key] = config[key]

    return json.dumps(oci_config, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _build_oci_manifest(
    config_descriptor: Descriptor,
    layer_descriptors: list[Descriptor],
) -> bytes:
    """Build an OCI Image Manifest JSON from config and layer descriptors.

    :returns: Canonical manifest JSON bytes.
    """
    manifest = ImageManifest(
        schema_version=2,
        media_type=OCI_IMAGE_MANIFEST,
        config=config_descriptor,
        layers=layer_descriptors,
    )
    return manifest.to_json().encode("utf-8")


def _convert_single_image(
    layout_path: Path,
    tar: tarfile.TarFile,
    docker_manifest_entry: dict,
) -> tuple[Descriptor, str, str]:
    """Convert one Docker save manifest entry to OCI blobs in the layout.

    :param layout_path: Root of the initialised OCI layout.
    :param tar: Open TarFile of the docker save output.
    :param docker_manifest_entry: One element from the docker save manifest.json.
    :returns: Tuple of (manifest_descriptor, architecture, os_name).
    """
    # 1. Read and convert config
    config_path = docker_manifest_entry["Config"]
    raw_config = _read_tar_member(tar, config_path)
    try:
        config_json = json.loads(raw_config)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DockerError(
            f"Failed to parse Docker image config '{config_path}' as JSON"
        ) from exc
    architecture = config_json.get("architecture", "unknown")
    os_name = config_json.get("os", "unknown")

    oci_config_bytes = _docker_config_to_oci(raw_config)
    config_digest, config_size = add_blob(layout_path, oci_config_bytes)
    config_descriptor = Descriptor(
        media_type=OCI_IMAGE_CONFIG,
        digest=config_digest,
        size=config_size,
    )

    # 2. Process layers
    layer_descriptors: list[Descriptor] = []
    for layer_path in docker_manifest_entry.get("Layers", []):
        layer_data = _read_tar_member(tar, layer_path)
        compressed = _ensure_gzip(layer_data)
        layer_digest, layer_size = add_blob(layout_path, compressed)
        layer_descriptors.append(
            Descriptor(
                media_type=OCI_IMAGE_LAYER_TAR_GZIP,
                digest=layer_digest,
                size=layer_size,
            )
        )

    # 3. Build and write OCI manifest
    manifest_bytes = _build_oci_manifest(config_descriptor, layer_descriptors)
    manifest_digest, manifest_size = add_blob(layout_path, manifest_bytes)
    manifest_descriptor = Descriptor(
        media_type=OCI_IMAGE_MANIFEST,
        digest=manifest_digest,
        size=manifest_size,
    )

    return manifest_descriptor, architecture, os_name


def _write_index_json(layout_path: Path, index: ImageIndex) -> None:
    """Write an OCI Image Index to layout_path/index.json."""
    content = json.dumps(json.loads(index.to_json()), indent=2).encode("utf-8")
    index_file = layout_path / "index.json"

    # Write atomically via a temporary file then replace.
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=layout_path, delete=False
    ) as tmp_file:
        tmp_file.write(content)
        tmp_path = Path(tmp_file.name)

    os.replace(tmp_path, index_file)


# ===========================================================================
# Public operations
# ===========================================================================


def list_images(name_filter: str | None = None) -> list[DockerImageInfo]:
    """List images available in the local Docker daemon.

    :param name_filter: Optional substring filter on image repository name.
    :returns: List of :class:`DockerImageInfo` summaries.
    :raises DockerError: If the Docker daemon is unreachable or returns an
        error.
    """
    client = _get_docker_client()

    try:
        images = client.images.list()
    except APIError as exc:
        raise DockerError(
            "Failed to list Docker images",
            str(exc),
        ) from exc

    results: list[DockerImageInfo] = []
    for img in images:
        attrs = img.attrs or {}
        repo_tags = img.tags or []

        # Apply name filter
        if name_filter:
            if not any(name_filter in tag for tag in repo_tags):
                continue

        results.append(
            DockerImageInfo(
                id=attrs.get("Id", img.id or ""),
                repo_tags=repo_tags,
                repo_digests=attrs.get("RepoDigests", []),
                size=attrs.get("Size", 0),
                created=attrs.get("Created", ""),
                architecture=attrs.get("Architecture", "unknown"),
                os=attrs.get("Os", "unknown"),
            )
        )

    return results


def export_image(
    image_ref: str,
    output_path: Union[str, Path],
    *,
    platform: str | None = None,
) -> None:
    """Export a Docker image as an OCI Image Layout directory.

    Layers are always gzip-compressed. Multi-platform images are fully
    supported: when *platform* is ``None`` all variants are exported,
    otherwise only the matching platform is included.

    :param image_ref: Docker image reference (e.g. ``nginx:latest`` or image ID).
    :param output_path: Filesystem path for the OCI layout directory.
    :param platform: Optional platform filter in ``os/architecture`` format.
    :raises DockerError: On daemon errors, image-not-found, or
        platform-not-found.
    :raises LayoutError: On filesystem / layout errors.
    """
    output = Path(output_path)

    # Validate output path before talking to Docker
    if output.exists():
        if (output / "oci-layout").exists():
            raise LayoutError(
                f"{output} is already an OCI Image Layout",
                "oci-layout file already exists; use a new directory or remove the existing layout",
            )
        if any(output.iterdir()):
            raise LayoutError(
                f"Output directory {output} already exists and is not empty",
                "use a new directory or remove the existing contents",
            )

    client = _get_docker_client()

    # Extract docker save tar
    docker_manifests, tar, _ = _extract_docker_save_tar(image_ref, client)

    if not docker_manifests:
        raise DockerError(
            f"No manifest entries found in docker save output for {image_ref!r}",
            "manifest.json is empty",
        )

    # Initialise OCI layout
    init_layout(output)

    # Determine which manifest entries to process
    is_multi = len(docker_manifests) > 1

    if platform is not None:
        filter_os, filter_arch = _parse_platform_string(platform)

    manifest_descriptors: list[Descriptor] = []

    available_platforms = []
    for entry in docker_manifests:
        # Read config to get platform info
        config_path = entry["Config"]
        raw_config = _read_tar_member(tar, config_path)
        config_json = json.loads(raw_config)
        img_arch = config_json.get("architecture", "unknown")
        img_os = config_json.get("os", "unknown")

        # Apply platform filter
        if platform is not None:
            # Record available platforms for error reporting if no match is found
            available_platforms.append(f"{img_os}/{img_arch}")
            if img_os != filter_os or img_arch != filter_arch:
                continue

        descriptor, architecture, os_name = _convert_single_image(
            output, tar, entry
        )

        # Add platform info to the descriptor for multi-platform layouts
        descriptor = Descriptor(
            media_type=descriptor.media_type,
            digest=descriptor.digest,
            size=descriptor.size,
            platform=Platform(architecture=architecture, os=os_name),
        )
        manifest_descriptors.append(descriptor)

    tar.close()

    if not manifest_descriptors:
        if platform is not None:
            raise DockerError(
                f"Platform {platform!r} not available for image {image_ref!r}",
                f"Available: {', '.join(available_platforms)}",
            )
        raise DockerError(
            f"No images found to export for {image_ref!r}",
            "manifest entries produced no OCI manifests",
        )

    # Write index.json
    index = ImageIndex(
        schema_version=2,
        media_type=OCI_IMAGE_INDEX,
        manifests=manifest_descriptors,
    )
    _write_index_json(output, index)

    logger.info(
        "Exported %d manifest(s) to %s",
        len(manifest_descriptors),
        output,
    )


def push_image(
    image_ref: str,
    dest: str,
    *,
    platform: str | None = None,
    insecure: bool = False,
    force: bool = False,
    chunked: bool = False,
    chunk_size: int = 65536,
) -> PushResult:
    """Export a Docker image and push it to a remote OCI registry.

    Creates a temporary OCI layout, then delegates to
    :func:`~regshape.libs.layout.operations.push_layout`.

    :param image_ref: Docker image reference (e.g. ``nginx:latest``).
    :param dest: Destination registry reference (``registry/repo[:tag]``).
    :param platform: Optional platform filter in ``os/architecture`` format.
    :param insecure: Allow HTTP (no TLS).
    :param force: Skip blob existence checks.
    :param chunked: Use chunked upload protocol for blobs.
    :param chunk_size: Chunk size in bytes for chunked uploads.
    :returns: :class:`~regshape.libs.layout.operations.PushResult`.
    :raises DockerError: On daemon errors.
    :raises LayoutError: On layout errors.
    """
    from regshape.libs.refs import parse_image_ref
    from regshape.libs.transport.client import RegistryClient, TransportConfig

    tmp_dir = tempfile.mkdtemp(prefix="regshape-docker-")
    tmp_layout = Path(tmp_dir) / "layout"

    try:
        export_image(image_ref, tmp_layout, platform=platform)

        registry, repo, reference = parse_image_ref(dest)

        config = TransportConfig(
            registry=registry,
            insecure=insecure,
        )
        client = RegistryClient(config)

        result = push_layout(
            layout_path=tmp_layout,
            client=client,
            repo=repo,
            tag_override=reference,
            force=force,
            chunked=chunked,
            chunk_size=chunk_size,
        )
        return result
    finally:
        # Clean up temp directory
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

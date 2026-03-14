#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.docker.operations`.

All Docker daemon interactions are mocked via ``unittest.mock``.
"""

import gzip
import hashlib
import io
import json
import tarfile
import tempfile

import pytest
from unittest.mock import MagicMock, patch

from regshape.libs.docker.operations import (
    DockerImageInfo,
    export_image,
    list_images,
    push_image,
    _compress_gzip,
    _docker_config_to_oci,
    _ensure_gzip,
    _is_gzipped,
    _parse_platform_string,
)
from regshape.libs.errors import DockerError, LayoutError
from regshape.libs.models.mediatype import (
    OCI_IMAGE_CONFIG,
    OCI_IMAGE_INDEX,
    OCI_IMAGE_LAYER_TAR_GZIP,
    OCI_IMAGE_MANIFEST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _make_docker_save_tar(manifests_json: list[dict], configs: dict, layers: dict) -> bytes:
    """Build a fake docker-save tar archive in-memory.

    :param manifests_json: The manifest.json content (list of dicts).
    :param configs: Mapping of config_path -> config JSON bytes.
    :param layers: Mapping of layer_path -> layer bytes.
    :returns: Raw tar bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # manifest.json
        mj_bytes = json.dumps(manifests_json).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(mj_bytes)
        tar.addfile(info, io.BytesIO(mj_bytes))

        # configs
        for path, content in configs.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

        # layers
        for path, content in layers.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    return buf.getvalue()


def _make_docker_config(architecture: str = "amd64", os_name: str = "linux") -> bytes:
    """Build a minimal Docker config JSON."""
    return json.dumps({
        "architecture": architecture,
        "os": os_name,
        "rootfs": {"type": "layers", "diff_ids": ["sha256:abc"]},
        "config": {"Env": ["PATH=/usr/bin"]},
        "history": [{"created": "2025-01-01T00:00:00Z"}],
    }).encode("utf-8")


def _make_single_platform_tar(architecture: str = "amd64", os_name: str = "linux") -> bytes:
    """Build a Docker save tar with a single platform image."""
    config = _make_docker_config(architecture, os_name)
    config_hash = hashlib.sha256(config).hexdigest()
    config_path = f"{config_hash}.json"
    layer_data = b"fake layer data"
    layer_path = "abc123/layer.tar"

    manifest = [{
        "Config": config_path,
        "RepoTags": ["testimage:latest"],
        "Layers": [layer_path],
    }]

    return _make_docker_save_tar(manifest, {config_path: config}, {layer_path: layer_data})


def _make_multi_platform_tar() -> bytes:
    """Build a Docker save tar with two platform variants."""
    config_amd64 = _make_docker_config("amd64", "linux")
    config_arm64 = _make_docker_config("arm64", "linux")

    hash_amd64 = hashlib.sha256(config_amd64).hexdigest()
    hash_arm64 = hashlib.sha256(config_arm64).hexdigest()

    layer_amd64 = b"amd64 layer"
    layer_arm64 = b"arm64 layer"

    manifests = [
        {
            "Config": f"{hash_amd64}.json",
            "RepoTags": ["testimage:latest"],
            "Layers": ["amd64/layer.tar"],
        },
        {
            "Config": f"{hash_arm64}.json",
            "RepoTags": ["testimage:latest"],
            "Layers": ["arm64/layer.tar"],
        },
    ]

    configs = {
        f"{hash_amd64}.json": config_amd64,
        f"{hash_arm64}.json": config_arm64,
    }
    layers = {
        "amd64/layer.tar": layer_amd64,
        "arm64/layer.tar": layer_arm64,
    }

    return _make_docker_save_tar(manifests, configs, layers)


def _mock_image_save(tar_bytes: bytes):
    """Return a generator that yields the tar bytes in chunks (simulating image.save())."""
    def save(named=True):
        # Yield in one chunk for simplicity
        yield tar_bytes
    return save


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_compress_gzip(self):
        data = b"hello world"
        compressed = _compress_gzip(data)
        assert compressed[:2] == b"\x1f\x8b"
        decompressed = gzip.decompress(compressed)
        assert decompressed == data

    def test_is_gzipped_true(self):
        compressed = _compress_gzip(b"test")
        assert _is_gzipped(compressed) is True

    def test_is_gzipped_false(self):
        assert _is_gzipped(b"plain data") is False

    def test_ensure_gzip_compresses_uncompressed(self):
        data = b"uncompressed"
        result = _ensure_gzip(data)
        assert _is_gzipped(result)
        assert gzip.decompress(result) == data

    def test_ensure_gzip_passes_through_gzipped(self):
        compressed = _compress_gzip(b"already compressed")
        result = _ensure_gzip(compressed)
        assert result == compressed

    def test_parse_platform_string_valid(self):
        os_name, arch = _parse_platform_string("linux/amd64")
        assert os_name == "linux"
        assert arch == "amd64"

    def test_parse_platform_string_invalid(self):
        with pytest.raises(DockerError, match="Invalid platform format"):
            _parse_platform_string("invalid")

    def test_parse_platform_string_empty_parts(self):
        with pytest.raises(DockerError, match="Invalid platform format"):
            _parse_platform_string("/")


class TestDockerConfigToOci:
    def test_preserves_oci_fields(self):
        docker_config = json.dumps({
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
            "config": {"Env": ["PATH=/usr/bin"]},
            "history": [],
            "created": "2025-01-01T00:00:00Z",
        }).encode("utf-8")

        oci_bytes = _docker_config_to_oci(docker_config)
        oci = json.loads(oci_bytes)

        assert oci["architecture"] == "amd64"
        assert oci["os"] == "linux"
        assert "rootfs" in oci
        assert "config" in oci
        assert "history" in oci
        assert "created" in oci

    def test_strips_docker_proprietary_fields(self):
        docker_config = json.dumps({
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
            "container": "abcdef123456",
            "docker_version": "20.10.0",
            "container_config": {"Hostname": "abc"},
        }).encode("utf-8")

        oci_bytes = _docker_config_to_oci(docker_config)
        oci = json.loads(oci_bytes)

        assert "container" not in oci
        assert "docker_version" not in oci
        assert "container_config" not in oci


# ---------------------------------------------------------------------------
# Tests: list_images
# ---------------------------------------------------------------------------


class TestListImages:
    @patch("regshape.libs.docker.operations.docker_sdk")
    def test_list_images_returns_info(self, mock_sdk):
        mock_client = MagicMock()
        mock_sdk.from_env.return_value = mock_client

        mock_image = MagicMock()
        mock_image.tags = ["nginx:latest"]
        mock_image.id = "sha256:abc123"
        mock_image.attrs = {
            "Id": "sha256:abc123def456",
            "RepoDigests": ["nginx@sha256:aaa"],
            "Size": 196083713,
            "Created": "2025-12-01T10:30:00Z",
            "Architecture": "amd64",
            "Os": "linux",
        }
        mock_client.images.list.return_value = [mock_image]

        result = list_images()

        assert len(result) == 1
        assert result[0].id == "sha256:abc123def456"
        assert result[0].repo_tags == ["nginx:latest"]
        assert result[0].size == 196083713
        assert result[0].architecture == "amd64"

    @patch("regshape.libs.docker.operations.docker_sdk")
    def test_list_images_with_filter(self, mock_sdk):
        mock_client = MagicMock()
        mock_sdk.from_env.return_value = mock_client

        img1 = MagicMock()
        img1.tags = ["nginx:latest"]
        img1.id = "sha256:aaa"
        img1.attrs = {"Id": "sha256:aaa", "RepoDigests": [], "Size": 100,
                       "Created": "", "Architecture": "amd64", "Os": "linux"}

        img2 = MagicMock()
        img2.tags = ["python:3.12"]
        img2.id = "sha256:bbb"
        img2.attrs = {"Id": "sha256:bbb", "RepoDigests": [], "Size": 200,
                       "Created": "", "Architecture": "amd64", "Os": "linux"}

        mock_client.images.list.return_value = [img1, img2]

        result = list_images(name_filter="nginx")
        assert len(result) == 1
        assert result[0].repo_tags == ["nginx:latest"]

    @patch("regshape.libs.docker.operations.docker_sdk")
    def test_list_images_daemon_not_running(self, mock_sdk):
        from docker.errors import DockerException
        mock_sdk.from_env.side_effect = DockerException("connection refused")

        with pytest.raises(DockerError, match="Cannot connect to Docker daemon"):
            list_images()


# ---------------------------------------------------------------------------
# Tests: export_image
# ---------------------------------------------------------------------------


class TestExportImage:
    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_single_platform(self, mock_get_client, tmp_path):
        tar_bytes = _make_single_platform_tar()
        mock_client = MagicMock()
        mock_image = MagicMock()
        mock_image.save = _mock_image_save(tar_bytes)
        mock_client.images.get.return_value = mock_image
        mock_get_client.return_value = mock_client

        output = tmp_path / "layout"
        export_image("testimage:latest", output)

        # Verify OCI layout structure
        assert (output / "oci-layout").exists()
        assert (output / "index.json").exists()
        assert (output / "blobs" / "sha256").is_dir()

        # Verify index.json
        index = json.loads((output / "index.json").read_text())
        assert index["schemaVersion"] == 2
        assert index["mediaType"] == OCI_IMAGE_INDEX
        assert len(index["manifests"]) == 1

        manifest_desc = index["manifests"][0]
        assert manifest_desc["mediaType"] == OCI_IMAGE_MANIFEST
        assert manifest_desc["platform"]["architecture"] == "amd64"
        assert manifest_desc["platform"]["os"] == "linux"

        # Verify manifest blob exists and is valid
        digest = manifest_desc["digest"]
        _, hex_digest = digest.split(":", 1)
        manifest_blob = output / "blobs" / "sha256" / hex_digest
        assert manifest_blob.exists()
        manifest_data = json.loads(manifest_blob.read_text())
        assert manifest_data["mediaType"] == OCI_IMAGE_MANIFEST
        assert manifest_data["config"]["mediaType"] == OCI_IMAGE_CONFIG
        assert len(manifest_data["layers"]) == 1
        assert manifest_data["layers"][0]["mediaType"] == OCI_IMAGE_LAYER_TAR_GZIP

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_multi_platform_all(self, mock_get_client, tmp_path):
        tar_bytes = _make_multi_platform_tar()
        mock_client = MagicMock()
        mock_image = MagicMock()
        mock_image.save = _mock_image_save(tar_bytes)
        mock_client.images.get.return_value = mock_image
        mock_get_client.return_value = mock_client

        output = tmp_path / "layout"
        export_image("testimage:latest", output)

        index = json.loads((output / "index.json").read_text())
        assert len(index["manifests"]) == 2

        platforms = {
            (m["platform"]["os"], m["platform"]["architecture"])
            for m in index["manifests"]
        }
        assert ("linux", "amd64") in platforms
        assert ("linux", "arm64") in platforms

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_multi_platform_filtered(self, mock_get_client, tmp_path):
        tar_bytes = _make_multi_platform_tar()
        mock_client = MagicMock()
        mock_image = MagicMock()
        mock_image.save = _mock_image_save(tar_bytes)
        mock_client.images.get.return_value = mock_image
        mock_get_client.return_value = mock_client

        output = tmp_path / "layout"
        export_image("testimage:latest", output, platform="linux/arm64")

        index = json.loads((output / "index.json").read_text())
        assert len(index["manifests"]) == 1
        assert index["manifests"][0]["platform"]["architecture"] == "arm64"

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_platform_not_found(self, mock_get_client, tmp_path):
        tar_bytes = _make_single_platform_tar("amd64", "linux")
        mock_client = MagicMock()
        mock_image = MagicMock()
        mock_image.save = _mock_image_save(tar_bytes)
        mock_client.images.get.return_value = mock_image
        mock_get_client.return_value = mock_client

        output = tmp_path / "layout"
        with pytest.raises(DockerError, match="Platform.*not available"):
            export_image("testimage:latest", output, platform="linux/arm64")

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_image_not_found(self, mock_get_client, tmp_path):
        from docker.errors import ImageNotFound
        mock_client = MagicMock()
        mock_client.images.get.side_effect = ImageNotFound("not found")
        mock_get_client.return_value = mock_client

        output = tmp_path / "layout"
        with pytest.raises(DockerError, match="not found in local Docker store"):
            export_image("nonexistent:latest", output)
    def test_export_fails_if_output_is_existing_layout_without_docker_mock(self, tmp_path):
        output = tmp_path / "layout"
        output.mkdir()
        (output / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')

        with pytest.raises(LayoutError, match="already an OCI Image Layout"):
            export_image("testimage:latest", output)

    def test_export_fails_if_output_dir_not_empty_without_docker_mock(self, tmp_path):
        output = tmp_path / "layout"
        output.mkdir()
        (output / "somefile.txt").write_text("hello")

        with pytest.raises(LayoutError, match="not empty"):
            export_image("testimage:latest", output)

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_fails_if_output_is_existing_layout_with_mocked_client(
        self, mock_get_client, tmp_path
    ):
        output = tmp_path / "layout"
        output.mkdir()
        (output / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')

        with pytest.raises(LayoutError, match="already an OCI Image Layout"):
            export_image("testimage:latest", output)

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_fails_if_output_dir_not_empty_with_mocked_client(
        self, mock_get_client, tmp_path
    ):
        output = tmp_path / "layout"
        output.mkdir()
        (output / "somefile.txt").write_text("hello")

        with pytest.raises(LayoutError, match="not empty"):
            export_image("testimage:latest", output)

    @patch("regshape.libs.docker.operations._get_docker_client")
    def test_export_layers_are_gzip_compressed(self, mock_get_client, tmp_path):
        tar_bytes = _make_single_platform_tar()
        mock_client = MagicMock()
        mock_image = MagicMock()
        mock_image.save = _mock_image_save(tar_bytes)
        mock_client.images.get.return_value = mock_image
        mock_get_client.return_value = mock_client

        output = tmp_path / "layout"
        export_image("testimage:latest", output)

        # Find layer blobs and verify they are gzip-compressed
        index = json.loads((output / "index.json").read_text())
        manifest_digest = index["manifests"][0]["digest"]
        _, manifest_hex = manifest_digest.split(":", 1)
        manifest_data = json.loads(
            (output / "blobs" / "sha256" / manifest_hex).read_text()
        )

        for layer in manifest_data["layers"]:
            _, layer_hex = layer["digest"].split(":", 1)
            layer_bytes = (output / "blobs" / "sha256" / layer_hex).read_bytes()
            # Verify gzip magic bytes
            assert layer_bytes[:2] == b"\x1f\x8b", "Layer should be gzip-compressed"


# ---------------------------------------------------------------------------
# Tests: push_image
# ---------------------------------------------------------------------------


class TestPushImage:
    @patch("regshape.libs.docker.operations.push_layout")
    @patch("regshape.libs.docker.operations.export_image")
    def test_push_calls_export_and_push_layout(self, mock_export, mock_push_layout):
        mock_push_result = MagicMock()
        mock_push_layout.return_value = mock_push_result

        result = push_image(
            "nginx:latest",
            "registry.io/myrepo/nginx:v1",
            insecure=False,
            force=True,
            chunked=True,
            chunk_size=131072,
        )

        # export_image was called with a temp layout path
        mock_export.assert_called_once()
        call_args = mock_export.call_args
        assert call_args[0][0] == "nginx:latest"
        assert call_args[1]["platform"] is None

        # push_layout was called with correct params
        mock_push_layout.assert_called_once()
        push_kwargs = mock_push_layout.call_args[1]
        assert push_kwargs["repo"] == "myrepo/nginx"
        assert push_kwargs["tag_override"] == "v1"
        assert push_kwargs["force"] is True
        assert push_kwargs["chunked"] is True
        assert push_kwargs["chunk_size"] == 131072

        assert result is mock_push_result

    @patch("regshape.libs.docker.operations.push_layout")
    @patch("regshape.libs.docker.operations.export_image")
    def test_push_passes_platform_to_export(self, mock_export, mock_push_layout):
        mock_push_layout.return_value = MagicMock()

        push_image(
            "nginx:latest",
            "registry.io/myrepo/nginx:v1",
            platform="linux/arm64",
        )

        call_kwargs = mock_export.call_args[1]
        assert call_kwargs["platform"] == "linux/arm64"

    @patch("regshape.libs.docker.operations.push_layout")
    @patch("regshape.libs.docker.operations.export_image")
    def test_push_uses_insecure_for_transport_config(self, mock_export, mock_push_layout):
        mock_push_layout.return_value = MagicMock()

        push_image(
            "nginx:latest",
            "registry.io/myrepo/nginx:v1",
            insecure=True,
        )

        push_kwargs = mock_push_layout.call_args[1]
        client = push_kwargs["client"]
        assert client.config.insecure is True

    @patch("regshape.libs.docker.operations.push_layout")
    @patch("regshape.libs.docker.operations.export_image")
    def test_push_cleans_up_temp_dir_on_success(self, mock_export, mock_push_layout, tmp_path):
        import os
        mock_push_layout.return_value = MagicMock()

        created_dirs = []
        original_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(**kwargs):
            d = original_mkdtemp(**kwargs)
            created_dirs.append(d)
            return d

        with patch("regshape.libs.docker.operations.tempfile.mkdtemp", side_effect=tracking_mkdtemp):
            push_image("nginx:latest", "registry.io/myrepo/nginx:v1")

        assert len(created_dirs) == 1
        assert not os.path.exists(created_dirs[0])

    @patch("regshape.libs.docker.operations.push_layout")
    @patch("regshape.libs.docker.operations.export_image")
    def test_push_cleans_up_temp_dir_on_failure(self, mock_export, mock_push_layout):
        import os
        mock_export.side_effect = DockerError("export failed", "test")

        created_dirs = []
        original_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(**kwargs):
            d = original_mkdtemp(**kwargs)
            created_dirs.append(d)
            return d

        with patch("regshape.libs.docker.operations.tempfile.mkdtemp", side_effect=tracking_mkdtemp):
            with pytest.raises(DockerError):
                push_image("nginx:latest", "registry.io/myrepo/nginx:v1")

        assert len(created_dirs) == 1
        assert not os.path.exists(created_dirs[0])

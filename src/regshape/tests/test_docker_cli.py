#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.docker`.

Uses the Click test runner to exercise all docker CLI commands.
Library functions are mocked to avoid requiring a running Docker daemon.
"""

import json

import pytest
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from regshape.cli.main import regshape
from regshape.libs.docker.operations import DockerImageInfo
from regshape.libs.errors import DockerError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner():
    return CliRunner()


def _sample_images():
    return [
        DockerImageInfo(
            id="sha256:a8758716bb6a92c62e8e2f5a463d53a31ef3a32e12cd1a41ee1030e5560268b0",
            repo_tags=["nginx:latest", "nginx:1.25"],
            repo_digests=["nginx@sha256:aaa"],
            size=196083713,
            created="2025-12-01T10:30:00Z",
            architecture="amd64",
            os="linux",
        ),
        DockerImageInfo(
            id="sha256:b5d5cef26b2a3e9f2dfc4e1b0e5d1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
            repo_tags=["python:3.12"],
            repo_digests=[],
            size=1073741824,
            created="2025-11-15T08:00:00Z",
            architecture="amd64",
            os="linux",
        ),
    ]


# ---------------------------------------------------------------------------
# docker list
# ---------------------------------------------------------------------------


class TestDockerListCLI:
    @patch("regshape.cli.docker.list_images")
    def test_list_plain_output(self, mock_list):
        mock_list.return_value = _sample_images()
        result = _runner().invoke(regshape, ["docker", "list"])
        assert result.exit_code == 0, result.output
        assert "nginx" in result.output
        assert "python" in result.output
        assert "REPOSITORY" in result.output

    @patch("regshape.cli.docker.list_images")
    def test_list_json_output(self, mock_list):
        mock_list.return_value = _sample_images()
        result = _runner().invoke(regshape, ["docker", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 2
        assert data[0]["repo_tags"] == ["nginx:latest", "nginx:1.25"]
        assert data[1]["repo_tags"] == ["python:3.12"]

    @patch("regshape.cli.docker.list_images")
    def test_list_with_filter(self, mock_list):
        mock_list.return_value = [_sample_images()[0]]
        result = _runner().invoke(regshape, ["docker", "list", "--filter", "nginx"])
        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(name_filter="nginx")

    @patch("regshape.cli.docker.list_images")
    def test_list_empty(self, mock_list):
        mock_list.return_value = []
        result = _runner().invoke(regshape, ["docker", "list"])
        assert result.exit_code == 0, result.output
        assert "No images found" in result.output

    @patch("regshape.cli.docker.list_images")
    def test_list_docker_error(self, mock_list):
        mock_list.side_effect = DockerError(
            "Cannot connect to Docker daemon. Is Docker Desktop running?",
            "connection refused",
        )
        result = _runner().invoke(regshape, ["docker", "list"])
        assert result.exit_code != 0
        assert "Error" in result.output


# ---------------------------------------------------------------------------
# docker export
# ---------------------------------------------------------------------------


class TestDockerExportCLI:
    @patch("regshape.cli.docker.export_image")
    def test_export_plain_output(self, mock_export, tmp_path):
        output = str(tmp_path / "layout")
        result = _runner().invoke(
            regshape, ["docker", "export", "--image", "nginx:latest", "--output", output]
        )
        assert result.exit_code == 0, result.output
        assert "Exported" in result.output
        assert "nginx:latest" in result.output
        mock_export.assert_called_once_with("nginx:latest", output, platform=None)

    @patch("regshape.cli.docker.export_image")
    def test_export_json_output(self, mock_export, tmp_path):
        output = str(tmp_path / "layout")
        result = _runner().invoke(
            regshape,
            ["docker", "export", "--image", "nginx:latest", "--output", output, "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["image"] == "nginx:latest"
        assert data["output"] == output

    @patch("regshape.cli.docker.export_image")
    def test_export_with_platform(self, mock_export, tmp_path):
        output = str(tmp_path / "layout")
        result = _runner().invoke(
            regshape,
            ["docker", "export", "--image", "nginx:latest", "--output", output,
             "--platform", "linux/amd64"],
        )
        assert result.exit_code == 0, result.output
        assert "linux/amd64" in result.output
        mock_export.assert_called_once_with(
            "nginx:latest", output, platform="linux/amd64"
        )

    @patch("regshape.cli.docker.export_image")
    def test_export_docker_error(self, mock_export, tmp_path):
        mock_export.side_effect = DockerError(
            "Image 'nope' not found in local Docker store",
            "not found",
        )
        output = str(tmp_path / "layout")
        result = _runner().invoke(
            regshape, ["docker", "export", "--image", "nope", "--output", output]
        )
        assert result.exit_code != 0
        assert "Error" in result.output

    def test_export_missing_required_options(self):
        result = _runner().invoke(regshape, ["docker", "export"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# docker push
# ---------------------------------------------------------------------------


class TestDockerPushCLI:
    @patch("regshape.cli.docker.push_image")
    def test_push_plain_output(self, mock_push):
        mock_result = MagicMock()
        mock_result.manifests = [
            MagicMock(blobs=[
                MagicMock(uploaded=True),
                MagicMock(uploaded=False),
            ])
        ]
        mock_push.return_value = mock_result

        result = _runner().invoke(
            regshape,
            ["docker", "push", "--image", "nginx:latest",
             "--dest", "registry.io/myrepo/nginx:v1"],
        )
        assert result.exit_code == 0, result.output
        assert "Pushed" in result.output
        assert "1 manifest(s)" in result.output
        assert "1 blob(s) uploaded" in result.output
        assert "1 blob(s) skipped" in result.output

    @patch("regshape.cli.docker.push_image")
    def test_push_json_output(self, mock_push):
        mock_result = MagicMock()
        mock_result.manifests = [
            MagicMock(blobs=[
                MagicMock(uploaded=True),
                MagicMock(uploaded=True),
            ])
        ]
        mock_push.return_value = mock_result

        result = _runner().invoke(
            regshape,
            ["docker", "push", "--image", "nginx:latest",
             "--dest", "registry.io/myrepo/nginx:v1", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["image"] == "nginx:latest"
        assert data["destination"] == "registry.io/myrepo/nginx:v1"
        assert data["manifests_pushed"] == 1
        assert data["blobs_uploaded"] == 2

    @patch("regshape.cli.docker.push_image")
    def test_push_with_platform(self, mock_push):
        mock_result = MagicMock()
        mock_result.manifests = []
        mock_push.return_value = mock_result

        result = _runner().invoke(
            regshape,
            ["docker", "push", "--image", "nginx:latest",
             "--dest", "registry.io/myrepo/nginx:v1",
             "--platform", "linux/arm64"],
        )
        assert result.exit_code == 0, result.output

    @patch("regshape.cli.docker.push_image")
    def test_push_docker_error(self, mock_push):
        mock_push.side_effect = DockerError(
            "Cannot connect to Docker daemon. Is Docker Desktop running?",
            "connection refused",
        )
        result = _runner().invoke(
            regshape,
            ["docker", "push", "--image", "nginx:latest",
             "--dest", "registry.io/myrepo/nginx:v1"],
        )
        assert result.exit_code != 0
        assert "Error" in result.output

    @patch("regshape.cli.docker.push_image")
    def test_push_with_force_and_chunked(self, mock_push):
        mock_result = MagicMock()
        mock_result.manifests = []
        mock_push.return_value = mock_result

        result = _runner().invoke(
            regshape,
            ["docker", "push", "--image", "nginx:latest",
             "--dest", "registry.io/myrepo/nginx:v1",
             "--force", "--chunked", "--chunk-size", "131072"],
        )
        assert result.exit_code == 0, result.output
        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args
        assert call_kwargs[1]["force"] is True
        assert call_kwargs[1]["chunked"] is True
        assert call_kwargs[1]["chunk_size"] == 131072

    def test_push_missing_required_options(self):
        result = _runner().invoke(regshape, ["docker", "push"])
        assert result.exit_code != 0

#!/usr/bin/env python3

"""Tests for ``layout push`` — both the library function and the CLI command.

Library operations are tested with mocked registry calls (no real network).
CLI tests use the Click test runner.
"""

import gzip
import io
import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from regshape.cli.main import regshape
from regshape.libs.errors import BlobError, LayoutError
from regshape.libs.layout.operations import (
    PushResult,
    generate_config,
    generate_manifest,
    init_layout,
    push_layout,
    stage_layer,
    validate_layout,
)
from regshape.libs.models.mediatype import OCI_IMAGE_LAYER_TAR_GZIP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner():
    return CliRunner()


def _make_gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def _build_layout(tmp_path, ref_name="latest"):
    """Create a complete single-manifest layout for testing."""
    layout_dir = tmp_path / "layout"
    init_layout(layout_dir)
    layer = _make_gzip(b"test layer content")
    stage_layer(layout_dir, layer, OCI_IMAGE_LAYER_TAR_GZIP)
    generate_config(layout_dir)
    generate_manifest(layout_dir, ref_name=ref_name)
    return layout_dir


def _build_multi_manifest_layout(tmp_path):
    """Create a layout with two manifests for testing."""
    layout_dir = tmp_path / "layout"
    init_layout(layout_dir)
    layer = _make_gzip(b"test layer content")
    stage_layer(layout_dir, layer, OCI_IMAGE_LAYER_TAR_GZIP)
    generate_config(layout_dir)
    generate_manifest(layout_dir, ref_name="v1")
    # Stage a second layer + config + manifest to get two entries in index.json
    layer2 = _make_gzip(b"second layer content")
    stage_layer(layout_dir, layer2, OCI_IMAGE_LAYER_TAR_GZIP)
    generate_config(layout_dir)
    generate_manifest(layout_dir, ref_name="v2")
    return layout_dir


def _mock_client(registry="registry.io"):
    """Return a mock RegistryClient."""
    client = MagicMock()
    client.config = MagicMock()
    client.config.registry = registry
    client.config.insecure = False
    return client


# ---------------------------------------------------------------------------
# Library: push_layout
# ---------------------------------------------------------------------------


class TestPushLayout:
    """Tests for the push_layout library function."""

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_uploads_all_blobs_and_manifest(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path)
        client = _mock_client()

        # All blobs are new (HEAD returns 404)
        mock_head.side_effect = BlobError("not found", "404")
        mock_upload.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage",
                             tag_override="latest")

        assert isinstance(result, PushResult)
        assert result.manifests_pushed == 1
        assert result.blobs_uploaded == 2  # 1 layer + 1 config
        assert result.blobs_skipped == 0
        assert mock_upload.call_count == 2
        assert mock_push_manifest.call_count == 1

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_skips_existing_blobs(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path)
        client = _mock_client()

        # All blobs already exist (HEAD returns success)
        mock_head.return_value = MagicMock()
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage",
                             tag_override="latest")

        assert result.blobs_uploaded == 0
        assert result.blobs_skipped == 2
        mock_upload.assert_not_called()

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_force_skips_head_check(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path)
        client = _mock_client()

        mock_upload.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage",
                             tag_override="latest", force=True)

        mock_head.assert_not_called()
        assert result.blobs_uploaded == 2

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob_chunked")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_chunked_mode(
        self, mock_head, mock_upload_chunked, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path)
        client = _mock_client()

        mock_head.side_effect = BlobError("not found", "404")
        mock_upload_chunked.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage",
                             tag_override="latest", chunked=True,
                             chunk_size=1024)

        assert mock_upload_chunked.call_count == 2
        assert result.blobs_uploaded == 2

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_uses_ref_name_annotation(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path, ref_name="v1.0")
        client = _mock_client()

        mock_head.side_effect = BlobError("not found", "404")
        mock_upload.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage")

        assert result.manifests[0].reference == "v1.0"
        # Verify the manifest was pushed with "v1.0" as reference
        call_args = mock_push_manifest.call_args
        assert call_args[0][2] == "v1.0"

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_tag_override_wins(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path, ref_name="v1.0")
        client = _mock_client()

        mock_head.side_effect = BlobError("not found", "404")
        mock_upload.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage",
                             tag_override="prod")

        assert result.manifests[0].reference == "prod"
        call_args = mock_push_manifest.call_args
        assert call_args[0][2] == "prod"

    def test_push_fails_on_invalid_layout(self, tmp_path):
        layout_dir = tmp_path / "bad"
        layout_dir.mkdir()
        client = _mock_client()

        with pytest.raises(LayoutError):
            push_layout(layout_dir, client, "myrepo/myimage")

    def test_push_fails_on_empty_index(self, tmp_path):
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        client = _mock_client()

        with pytest.raises(LayoutError, match="no manifests"):
            push_layout(layout_dir, client, "myrepo/myimage")

    def test_push_fails_tag_override_with_multi_manifest(self, tmp_path):
        """Tag override with multiple manifests should fail."""
        layout_dir = tmp_path / "layout"
        init_layout(layout_dir)
        layer = _make_gzip(b"layer1")
        stage_layer(layout_dir, layer, OCI_IMAGE_LAYER_TAR_GZIP)
        generate_config(layout_dir)
        generate_manifest(layout_dir, ref_name="first")
        # Reset staging to add a second manifest
        from regshape.libs.layout.operations import _read_stage_raw, _write_stage
        stage = _read_stage_raw(layout_dir)
        stage["layers"] = []
        stage["config"] = None
        stage["manifest"] = None
        _write_stage(layout_dir, stage)
        layer2 = _make_gzip(b"layer2")
        stage_layer(layout_dir, layer2, OCI_IMAGE_LAYER_TAR_GZIP)
        generate_config(layout_dir, architecture="arm64")
        generate_manifest(layout_dir, ref_name="second")

        client = _mock_client()
        with pytest.raises(LayoutError, match="tag override"):
            push_layout(layout_dir, client, "myrepo/myimage",
                        tag_override="latest")

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_progress_callback_receives_events(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path)
        client = _mock_client()

        mock_head.side_effect = BlobError("not found", "404")
        mock_upload.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        events = []

        def cb(event, **kwargs):
            events.append(event)

        push_layout(layout_dir, client, "myrepo/myimage",
                    tag_override="latest", progress_callback=cb)

        assert "blob_start" in events
        assert "blob_done" in events
        assert "manifest_done" in events

    @patch("regshape.libs.manifests.push_manifest")
    @patch("regshape.libs.blobs.upload_blob")
    @patch("regshape.libs.blobs.head_blob")
    def test_push_result_has_correct_summary(
        self, mock_head, mock_upload, mock_push_manifest, tmp_path
    ):
        layout_dir = _build_layout(tmp_path)
        client = _mock_client()

        # First blob exists, second is new
        call_count = [0]
        def head_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise BlobError("not found", "404")
            return MagicMock()

        mock_head.side_effect = head_side_effect
        mock_upload.return_value = "sha256:confirmed"
        mock_push_manifest.return_value = "sha256:manifest_confirmed"

        result = push_layout(layout_dir, client, "myrepo/myimage",
                             tag_override="latest")

        assert result.blobs_uploaded == 1
        assert result.blobs_skipped == 1
        assert result.manifests_pushed == 1
        assert result.bytes_uploaded > 0


# ---------------------------------------------------------------------------
# CLI: layout push
# ---------------------------------------------------------------------------


class TestPushCLI:
    """Tests for the layout push CLI command."""

    @patch("regshape.cli.layout.RegistryClient")
    @patch("regshape.cli.layout.push_layout")
    def test_push_basic(self, mock_push, mock_client_cls, tmp_path):
        layout_dir = _build_layout(tmp_path)
        mock_push.return_value = PushResult(
            layout_path=str(layout_dir),
            destination="registry.io/myrepo",
            manifests_pushed=1,
            blobs_uploaded=2,
            blobs_skipped=0,
            bytes_uploaded=1024,
        )

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
        ])
        assert result.exit_code == 0, result.output
        assert "Push complete" in result.stderr

    @patch("regshape.cli.layout.RegistryClient")
    @patch("regshape.cli.layout.push_layout")
    def test_push_json_output(self, mock_push, mock_client_cls, tmp_path):
        layout_dir = _build_layout(tmp_path)
        from regshape.libs.layout.operations import BlobPushReport, ManifestPushReport
        mock_push.return_value = PushResult(
            layout_path=str(layout_dir),
            destination="registry.io/myrepo",
            manifests=[ManifestPushReport(
                digest="sha256:aaa",
                reference="latest",
                media_type="application/vnd.oci.image.manifest.v1+json",
                blobs=[BlobPushReport(
                    digest="sha256:bbb",
                    size=100,
                    media_type="application/vnd.oci.image.layer.v1.tar+gzip",
                    action="uploaded",
                )],
                status="pushed",
            )],
            manifests_pushed=1,
            blobs_uploaded=1,
            blobs_skipped=0,
            bytes_uploaded=100,
        )

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["summary"]["manifests_pushed"] == 1
        assert data["manifests"][0]["status"] == "pushed"

    def test_push_dry_run(self, tmp_path):
        layout_dir = _build_layout(tmp_path)

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
            "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "[dry-run]" in result.output
        assert "Would upload blob" in result.output
        assert "Would push manifest" in result.output

    def test_push_dry_run_json(self, tmp_path):
        layout_dir = _build_layout(tmp_path)

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
            "--dry-run",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert len(data["manifests"]) == 1
        assert len(data["manifests"][0]["blobs"]) >= 1

    def test_push_fails_bad_dest(self, tmp_path):
        layout_dir = _build_layout(tmp_path)

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "nope",
        ])
        assert result.exit_code != 0

    @patch("regshape.cli.layout.RegistryClient")
    @patch("regshape.cli.layout.push_layout")
    def test_push_layout_error_exits_1(self, mock_push, mock_client_cls, tmp_path):
        layout_dir = _build_layout(tmp_path)
        mock_push.side_effect = LayoutError("invalid", "test")

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
        ])
        assert result.exit_code == 1

    @patch("regshape.cli.layout.RegistryClient")
    @patch("regshape.cli.layout.push_layout")
    def test_push_with_force_flag(self, mock_push, mock_client_cls, tmp_path):
        layout_dir = _build_layout(tmp_path)
        mock_push.return_value = PushResult(
            layout_path=str(layout_dir),
            destination="registry.io/myrepo",
            manifests_pushed=1,
            blobs_uploaded=2,
            blobs_skipped=0,
            bytes_uploaded=1024,
        )

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
            "--force",
        ])
        assert result.exit_code == 0, result.output
        # Verify force=True was passed
        call_kwargs = mock_push.call_args[1]
        assert call_kwargs["force"] is True

    @patch("regshape.cli.layout.RegistryClient")
    @patch("regshape.cli.layout.push_layout")
    def test_push_with_chunked_flags(self, mock_push, mock_client_cls, tmp_path):
        layout_dir = _build_layout(tmp_path)
        mock_push.return_value = PushResult(
            layout_path=str(layout_dir),
            destination="registry.io/myrepo",
            manifests_pushed=1,
            blobs_uploaded=2,
            blobs_skipped=0,
            bytes_uploaded=1024,
        )

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:latest",
            "--chunked",
            "--chunk-size", "1048576",
        ])
        assert result.exit_code == 0, result.output
        call_kwargs = mock_push.call_args[1]
        assert call_kwargs["chunked"] is True
        assert call_kwargs["chunk_size"] == 1048576

    def test_dry_run_rejects_tag_override_with_multiple_manifests(self, tmp_path):
        layout_dir = _build_multi_manifest_layout(tmp_path)

        result = _runner().invoke(regshape, [
            "layout", "push",
            "--path", str(layout_dir),
            "--dest", "registry.io/myrepo:custom_tag",
            "--dry-run",
        ])
        assert result.exit_code != 0
        assert "tag override" in result.output.lower() or "multiple" in result.output.lower()

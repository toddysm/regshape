#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.refs`."""

import pytest

from regshape.libs.refs import format_ref, parse_image_ref


# ===========================================================================
# parse_image_ref
# ===========================================================================

class TestParseImageRef:

    # --- happy-path: tag references ---

    def test_registry_repo_tag(self):
        r, repo, ref = parse_image_ref("acr.io/myrepo/myimage:v1.0")
        assert r == "acr.io"
        assert repo == "myrepo/myimage"
        assert ref == "v1.0"

    def test_registry_single_name_tag(self):
        r, repo, ref = parse_image_ref("acr.io/myimage:latest")
        assert r == "acr.io"
        assert repo == "myimage"
        assert ref == "latest"

    def test_no_tag_defaults_to_latest(self):
        r, repo, ref = parse_image_ref("acr.io/myimage")
        assert ref == "latest"

    def test_localhost_registry(self):
        r, repo, ref = parse_image_ref("localhost/myimage:v2")
        assert r == "localhost"
        assert ref == "v2"

    def test_localhost_with_port(self):
        r, repo, ref = parse_image_ref("localhost:5000/myimage:v2")
        assert r == "localhost:5000"
        assert repo == "myimage"
        assert ref == "v2"

    def test_registry_with_port_and_tag(self):
        r, repo, ref = parse_image_ref("acr.io:443/repo:tag")
        assert r == "acr.io:443"
        assert repo == "repo"
        assert ref == "tag"

    # --- happy-path: digest references ---

    def test_registry_repo_digest(self):
        r, repo, ref = parse_image_ref("acr.io/myrepo/myimage@sha256:abc123")
        assert r == "acr.io"
        assert repo == "myrepo/myimage"
        assert ref == "sha256:abc123"

    def test_registry_single_name_digest(self):
        r, repo, ref = parse_image_ref("acr.io/myimage@sha256:deadbeef")
        assert repo == "myimage"
        assert ref == "sha256:deadbeef"

    def test_sha512_digest(self):
        r, repo, ref = parse_image_ref("acr.io/img@sha512:ffffffff")
        assert ref == "sha512:ffffffff"

    # --- error paths ---

    def test_no_registry_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot determine registry"):
            parse_image_ref("myimage:tag")

    def test_bare_name_no_registry_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot determine registry"):
            parse_image_ref("myimage")

    def test_empty_repo_after_registry_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot determine repository"):
            parse_image_ref("acr.io/")

    def test_registry_only_no_repo_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot determine repository"):
            parse_image_ref("acr.io")


# ===========================================================================
# format_ref
# ===========================================================================

class TestFormatRef:

    def test_tag_uses_colon_separator(self):
        result = format_ref("acr.io", "myrepo/myimage", "latest")
        assert result == "acr.io/myrepo/myimage:latest"

    def test_sha256_digest_uses_at_separator(self):
        result = format_ref("acr.io", "myrepo/myimage", "sha256:abc123")
        assert result == "acr.io/myrepo/myimage@sha256:abc123"

    def test_sha512_digest_uses_at_separator(self):
        result = format_ref("acr.io", "img", "sha512:deadbeef")
        assert result == "acr.io/img@sha512:deadbeef"

    def test_arbitrary_tag_uses_colon(self):
        result = format_ref("registry.example.io", "app/service", "v2.3.4")
        assert result == "registry.example.io/app/service:v2.3.4"

    def test_localhost_registry(self):
        result = format_ref("localhost:5000", "myimage", "dev")
        assert result == "localhost:5000/myimage:dev"

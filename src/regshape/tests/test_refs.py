#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.refs`."""

import pytest

from regshape.libs.errors import ReferenceValidationError, UnsafeReferenceError
from regshape.libs.refs import (
    format_ref,
    is_digest,
    parse_artifact_ref,
    parse_image_ref,
    parse_repository_ref,
    reject_unsafe_reference,
    validate_digest,
)


@pytest.mark.parametrize("reference,digest_shaped", [
    ("latest", False), ("sha256", False), ("v1.2-rc_1", False), ("", False),
    ("sha256:" + "a" * 64, True), ("sha512:" + "b" * 128, True),
    ("sha384:abc", True), ("sha256:", True), ("sha256:xyz", True),
])
def test_digest_classification_preserves_invalid_digest_kind(reference, digest_shaped):
    """Malformed and unsupported digests must not fall through to tag resolution."""
    assert is_digest(reference) is digest_shaped
    separator = "@" if digest_shaped else ":"
    assert format_ref("registry.example.invalid", "pkg", reference) == (
        f"registry.example.invalid/pkg{separator}{reference}"
    )


@pytest.mark.parametrize("reference", ["sha256:" + "a" * 64, "sha512:" + "b" * 128])
def test_validate_digest_preserves_complete_supported_identity(reference):
    """Validation accepts both supported algorithms without rewriting the token."""
    assert validate_digest(reference) == reference


@pytest.mark.parametrize("reference", [
    "latest", "", None, "sha384:" + "a" * 96, "SHA256:" + "a" * 64,
    "sha256", "sha256:", "sha256:" + "a" * 63, "sha256:" + "a" * 65,
    "sha512:" + "a" * 127, "sha512:" + "a" * 129,
    "sha256:" + "g" * 64, "sha256:" + "A" * 64,
    "sha256:" + "a" * 64 + "\n", " sha256:" + "a" * 64,
])
def test_validate_digest_rejects_unsupported_or_malformed_identity(reference):
    """Reject wrong algorithms, invalid lengths/non-hex and unnormalized input."""
    with pytest.raises(ReferenceValidationError):
        validate_digest(reference)


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
# parse_artifact_ref
# ===========================================================================

class TestParseArtifactRef:
    """Verify both parser names share safe inputs, results and error behavior."""

    @pytest.mark.parametrize("reference,expected", [
        ("registry.example.invalid/team/pkg:v1", ("registry.example.invalid", "team/pkg", "v1")),
        ("registry.example.invalid/pkg", ("registry.example.invalid", "pkg", "latest")),
        ("localhost:5000/team/pkg", ("localhost:5000", "team/pkg", "latest")),
        ("localhost:5000/pkg:v2", ("localhost:5000", "pkg", "v2")),
        ("registry.example.invalid:443/pkg:tag", ("registry.example.invalid:443", "pkg", "tag")),
        ("registry.example.invalid/pkg@sha256:abc123", ("registry.example.invalid", "pkg", "sha256:abc123")),
        ("registry.example.invalid/pkg@sha512:abcd", ("registry.example.invalid", "pkg", "sha512:abcd")),
        ("registry.example.invalid/pkg:", ("registry.example.invalid", "pkg", "")),
    ])
    def test_artifact_and_legacy_call_forms_match(
        self, reference: str, expected: tuple[str, str, str],
    ) -> None:
        """Keep exact results for safe inputs, including unvalidated digest/tag payloads."""
        assert parse_artifact_ref(reference) == expected
        assert parse_artifact_ref(artifact_ref=reference) == expected
        assert parse_image_ref(reference) == expected
        assert parse_image_ref(image_ref=reference) == expected

    @pytest.mark.parametrize("reference", ["", "pkg", "pkg:v1", "registry.example.invalid", "registry.example.invalid/"])
    def test_artifact_and_legacy_errors_match(self, reference: str) -> None:
        """Both parser names report the same safety and missing-registry errors."""
        with pytest.raises(ValueError) as old_error:
            parse_image_ref(image_ref=reference)
        with pytest.raises(ValueError) as new_error:
            parse_artifact_ref(artifact_ref=reference)
        assert str(new_error.value) == str(old_error.value)


@pytest.mark.parametrize("value,expected", [
    ("acr.io/repo", ("acr.io", "repo")),
    ("acr.io/team/nested/repo", ("acr.io", "team/nested/repo")),
    ("acr.io:443/team/repo", ("acr.io:443", "team/repo")),
    ("localhost/repo", ("localhost", "repo")),
    ("localhost:5000/team/repo", ("localhost:5000", "team/repo")),
    ("[::1]:5000/team/repo", ("[::1]:5000", "team/repo")),
])
def test_repository_ref_preserves_registry_and_nested_path(value, expected):
    """Plain repository parsing keeps ports/nesting and does not invent a tag."""
    assert parse_repository_ref(value) == expected


@pytest.mark.parametrize("suffix", [
    ":v1", ":latest", ":", "@sha256:" + "a" * 64, "@sha512:abcd", "/", "//",
])
def test_repository_ref_rejects_qualifiers_and_trailing_slashes(suffix):
    """All qualifiers and the previously rejected trailing slash use one diagnostic."""
    with pytest.raises(ValueError) as caught:
        parse_repository_ref("localhost:5000/team/pkg" + suffix)
    assert str(caught.value) == (
        "Repository must be a plain 'registry/repository' without a tag or digest"
    )


@pytest.mark.parametrize("value", ["repo", "repo:v1", "acr.io", "acr.io/"])
def test_repository_ref_requires_registry_and_repository(value):
    """A plain-repository API must not guess a registry or accept an empty path."""
    with pytest.raises(ValueError, match="Cannot determine"):
        parse_repository_ref(value)


@pytest.mark.parametrize("parser", [
    reject_unsafe_reference, parse_image_ref, parse_artifact_ref, parse_repository_ref,
])
@pytest.mark.parametrize("value", [
    "", None,
    "https://synthetic-user:synthetic-secret@acr.io/team/pkg:v1",
    "//synthetic-user:synthetic-secret@acr.io/team/pkg:v1",
    "//synthetic-user:synthetic-secret@acr.io:5000",
    "/synthetic-user:synthetic-secret@acr.io:5000",
    "//acr.io/team/pkg",
    "https:/acr.io/team/pkg",
    "synthetic-user:synthetic-secret@acr.io:5000/team/pkg:v1",
    "synthetic-user@acr.io/team/pkg:v1",
    "acr.io/pkg@sha256:abc@sha256:def",
    "acr.io/pkg@bad",
    "acr.io/pkg@synthetic-user:synthetic-secret/other",
    "acr.io/pkg name", " acr.io/pkg", "acr.io/pkg\u00a0",
    "acr.io/pkg\t", "acr.io/pkg\n", "acr.io/pkg\r", "acr.io/pkg\x00",
    "acr.io/pkg\x1b", "acr.io/pkg\x7f", "acr.io/pkg\x80",
    "acr.io/pkg\\other", "acr.io/pkg?query", "acr.io/pkg#fragment", "acr.io/pkg%2fother",
])
def test_shared_reference_safety_precedes_parsing_and_never_echoes_input(parser, value):
    """Every parser and direct SDK guard reject unsafe raw syntax with a safe error."""
    with pytest.raises(UnsafeReferenceError) as caught:
        parser(value)
    assert str(caught.value) == "Use a registry/repository tag or digest, not a URL."
    assert "synthetic-user" not in str(caught.value)
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("parser", [parse_image_ref, parse_artifact_ref])
@pytest.mark.parametrize("value,expected", [
    ("acr.io/pkg@bad", ("acr.io", "pkg", "bad")),
    ("acr.io/pkg:bad%2ftag", ("acr.io", "pkg", "bad%2ftag")),
])
def test_parser_safety_optout_is_explicit_and_preserves_legacy_tokens(parser, value, expected):
    """Only explicit SDK strict=False restores unsafe legacy parsing for conformance tests."""
    with pytest.raises(UnsafeReferenceError):
        parser(value)
    assert parser(value, strict=False) == expected


def test_repository_safety_optout_does_not_disable_plain_repository_check():
    """Conformance callers can bypass safety, but cannot turn qualifiers into repository names."""
    assert parse_repository_ref("acr.io/pkg%2fchild", strict=False) == ("acr.io", "pkg%2fchild")
    with pytest.raises(ValueError, match="plain"):
        parse_repository_ref("acr.io/pkg:latest", strict=False)


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

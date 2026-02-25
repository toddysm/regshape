#!/usr/bin/env python3

"""
:mod: `test_sanitization` - Test suite for header sanitization utilities
=========================================================================

    module:: test_sanitization
    :platform: Unix, Windows
    :synopsis: Tests for ``regshape.libs.decorators.sanitization``.
               Covers all branches of ``redact_header_value`` and
               ``redact_headers``, with emphasis on security-critical
               edge cases.
    moduleauthor:: ToddySM <toddysm@gmail.com>

Test categories:
  - TestSensitiveHeaders       — membership and immutability of the denylist
  - TestRedactHeaderValue      — per-value redaction logic (all branches)
  - TestRedactHeaders          — whole-dict redaction convenience function
"""

import pytest

from regshape.libs.decorators.sanitization import (
    SENSITIVE_HEADERS,
    redact_header_value,
    redact_headers,
)


# ===========================================================================
# TestSensitiveHeaders
# ===========================================================================

class TestSensitiveHeaders:
    """Verify the denylist constant is correct and immutable."""

    def test_contains_authorization(self):
        assert "authorization" in SENSITIVE_HEADERS

    def test_contains_proxy_authorization(self):
        assert "proxy-authorization" in SENSITIVE_HEADERS

    def test_contains_cookie(self):
        assert "cookie" in SENSITIVE_HEADERS

    def test_contains_set_cookie(self):
        assert "set-cookie" in SENSITIVE_HEADERS

    def test_is_frozenset(self):
        assert isinstance(SENSITIVE_HEADERS, frozenset)

    def test_is_immutable(self):
        with pytest.raises((AttributeError, TypeError)):
            SENSITIVE_HEADERS.add("x-custom")  # type: ignore[attr-defined]

    def test_non_sensitive_header_not_in_denylist(self):
        for header in ("content-type", "x-request-id", "accept", "cache-control"):
            assert header not in SENSITIVE_HEADERS


# ===========================================================================
# TestRedactHeaderValue
# ===========================================================================

class TestRedactHeaderValue:
    """Unit tests for redact_header_value — one test per branch/edge-case."""

    # --- Non-sensitive headers -------------------------------------------

    def test_non_sensitive_header_returned_unchanged(self):
        assert redact_header_value("Content-Type", "application/json") == "application/json"

    def test_non_sensitive_header_empty_value_returned_unchanged(self):
        assert redact_header_value("X-Custom", "") == ""

    def test_non_sensitive_header_case_insensitive_check(self):
        """Header names are normalised to lowercase before denylist lookup."""
        assert redact_header_value("CONTENT-TYPE", "text/plain") == "text/plain"

    # --- Authorization header --------------------------------------------

    def test_authorization_bearer_redacts_token(self):
        result = redact_header_value("Authorization", "Bearer super-secret-token")
        assert result == "Bearer <redacted>"
        assert "super-secret-token" not in result

    def test_authorization_basic_redacts_credentials(self):
        result = redact_header_value("Authorization", "Basic dXNlcjpwYXNz")
        assert result == "Basic <redacted>"
        assert "dXNlcjpwYXNz" not in result

    def test_authorization_header_name_case_insensitive(self):
        """Mixed-case header names must still be redacted."""
        result = redact_header_value("AUTHORIZATION", "Bearer token123")
        assert result == "Bearer <redacted>"

    def test_authorization_mixed_case_header_name(self):
        result = redact_header_value("Authorization", "Bearer token123")
        assert result == "Bearer <redacted>"

    def test_authorization_scheme_only_no_space(self):
        """A value with no space (scheme token only, no credentials) is fully redacted."""
        result = redact_header_value("Authorization", "Bearer")
        # partition(" ") gives ("Bearer", "", "") — scheme is non-empty → "Bearer <redacted>"
        assert result == "Bearer <redacted>"

    def test_authorization_empty_value_fully_redacted(self):
        """An empty Authorization value falls through to the final <redacted>."""
        result = redact_header_value("Authorization", "")
        assert result == "<redacted>"

    def test_authorization_multiple_spaces_keeps_only_first_token(self):
        """Only the first whitespace-delimited token (scheme) is preserved."""
        result = redact_header_value("Authorization", "Bearer tok en with spaces")
        assert result == "Bearer <redacted>"
        assert "tok" not in result

    # --- Proxy-Authorization header ---------------------------------------

    def test_proxy_authorization_redacted(self):
        result = redact_header_value("Proxy-Authorization", "Basic abc123")
        assert result == "Basic <redacted>"
        assert "abc123" not in result

    def test_proxy_authorization_case_insensitive(self):
        result = redact_header_value("PROXY-AUTHORIZATION", "Bearer xyz")
        assert result == "Bearer <redacted>"

    def test_proxy_authorization_empty_value_fully_redacted(self):
        result = redact_header_value("Proxy-Authorization", "")
        assert result == "<redacted>"

    # --- Cookie header ---------------------------------------------------

    def test_cookie_fully_redacted(self):
        result = redact_header_value("Cookie", "session=abc123; csrf=xyz")
        assert result == "<redacted>"
        assert "abc123" not in result
        assert "xyz" not in result

    def test_cookie_case_insensitive(self):
        result = redact_header_value("COOKIE", "session=secret")
        assert result == "<redacted>"

    def test_cookie_empty_value_redacted(self):
        result = redact_header_value("Cookie", "")
        assert result == "<redacted>"

    # --- Set-Cookie header -----------------------------------------------

    def test_set_cookie_fully_redacted(self):
        result = redact_header_value("Set-Cookie", "session=abc123; HttpOnly; Secure")
        assert result == "<redacted>"
        assert "abc123" not in result

    def test_set_cookie_case_insensitive(self):
        result = redact_header_value("SET-COOKIE", "id=42; Path=/")
        assert result == "<redacted>"

    def test_set_cookie_empty_value_redacted(self):
        result = redact_header_value("Set-Cookie", "")
        assert result == "<redacted>"


# ===========================================================================
# TestRedactHeaders
# ===========================================================================

class TestRedactHeaders:
    """Unit tests for the whole-dict redact_headers convenience function."""

    def test_empty_dict_returns_empty_dict(self):
        assert redact_headers({}) == {}

    def test_non_sensitive_headers_passed_through(self):
        headers = {"Content-Type": "application/json", "X-Request-Id": "abc"}
        assert redact_headers(headers) == headers

    def test_sensitive_headers_redacted(self):
        headers = {
            "Authorization": "Bearer token123",
            "Cookie": "session=secret",
        }
        result = redact_headers(headers)
        assert result["Authorization"] == "Bearer <redacted>"
        assert result["Cookie"] == "<redacted>"

    def test_original_dict_not_mutated(self):
        headers = {"Authorization": "Bearer token123", "Content-Type": "application/json"}
        original = dict(headers)
        redact_headers(headers)
        assert headers == original

    def test_mixed_sensitive_and_non_sensitive(self):
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
            "Accept": "application/vnd.oci.image.manifest.v1+json",
            "Set-Cookie": "id=42; Path=/",
        }
        result = redact_headers(headers)
        assert result["Content-Type"] == "application/json"
        assert result["Accept"] == "application/vnd.oci.image.manifest.v1+json"
        assert result["Authorization"] == "Bearer <redacted>"
        assert result["Set-Cookie"] == "<redacted>"

    def test_all_sensitive_headers_at_once(self):
        headers = {
            "Authorization": "Bearer tok",
            "Proxy-Authorization": "Basic creds",
            "Cookie": "a=b",
            "Set-Cookie": "c=d",
        }
        result = redact_headers(headers)
        for key in headers:
            assert "tok" not in result[key]
            assert "creds" not in result[key]
            assert "a=b" not in result[key]
            assert "c=d" not in result[key]
            assert "<redacted>" in result[key]

    def test_preserves_key_order(self):
        """Dict insertion order is preserved after redaction."""
        headers = {
            "Authorization": "Bearer t",
            "Content-Type": "application/json",
            "Cookie": "s=1",
        }
        assert list(redact_headers(headers).keys()) == list(headers.keys())

    def test_case_insensitive_redaction_in_dict(self):
        """Upper-case header names are still redacted correctly."""
        headers = {"AUTHORIZATION": "Bearer token", "COOKIE": "s=1"}
        result = redact_headers(headers)
        assert result["AUTHORIZATION"] == "Bearer <redacted>"
        assert result["COOKIE"] == "<redacted>"

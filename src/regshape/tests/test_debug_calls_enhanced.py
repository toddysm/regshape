#!/usr/bin/env python3

"""Tests for enhanced --debug-calls formatting."""

import io

from regshape.libs.decorators import TelemetryConfig, configure_telemetry
from regshape.libs.decorators.call_details import format_curl_debug, _body_preview


class TestBodyPreview:
    """Tests for the _body_preview helper."""

    def test_empty_body(self):
        assert _body_preview(None, "", 1) == "(empty)"

    def test_empty_bytes(self):
        assert _body_preview(b"", "application/json", 1) == "(empty)"

    def test_json_body_truncated_at_verbosity_1(self):
        body = b'{"key": "' + b'a' * 300 + b'"}'
        result = _body_preview(body, "application/json", 1)
        assert len(result) <= 203  # 200 + "..."
        assert result.endswith("...")

    def test_json_body_full_at_verbosity_2(self):
        body = b'{"key": "' + b'a' * 300 + b'"}'
        result = _body_preview(body, "application/json", 2)
        assert "..." not in result
        assert len(result) == len(body)

    def test_text_plain_body(self):
        body = b"Hello World"
        result = _body_preview(body, "text/plain", 1)
        assert result == "Hello World"

    def test_binary_body(self):
        body = b"\x00\x01\x02" * 100
        result = _body_preview(body, "application/octet-stream", 1)
        assert result == "<binary, 300 bytes>"

    def test_unknown_content_type_treated_as_binary(self):
        body = b"some data"
        result = _body_preview(body, "application/x-custom", 1)
        assert "<binary," in result

    def test_json_subtype(self):
        body = b'{"schema": 2}'
        result = _body_preview(body, "application/vnd.oci.image.manifest.v1+json", 1)
        assert result == '{"schema": 2}'

    def test_short_json_not_truncated(self):
        body = b'{"ok": true}'
        result = _body_preview(body, "application/json", 1)
        assert result == '{"ok": true}'
        assert "..." not in result


class TestFormatCurlDebugEnhanced:
    """Tests for enhanced format_curl_debug output."""

    def _capture(self, **kwargs):
        buf = io.StringIO()
        log = io.StringIO()
        format_curl_debug(
            method="GET",
            url="https://registry.example.com/v2/myrepo/manifests/latest",
            req_headers={"Accept": "application/json"},
            status_code=200,
            reason="OK",
            resp_headers={"Content-Type": "application/json", "Content-Length": "42"},
            out=buf,
            log_file=log,
            **kwargs,
        )
        return buf.getvalue(), log.getvalue()

    def test_elapsed_line_present(self):
        text, _ = self._capture(elapsed=0.123)
        assert "* Elapsed: 0.123s" in text

    def test_no_elapsed_when_none(self):
        text, _ = self._capture(elapsed=None)
        assert "* Elapsed:" not in text

    def test_body_preview_present(self):
        text, _ = self._capture(
            resp_body=b'{"status": "ok"}',
            verbosity=1,
        )
        assert '* Body: {"status": "ok"}' in text

    def test_empty_body_preview(self):
        text, _ = self._capture(resp_body=None, verbosity=1)
        assert "* Body: (empty)" in text

    def test_binary_body_preview(self):
        text, _ = self._capture(
            resp_body=b"\x00\x01\x02",
            verbosity=1,
        )
        # resp_headers has Content-Type: application/json, so it decodes as text
        # Let's test with proper binary content type
        buf = io.StringIO()
        format_curl_debug(
            method="GET",
            url="https://registry.example.com/v2/myrepo/blobs/sha256:abc",
            req_headers={},
            status_code=200,
            reason="OK",
            resp_headers={"Content-Type": "application/octet-stream"},
            out=buf,
            resp_body=b"\x00\x01\x02" * 100,
            verbosity=1,
        )
        assert "<binary, 300 bytes>" in buf.getvalue()

    def test_req_content_length_line(self):
        text, _ = self._capture(req_content_length=1234)
        assert "> Content-Length: 1234" in text

    def test_no_duplicate_content_length(self):
        """If Content-Length is already in headers, don't add it again."""
        buf = io.StringIO()
        format_curl_debug(
            method="PUT",
            url="https://registry.example.com/v2/myrepo/manifests/latest",
            req_headers={"Content-Length": "1234"},
            status_code=201,
            reason="Created",
            resp_headers={},
            out=buf,
            req_content_length=1234,
        )
        text = buf.getvalue()
        assert text.count("Content-Length: 1234") == 1

    def test_separator_between_calls(self):
        """Blank line at the end of each debug block."""
        text, _ = self._capture(elapsed=0.1, verbosity=1)
        # Output ends with a blank line (separator for next call)
        assert text.endswith("\n\n")

    def test_dual_output_to_log_file(self):
        text, log = self._capture(elapsed=0.1, verbosity=1)
        assert "* Connected to" in text
        assert "* Connected to" in log
        assert "* Elapsed:" in text
        assert "* Elapsed:" in log

    def test_verbosity_2_full_body(self):
        body = b'{"key": "' + b'x' * 500 + b'"}'
        buf = io.StringIO()
        format_curl_debug(
            method="GET",
            url="https://registry.example.com/v2/myrepo/manifests/latest",
            req_headers={},
            status_code=200,
            reason="OK",
            resp_headers={"Content-Type": "application/json"},
            out=buf,
            resp_body=body,
            verbosity=2,
        )
        text = buf.getvalue()
        assert "..." not in text
        # Full body should be present
        assert 'x' * 500 in text

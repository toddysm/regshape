#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.transport.models`."""

import pytest
from unittest.mock import MagicMock

from regshape.libs.transport.models import RegistryRequest, RegistryResponse


class TestRegistryRequest:

    def test_valid_request_creation(self):
        req = RegistryRequest(
            method="GET",
            url="https://registry.example.com/v2/repo/manifests/tag",
            headers={"Accept": "application/vnd.oci.image.manifest.v1+json"}
        )
        assert req.method == "GET"
        assert req.url == "https://registry.example.com/v2/repo/manifests/tag"
        assert req.headers["Accept"] == "application/vnd.oci.image.manifest.v1+json"
        assert req.body is None
        assert req.stream is False

    def test_request_with_body_and_params(self):
        body = b'{"some": "json"}'
        params = {"n": "50", "last": "myrepo"}
        req = RegistryRequest(
            method="POST",
            url="https://registry.example.com/v2/repo/blobs/uploads/",
            headers={"Content-Type": "application/json"},
            body=body,
            params=params,
            stream=True,
            timeout=60
        )
        assert req.method == "POST"
        assert req.body == body
        assert req.params == params
        assert req.stream is True
        assert req.timeout == 60

    def test_empty_method_raises_error(self):
        with pytest.raises(ValueError, match="RegistryRequest\\.method must not be empty"):
            RegistryRequest(
                method="",
                url="https://registry.example.com/v2/",
                headers={}
            )

    def test_empty_url_raises_error(self):
        with pytest.raises(ValueError, match="RegistryRequest\\.url must not be empty"):
            RegistryRequest(
                method="GET",
                url="",
                headers={}
            )

    def test_non_dict_headers_raises_error(self):
        with pytest.raises(TypeError, match="RegistryRequest\\.headers must be a dict"):
            RegistryRequest(
                method="GET",
                url="https://registry.example.com/v2/",
                headers="not-a-dict"
            )

    def test_streaming_body_with_iterable(self):
        body_chunks = [b'chunk1', b'chunk2', b'chunk3']
        req = RegistryRequest(
            method="PUT",
            url="https://registry.example.com/v2/repo/manifests/tag",
            headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
            body=iter(body_chunks),
            stream=True
        )
        assert req.method == "PUT"
        assert req.stream is True
        # Verify we can iterate over the body
        assert list(req.body) == body_chunks


class TestRegistryResponse:

    def test_valid_response_creation(self):
        mock_response = MagicMock()
        mock_response.text = "response text"
        
        resp = RegistryResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b'{"key": "value"}',
            raw_response=mock_response
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/json"
        assert resp.body == b'{"key": "value"}'
        assert resp.raw_response == mock_response

    def test_from_requests_response(self):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.headers = {"Location": "/v2/repo/blobs/sha256:abc123"}
        mock_response.content = b"Created"
        mock_response.text = "Created"

        resp = RegistryResponse.from_requests_response(mock_response)
        assert resp.status_code == 201
        assert resp.headers["Location"] == "/v2/repo/blobs/sha256:abc123"
        assert resp.body == b"Created"
        assert resp.raw_response == mock_response

    def test_from_requests_response_streaming(self):
        """stream=True should NOT read response.content."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/octet-stream"}
        # content should never be accessed
        mock_response.content = property(lambda self: (_ for _ in ()).throw(
            AssertionError("content should not be accessed for streaming")))

        resp = RegistryResponse.from_requests_response(mock_response, stream=True)
        assert resp.status_code == 200
        assert resp.body is None
        assert resp.is_streaming is True

    def test_text_property(self):
        mock_response = MagicMock()
        mock_response.text = "Hello World"
        
        resp = RegistryResponse(
            status_code=200,
            headers={},
            body=b"Hello World",
            raw_response=mock_response
        )
        assert resp.text == "Hello World"

    def test_ok_property_success_status(self):
        mock_response = MagicMock()
        
        # Test various success status codes
        for status_code in [200, 201, 202, 204, 299]:
            resp = RegistryResponse(
                status_code=status_code,
                headers={}, 
                body=b"",
                raw_response=mock_response
            )
            assert resp.ok is True

    def test_ok_property_error_status(self):
        mock_response = MagicMock()
        
        # Test various error status codes  
        for status_code in [199, 300, 400, 401, 404, 500]:
            resp = RegistryResponse(
                status_code=status_code,
                headers={},
                body=b"",
                raw_response=mock_response
            )
            assert resp.ok is False

    def test_non_int_status_code_raises_error(self):
        mock_response = MagicMock()
        
        with pytest.raises(TypeError, match="RegistryResponse\\.status_code must be an int"):
            RegistryResponse(
                status_code="200",
                headers={},
                body=b"",
                raw_response=mock_response
            )

    def test_non_dict_headers_raises_error(self):
        mock_response = MagicMock()
        
        with pytest.raises(TypeError, match="RegistryResponse\\.headers must be a dict"):
            RegistryResponse(
                status_code=200,
                headers="not-a-dict",
                body=b"",
                raw_response=mock_response
            )

    def test_non_bytes_body_raises_error(self):
        mock_response = MagicMock()
        
        with pytest.raises(TypeError, match="RegistryResponse\\.body must be bytes or None"):
            RegistryResponse(
                status_code=200,
                headers={},
                body="not-bytes",
                raw_response=mock_response
            )

    def test_none_body_allowed(self):
        """Test that body=None is valid (streaming responses)."""
        mock_response = MagicMock()
        resp = RegistryResponse(
            status_code=200,
            headers={"Content-Type": "application/octet-stream"},
            body=None,
            raw_response=mock_response
        )
        assert resp.body is None
        assert resp.is_streaming is True

    def test_is_streaming_false_for_buffered(self):
        mock_response = MagicMock()
        resp = RegistryResponse(
            status_code=200,
            headers={},
            body=b"data",
            raw_response=mock_response
        )
        assert resp.is_streaming is False

    def test_content_property_returns_body(self):
        """For buffered responses, .content returns the stored body."""
        mock_response = MagicMock()
        resp = RegistryResponse(
            status_code=200,
            headers={},
            body=b"hello",
            raw_response=mock_response
        )
        assert resp.content == b"hello"

    def test_content_property_lazily_reads_for_streaming(self):
        """For streaming responses, .content reads raw_response.content once."""
        mock_response = MagicMock()
        mock_response.content = b"lazy-body"
        resp = RegistryResponse(
            status_code=200,
            headers={},
            body=None,
            raw_response=mock_response
        )
        assert resp.is_streaming is True
        # Accessing .content should materialise the body
        assert resp.content == b"lazy-body"
        assert resp.body == b"lazy-body"
        assert resp.is_streaming is False

    def test_iter_content_delegates_to_raw_response(self):
        """iter_content should delegate to raw_response.iter_content."""
        mock_response = MagicMock()
        mock_response.iter_content.return_value = iter([b"chunk1", b"chunk2"])
        resp = RegistryResponse(
            status_code=200,
            headers={},
            body=None,
            raw_response=mock_response
        )
        chunks = list(resp.iter_content(chunk_size=1024))
        assert chunks == [b"chunk1", b"chunk2"]
        mock_response.iter_content.assert_called_once_with(chunk_size=1024)

    def test_headers_converted_to_dict(self):
        """Test that from_requests_response converts headers properly."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b""
        
        # Create a simple dict-like object for headers
        class MockHeaders:
            def __init__(self):
                self.data = {"Content-Type": "application/json", "Content-Length": "123"}
            
            def __iter__(self):
                return iter(self.data.items())
        
        mock_response.headers = MockHeaders()
        
        resp = RegistryResponse.from_requests_response(mock_response)
        assert isinstance(resp.headers, dict)
        assert resp.headers["Content-Type"] == "application/json"
        assert resp.headers["Content-Length"] == "123"
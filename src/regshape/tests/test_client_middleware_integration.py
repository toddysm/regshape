#!/usr/bin/env python3

"""
test_client_middleware_integration.py - Test RegistryClient middleware integration

Tests for:
- RegistryClient with middleware enabled/disabled
- TransportConfig middleware options 
- Middleware pipeline integration with existing auth flow
- Backward compatibility with legacy authentication
- Custom middleware in RegistryClient
"""

import pytest
from unittest.mock import Mock, patch, call
import requests

from regshape.libs.transport.client import RegistryClient, TransportConfig
from regshape.libs.transport.middleware import (
    BaseMiddleware, AuthMiddleware, LoggingMiddleware, 
    RetryMiddleware, CachingMiddleware, RetryConfig
)
from regshape.libs.transport.models import RegistryRequest, RegistryResponse
from regshape.libs.errors import AuthError


def _create_mock_response(status_code: int, headers: dict, content: bytes) -> requests.Response:
    """Helper to create mock requests.Response."""
    mock_response = Mock(spec=requests.Response)
    mock_response.status_code = status_code
    mock_response.headers = headers
    mock_response.content = content
    mock_response.text = content.decode('utf-8') if content else ""
    return mock_response


class TestTransportConfigMiddleware:
    """Test TransportConfig middleware options."""
    
    def test_default_config_enables_middleware(self):
        """Test that middleware is enabled by default."""
        config = TransportConfig("registry.example.com")
        
        assert config.enable_middleware is True
        assert config.enable_logging is False
        assert config.enable_retries is False
        assert config.enable_caching is False
        assert config.retry_config is None
        assert config.cache_size == 100
        assert config.middlewares == []
    
    def test_config_with_middleware_options(self):
        """Test configuring middleware options."""
        retry_config = RetryConfig(max_retries=5, backoff_factor=2.0)
        custom_middleware = Mock(spec=BaseMiddleware)
        
        config = TransportConfig(
            registry="registry.example.com",
            enable_logging=True,
            enable_retries=True,
            enable_caching=True,
            retry_config=retry_config,
            cache_size=500,
            middlewares=[custom_middleware]
        )
        
        assert config.enable_logging is True
        assert config.enable_retries is True
        assert config.enable_caching is True
        assert config.retry_config is retry_config
        assert config.cache_size == 500
        assert config.middlewares == [custom_middleware]
    
    def test_config_with_disabled_middleware(self):
        """Test disabling the middleware pipeline entirely."""
        config = TransportConfig(
            registry="registry.example.com",
            enable_middleware=False
        )
        
        assert config.enable_middleware is False


class TestRegistryClientMiddlewareSetup:
    """Test RegistryClient middleware pipeline setup."""
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    def test_client_with_middleware_disabled(self, mock_resolve):
        """Test client initialization with middleware disabled."""
        mock_resolve.return_value = (None, None)
        
        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)
        
        assert client._pipeline is None
        assert client.config.enable_middleware is False
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    def test_client_middleware_setup_no_credentials(self, mock_resolve):
        """Test middleware setup when no credentials are resolved."""
        mock_resolve.return_value = (None, None)
        
        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)
        
        assert client._pipeline is not None
        assert client._pipeline.get_middleware_count() == 0  # No auth middleware added
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    def test_client_middleware_setup_with_credentials(self, mock_resolve):
        """Test middleware setup with resolved credentials."""
        mock_resolve.return_value = ("testuser", "testpass")
        
        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)
        
        assert client._pipeline is not None
        assert client._pipeline.get_middleware_count() == 1  # Auth middleware added
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    def test_client_middleware_setup_with_all_options(self, mock_resolve):
        """Test middleware setup with all built-in middleware enabled.""" 
        mock_resolve.return_value = ("user", "pass")
        
        retry_config = RetryConfig(max_retries=2)
        custom_middleware = Mock(spec=BaseMiddleware)
        
        config = TransportConfig(
            registry="registry.example.com",
            enable_logging=True,
            enable_retries=True,
            enable_caching=True,
            retry_config=retry_config,
            cache_size=200,
            middlewares=[custom_middleware]
        )
        client = RegistryClient(config)
        
        # Should have: Auth + Logging + Retry + Caching + Custom = 5 middleware
        assert client._pipeline.get_middleware_count() == 5


class TestRegistryClientRequestWithMiddleware:
    """Test RegistryClient request handling with middleware enabled."""
    
    @patch('regshape.libs.transport.client.resolve_credentials')  
    @patch('regshape.libs.transport.client.http_request')
    def test_successful_request_with_middleware(self, mock_http_request, mock_resolve):
        """Test successful request through middleware pipeline."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(200, {"Content-Type": "application/json"}, b'{"test": true}')
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)
        
        response = client.get("/v2/test/manifests/latest")
        
        assert response is mock_response
        assert response.status_code == 200
        assert client.last_response is mock_response
        mock_http_request.assert_called_once()
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_request_with_auth_middleware(self, mock_http_request, mock_resolve):
        """Test request with authentication middleware."""
        mock_resolve.return_value = ("user", "pass")
        mock_response = _create_mock_response(200, {}, b"success")
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com") 
        client = RegistryClient(config)
        
        response = client.get("/v2/test/tags/list")
        
        assert response.status_code == 200
        # Verify that http_request was called (auth middleware should add Authorization header)
        mock_http_request.assert_called_once()
        call_args = mock_http_request.call_args
        headers = call_args[1]['headers']
        assert 'Authorization' in headers
        assert headers['Authorization'].startswith('Basic ')
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_request_with_caching_middleware(self, mock_http_request, mock_resolve):
        """Test request with caching middleware."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(200, {"Content-Type": "application/json"}, b'{"cached": true}')
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com", enable_caching=True)
        client = RegistryClient(config)
        
        # First request
        response1 = client.get("/v2/test/manifests/latest")
        assert response1.status_code == 200
        assert mock_http_request.call_count == 1
        
        # Second request should use cache (same GET request)
        response2 = client.get("/v2/test/manifests/latest")
        assert response2.status_code == 200
        assert mock_http_request.call_count == 1  # No additional HTTP call
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_request_with_custom_middleware(self, mock_http_request, mock_resolve):
        """Test request with custom middleware in the pipeline."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(200, {}, b"response")
        mock_http_request.return_value = mock_response
        
        # Custom middleware that adds a header
        class CustomHeaderMiddleware(BaseMiddleware):
            def process_request(self, request):
                request.headers["X-Custom"] = "test-value"
                return request
        
        custom_middleware = CustomHeaderMiddleware()
        config = TransportConfig(
            registry="registry.example.com",
            middlewares=[custom_middleware]
        )
        client = RegistryClient(config)
        
        client.post("/v2/test/blobs/uploads/", headers={"Content-Type": "application/json"})
        
        # Verify custom header was added
        mock_http_request.assert_called_once()
        call_args = mock_http_request.call_args
        headers = call_args[1]['headers']
        assert headers["X-Custom"] == "test-value"
        assert headers["Content-Type"] == "application/json"


class TestRegistryClientLegacyCompatibility:
    """Test RegistryClient backward compatibility with middleware disabled."""
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_legacy_request_without_middleware(self, mock_http_request, mock_resolve):
        """Test request handling with middleware disabled (legacy mode)."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(200, {}, b"legacy response")
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)
        
        response = client.get("/v2/")
        
        assert response is mock_response
        assert response.status_code == 200
        assert client.last_response is mock_response
        mock_http_request.assert_called_once()
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    @patch('regshape.libs.transport.client._normalize_www_authenticate')
    @patch('regshape.libs.auth.registryauth.authenticate')
    def test_legacy_auth_flow_401_handling(self, mock_authenticate, mock_normalize, mock_http_request, mock_resolve):
        """Test legacy 401/WWW-Authenticate flow when middleware is disabled."""
        mock_resolve.return_value = ("user", "pass")
        
        # First response: 401 with WWW-Authenticate
        auth_response = _create_mock_response(401, {"WWW-Authenticate": 'Basic realm="test"'}, b"Unauthorized")
        # Second response: 200 with auth
        success_response = _create_mock_response(200, {}, b"success")
        
        mock_http_request.side_effect = [auth_response, success_response]
        mock_normalize.return_value = ('Basic realm="test"', "Basic")
        mock_authenticate.return_value = "base64encodedcreds"
        
        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)
        
        response = client.get("/v2/test/manifests/latest")
        
        assert response.status_code == 200
        assert mock_http_request.call_count == 2  # Initial request + retry with auth
        mock_authenticate.assert_called_once()
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_legacy_auth_error_no_www_authenticate(self, mock_http_request, mock_resolve):
        """Test legacy auth error handling when no WWW-Authenticate header."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(401, {}, b"Unauthorized")
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)
        
        with pytest.raises(AuthError, match="returned 401 without a WWW-Authenticate header"):
            client.get("/v2/test/manifests/latest")


class TestRegistryClientConvenienceMethods:
    """Test that convenience methods work with both middleware and legacy modes."""
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_all_http_methods_with_middleware(self, mock_http_request, mock_resolve):
        """Test all convenience methods work with middleware enabled."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(200, {}, b"ok")
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)
        
        # Test all HTTP method convenience functions
        methods_and_paths = [
            (client.get, "/v2/"),
            (client.head, "/v2/test/manifests/latest"),
            (client.put, "/v2/test/manifests/latest"),
            (client.post, "/v2/test/blobs/uploads/"),
            (client.patch, "/v2/test/blobs/uploads/uuid"),
            (client.delete, "/v2/test/manifests/latest"),
        ]
        
        for method_func, path in methods_and_paths:
            mock_http_request.reset_mock()
            response = method_func(path)
            assert response.status_code == 200
            mock_http_request.assert_called_once()
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_all_http_methods_legacy_mode(self, mock_http_request, mock_resolve):
        """Test all convenience methods work with middleware disabled."""
        mock_resolve.return_value = (None, None)
        mock_response = _create_mock_response(200, {}, b"legacy ok")
        mock_http_request.return_value = mock_response
        
        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)
        
        # Test all HTTP method convenience functions
        methods_and_paths = [
            (client.get, "/v2/"),
            (client.head, "/v2/test/manifests/latest"),
            (client.put, "/v2/test/manifests/latest"),
            (client.post, "/v2/test/blobs/uploads/"),
            (client.patch, "/v2/test/blobs/uploads/uuid"),
            (client.delete, "/v2/test/manifests/latest"),
        ]
        
        for method_func, path in methods_and_paths:
            mock_http_request.reset_mock()
            response = method_func(path)
            assert response.status_code == 200
            mock_http_request.assert_called_once()


class TestRegistryClientMiddlewareErrorHandling:
    """Test error handling in middleware integration."""
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    def test_auth_error_propagation_from_middleware(self, mock_resolve):
        """Test that AuthError from middleware is properly propagated."""
        mock_resolve.return_value = (None, None)
        
        # Custom middleware that raises AuthError
        class FailingAuthMiddleware(BaseMiddleware):
            def process_request(self, request):
                raise AuthError("Test auth failure", "Simulated auth error")
        
        config = TransportConfig(
            registry="registry.example.com",
            middlewares=[FailingAuthMiddleware()]
        )
        client = RegistryClient(config)
        
        with pytest.raises(AuthError, match="Test auth failure"):
            client.get("/v2/")
    
    @patch('regshape.libs.transport.client.resolve_credentials')
    def test_generic_middleware_error_propagation(self, mock_resolve):
        """Test that other exceptions from middleware are propagated."""
        mock_resolve.return_value = (None, None)
        
        # Custom middleware that raises RuntimeError
        class FailingMiddleware(BaseMiddleware):
            def process_request(self, request):
                raise RuntimeError("Middleware failure")
        
        config = TransportConfig(
            registry="registry.example.com",
            middlewares=[FailingMiddleware()]
        )
        client = RegistryClient(config)
        
        with pytest.raises(RuntimeError, match="Middleware failure"):
            client.post("/v2/test/blobs/uploads/")
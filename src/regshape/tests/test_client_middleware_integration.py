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
        """Test middleware setup when no credentials are resolved.
        
        AuthMiddleware is always added (for anonymous token flows).
        """
        mock_resolve.return_value = (None, None)
        
        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)
        
        assert client._pipeline is not None
        assert client._pipeline.get_middleware_count() == 1  # AuthMiddleware always present
    
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
        """Test request with authentication middleware handling 401 challenge."""
        mock_resolve.return_value = ("user", "pass")
        
        # First response: 401 with WWW-Authenticate challenge
        auth_response = Mock(spec=requests.Response)
        auth_response.status_code = 401
        auth_response.headers = {"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="registry"'}
        auth_response.content = b"Unauthorized"
        auth_response.text = "Unauthorized"

        # Second response: success after auth
        success_response = Mock(spec=requests.Response)
        success_response.status_code = 200
        success_response.headers = {}
        success_response.content = b"success"
        success_response.text = "success"
        
        mock_http_request.side_effect = [auth_response, success_response]
        
        config = TransportConfig("registry.example.com") 
        client = RegistryClient(config)
        
        with patch("regshape.libs.transport.middleware.registryauth.authenticate") as mock_auth:
            mock_auth.return_value = "a-bearer-token"
            response = client.get("/v2/test/tags/list")
        
        assert response.status_code == 200
        assert mock_http_request.call_count == 2
        # Initial request should have no Authorization header
        initial_headers = mock_http_request.call_args_list[0][1]['headers']
        assert 'Authorization' not in initial_headers
        # Retry request should carry the negotiated Authorization header
        retry_headers = mock_http_request.call_args_list[1][1]['headers']
        assert retry_headers['Authorization'] == 'Bearer a-bearer-token'
    
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


class TestAuthV2Fallback:
    """Test the /v2/ fallback when 401 lacks WWW-Authenticate.

    Some registries (e.g. Azure Container Registry) only return the
    WWW-Authenticate challenge header on the ``/v2/`` base endpoint, not
    on resource-specific endpoints like ``/v2/<name>/tags/list``.  Both
    the middleware path and the legacy path must fall back to probing
    ``/v2/`` to obtain the challenge in this case.
    """

    # -- Middleware path ----------------------------------------------------

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_middleware_v2_fallback_authenticates_successfully(
        self, mock_http_request, mock_resolve,
    ):
        """Middleware: 401 without WWW-Authenticate falls back to /v2/ probe."""
        mock_resolve.return_value = ("user", "pass")

        # 1st call: original request → 401 *without* WWW-Authenticate
        no_challenge = Mock(spec=requests.Response)
        no_challenge.status_code = 401
        no_challenge.headers = {}
        no_challenge.content = b"Unauthorized"
        no_challenge.text = "Unauthorized"

        # 2nd call: /v2/ probe → 401 *with* WWW-Authenticate
        v2_challenge = Mock(spec=requests.Response)
        v2_challenge.status_code = 401
        v2_challenge.headers = {
            "WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="registry"',
        }
        v2_challenge.content = b"Unauthorized"
        v2_challenge.text = "Unauthorized"

        # 3rd call: retried original request with Authorization → 200
        success = Mock(spec=requests.Response)
        success.status_code = 200
        success.headers = {"Content-Type": "application/json"}
        success.content = b'{"tags":["v1"]}'
        success.text = '{"tags":["v1"]}'

        mock_http_request.side_effect = [no_challenge, v2_challenge, success]

        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)

        with patch(
            "regshape.libs.transport.middleware.registryauth.authenticate"
        ) as mock_auth:
            mock_auth.return_value = "a-bearer-token"
            response = client.get("/v2/test/tags/list")

        assert response.status_code == 200
        assert mock_http_request.call_count == 3

        # 1st: original request (no auth header)
        first_headers = mock_http_request.call_args_list[0][1]["headers"]
        assert "Authorization" not in first_headers

        # 2nd: /v2/ probe (no auth header)
        second_url = mock_http_request.call_args_list[1][1]["url"]
        assert second_url.endswith("/v2/")

        # 3rd: retried original request with auth
        third_headers = mock_http_request.call_args_list[2][1]["headers"]
        assert third_headers["Authorization"] == "Bearer a-bearer-token"
        third_url = mock_http_request.call_args_list[2][1]["url"]
        assert "/v2/test/tags/list" in third_url

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_middleware_v2_fallback_probe_also_missing_challenge(
        self, mock_http_request, mock_resolve,
    ):
        """Middleware: raise AuthError when /v2/ probe also lacks WWW-Authenticate."""
        mock_resolve.return_value = ("user", "pass")

        # 1st call: original → 401 without WWW-Authenticate
        no_challenge = Mock(spec=requests.Response)
        no_challenge.status_code = 401
        no_challenge.headers = {}
        no_challenge.content = b"Unauthorized"
        no_challenge.text = "Unauthorized"

        # 2nd call: /v2/ probe → also 401 without WWW-Authenticate
        v2_no_challenge = Mock(spec=requests.Response)
        v2_no_challenge.status_code = 401
        v2_no_challenge.headers = {}
        v2_no_challenge.content = b"Unauthorized"
        v2_no_challenge.text = "Unauthorized"

        mock_http_request.side_effect = [no_challenge, v2_no_challenge]

        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)

        with pytest.raises(AuthError, match="returned 401 without a WWW-Authenticate header"):
            client.get("/v2/test/tags/list")

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_middleware_v2_fallback_probe_returns_200(
        self, mock_http_request, mock_resolve,
    ):
        """Middleware: raise AuthError when /v2/ probe returns 200 (no challenge)."""
        mock_resolve.return_value = ("user", "pass")

        no_challenge = Mock(spec=requests.Response)
        no_challenge.status_code = 401
        no_challenge.headers = {}
        no_challenge.content = b"Unauthorized"
        no_challenge.text = "Unauthorized"

        # /v2/ probe succeeds without auth — no challenge to extract
        v2_ok = Mock(spec=requests.Response)
        v2_ok.status_code = 200
        v2_ok.headers = {}
        v2_ok.content = b"{}"
        v2_ok.text = "{}"

        mock_http_request.side_effect = [no_challenge, v2_ok]

        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)

        with pytest.raises(AuthError, match="returned 401 without a WWW-Authenticate header"):
            client.get("/v2/test/tags/list")

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_middleware_no_fallback_when_challenge_present(
        self, mock_http_request, mock_resolve,
    ):
        """Middleware: normal 401 with WWW-Authenticate does not probe /v2/."""
        mock_resolve.return_value = ("user", "pass")

        challenge_401 = Mock(spec=requests.Response)
        challenge_401.status_code = 401
        challenge_401.headers = {
            "WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="reg"',
        }
        challenge_401.content = b"Unauthorized"
        challenge_401.text = "Unauthorized"

        success = Mock(spec=requests.Response)
        success.status_code = 200
        success.headers = {}
        success.content = b"ok"
        success.text = "ok"

        mock_http_request.side_effect = [challenge_401, success]

        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)

        with patch(
            "regshape.libs.transport.middleware.registryauth.authenticate"
        ) as mock_auth:
            mock_auth.return_value = "token"
            response = client.get("/v2/test/tags/list")

        assert response.status_code == 200
        # Only 2 calls: the original + the retry — no /v2/ probe
        assert mock_http_request.call_count == 2

    # -- Legacy path --------------------------------------------------------

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_legacy_v2_fallback_authenticates_successfully(
        self, mock_http_request, mock_resolve,
    ):
        """Legacy: 401 without WWW-Authenticate falls back to /v2/ probe."""
        mock_resolve.return_value = ("user", "pass")

        no_challenge = _create_mock_response(401, {}, b"Unauthorized")

        v2_challenge = _create_mock_response(
            401,
            {"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="reg"'},
            b"Unauthorized",
        )

        success = _create_mock_response(200, {}, b'{"tags":["v1"]}')

        mock_http_request.side_effect = [no_challenge, v2_challenge, success]

        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)

        with patch(
            "regshape.libs.auth.registryauth.authenticate"
        ) as mock_auth, patch(
            "regshape.libs.transport.client._normalize_www_authenticate"
        ) as mock_normalize:
            mock_normalize.return_value = (
                'Bearer realm="https://auth.example.com/token",service="reg"',
                "Bearer",
            )
            mock_auth.return_value = "a-bearer-token"
            response = client.get("/v2/test/tags/list")

        assert response.status_code == 200
        assert mock_http_request.call_count == 3

        # The /v2/ probe should target the base URL
        v2_url = mock_http_request.call_args_list[1][0][0]
        assert v2_url.endswith("/v2/")

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_legacy_v2_fallback_probe_also_missing_challenge(
        self, mock_http_request, mock_resolve,
    ):
        """Legacy: raise AuthError when /v2/ probe also lacks WWW-Authenticate."""
        mock_resolve.return_value = ("user", "pass")

        no_challenge = _create_mock_response(401, {}, b"Unauthorized")
        v2_no_challenge = _create_mock_response(401, {}, b"Unauthorized")

        mock_http_request.side_effect = [no_challenge, v2_no_challenge]

        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)

        with pytest.raises(AuthError, match="returned 401 without a WWW-Authenticate header"):
            client.get("/v2/test/tags/list")

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_legacy_no_fallback_when_challenge_present(
        self, mock_http_request, mock_resolve,
    ):
        """Legacy: normal 401 with WWW-Authenticate does not probe /v2/."""
        mock_resolve.return_value = ("user", "pass")

        challenge = _create_mock_response(
            401,
            {"WWW-Authenticate": 'Basic realm="test"'},
            b"Unauthorized",
        )
        success = _create_mock_response(200, {}, b"ok")

        mock_http_request.side_effect = [challenge, success]

        config = TransportConfig("registry.example.com", enable_middleware=False)
        client = RegistryClient(config)

        with patch(
            "regshape.libs.auth.registryauth.authenticate"
        ) as mock_auth, patch(
            "regshape.libs.transport.client._normalize_www_authenticate"
        ) as mock_normalize:
            mock_normalize.return_value = ('Basic realm="test"', "Basic")
            mock_auth.return_value = "base64creds"
            response = client.get("/v2/test/tags/list")

        assert response.status_code == 200
        # Only 2 calls — no /v2/ probe
        assert mock_http_request.call_count == 2

    # -- Case-insensitive header matching -----------------------------------

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_middleware_title_case_www_authenticate_header(
        self, mock_http_request, mock_resolve,
    ):
        """Middleware: ACR-style 'Www-Authenticate' (title-case) is recognised."""
        mock_resolve.return_value = ("user", "pass")

        # ACR returns the header as "Www-Authenticate", not "WWW-Authenticate"
        challenge_401 = Mock(spec=requests.Response)
        challenge_401.status_code = 401
        challenge_401.headers = {
            "Www-Authenticate": 'Bearer realm="https://acr.example.com/oauth2/token",service="acr"',
        }
        challenge_401.content = b"Unauthorized"
        challenge_401.text = "Unauthorized"

        success = Mock(spec=requests.Response)
        success.status_code = 200
        success.headers = {}
        success.content = b'{"tags":["v1"]}'
        success.text = '{"tags":["v1"]}'

        mock_http_request.side_effect = [challenge_401, success]

        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)

        with patch(
            "regshape.libs.transport.middleware.registryauth.authenticate"
        ) as mock_auth:
            mock_auth.return_value = "a-bearer-token"
            response = client.get("/v2/test/tags/list")

        assert response.status_code == 200
        # Only 2 calls — challenge found on first response, no /v2/ probe
        assert mock_http_request.call_count == 2

    @patch('regshape.libs.transport.client.resolve_credentials')
    @patch('regshape.libs.transport.client.http_request')
    def test_middleware_v2_fallback_with_title_case_header(
        self, mock_http_request, mock_resolve,
    ):
        """Middleware: /v2/ fallback finds title-case 'Www-Authenticate'."""
        mock_resolve.return_value = ("user", "pass")

        # 1st: original → 401 with empty headers
        no_challenge = Mock(spec=requests.Response)
        no_challenge.status_code = 401
        no_challenge.headers = {}
        no_challenge.content = b"Unauthorized"
        no_challenge.text = "Unauthorized"

        # 2nd: /v2/ probe → 401 with ACR-style title-case header
        v2_challenge = Mock(spec=requests.Response)
        v2_challenge.status_code = 401
        v2_challenge.headers = {
            "Www-Authenticate": 'Bearer realm="https://acr.example.com/oauth2/token",service="acr"',
        }
        v2_challenge.content = b"Unauthorized"
        v2_challenge.text = "Unauthorized"

        # 3rd: retried original → 200
        success = Mock(spec=requests.Response)
        success.status_code = 200
        success.headers = {}
        success.content = b'{"tags":["v1"]}'
        success.text = '{"tags":["v1"]}'

        mock_http_request.side_effect = [no_challenge, v2_challenge, success]

        config = TransportConfig("registry.example.com")
        client = RegistryClient(config)

        with patch(
            "regshape.libs.transport.middleware.registryauth.authenticate"
        ) as mock_auth:
            mock_auth.return_value = "a-bearer-token"
            response = client.get("/v2/test/tags/list")

        assert response.status_code == 200
        assert mock_http_request.call_count == 3
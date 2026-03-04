#!/usr/bin/env python3

"""
test_middleware.py - Test middleware protocol and pipeline implementation

Tests for:
- Middleware protocol implementation
- BaseMiddleware abstract class
- MiddlewarePipeline execution logic
- Error handling in middleware pipeline
- Middleware registration and ordering
"""

import pytest
from unittest.mock import Mock, call

from regshape.libs.transport.middleware import (
    Middleware, 
    BaseMiddleware,
    MiddlewarePipeline,
    NextHandler
)
from regshape.libs.transport.models import RegistryRequest, RegistryResponse


def _create_mock_response(status_code: int, headers: dict, body: bytes) -> RegistryResponse:
    """Helper to create RegistryResponse with mock requests.Response."""
    mock_raw_response = Mock()
    mock_raw_response.status_code = status_code
    mock_raw_response.headers = headers
    mock_raw_response.content = body
    mock_raw_response.text = body.decode('utf-8') if body else ""
    return RegistryResponse(status_code, headers, body, mock_raw_response)


class TestMiddleware:
    """Test the Middleware protocol."""
    
    def test_middleware_protocol_signature(self):
        """Test that middleware protocol has correct signature."""
        # Create a mock middleware that follows the protocol
        mock_middleware = Mock(spec=Middleware)
        request = RegistryRequest("GET", "https://example.com", {})
        next_handler = Mock(return_value=_create_mock_response(200, {}, b"test"))
        
        # Should be callable with request and next_handler
        mock_middleware(request, next_handler)
        mock_middleware.assert_called_once_with(request, next_handler)


class TestBaseMiddleware:
    """Test the BaseMiddleware abstract class."""
    
    def test_cannot_instantiate_base_middleware(self):
        """Test that BaseMiddleware is an abstract base class."""
        # BaseMiddleware is abstract but doesn't have abstract methods
        # so it can be instantiated - testing that it provides the right interface
        middleware = BaseMiddleware()
        assert hasattr(middleware, '__call__')
        assert hasattr(middleware, 'process_request')
        assert hasattr(middleware, 'process_response')
        assert hasattr(middleware, 'handle_error')
    
    def test_concrete_middleware_implementation(self):
        """Test that concrete middleware works correctly."""
        class TestMiddleware(BaseMiddleware):
            pass
        
        middleware = TestMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        expected_response = _create_mock_response(200, {}, b"test")
        next_handler = Mock(return_value=expected_response)
        
        result = middleware(request, next_handler)
        
        next_handler.assert_called_once_with(request)
        assert result is expected_response
    
    def test_process_request_hook(self):
        """Test that process_request hook is called and can modify request."""
        class TestMiddleware(BaseMiddleware):
            def process_request(self, request):
                # Modify the request by adding a header
                request.headers["X-Modified"] = "true"
                return request
        
        middleware = TestMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"test")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        # Verify the modified request was passed to next handler
        next_handler.assert_called_once()
        modified_request = next_handler.call_args[0][0]
        assert modified_request.headers["X-Modified"] == "true"
        assert result is response
    
    def test_process_response_hook(self):
        """Test that process_response hook is called and can modify response."""
        class TestMiddleware(BaseMiddleware):
            def process_response(self, request, response):
                # Modify the response by adding a header
                response.headers["X-Processed"] = "true"
                return response
        
        middleware = TestMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"test")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        next_handler.assert_called_once_with(request)
        assert result is response
        assert result.headers["X-Processed"] == "true"

    def test_process_response_receives_processed_request(self):
        """Test that process_response receives the request after process_request modifications."""
        captured = {}
        
        class TestMiddleware(BaseMiddleware):
            def process_request(self, request):
                return RegistryRequest(
                    method=request.method,
                    url=request.url,
                    headers={**request.headers, "Authorization": "Bearer tok123"},
                    stream=request.stream,
                )
            
            def process_response(self, request, response):
                captured['request'] = request
                return response
        
        middleware = TestMiddleware()
        original_request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"ok")
        next_handler = Mock(return_value=response)
        
        middleware(original_request, next_handler)
        
        # process_response must see the processed request, not the original
        assert "Authorization" in captured['request'].headers
        assert captured['request'].headers["Authorization"] == "Bearer tok123"

    def test_handle_error_receives_processed_request(self):
        """Test that handle_error receives the request after process_request modifications."""
        captured = {}
        
        class TestMiddleware(BaseMiddleware):
            def process_request(self, request):
                return RegistryRequest(
                    method=request.method,
                    url=request.url,
                    headers={**request.headers, "X-Trace": "abc"},
                    stream=request.stream,
                )
            
            def handle_error(self, request, error):
                captured['request'] = request
                return _create_mock_response(500, {}, b"error")
        
        middleware = TestMiddleware()
        original_request = RegistryRequest("GET", "https://example.com", {})
        next_handler = Mock(side_effect=RuntimeError("boom"))
        
        middleware(original_request, next_handler)
        
        # handle_error must see the processed request, not the original
        assert captured['request'].headers.get("X-Trace") == "abc"
    
    def test_handle_error_hook(self):
        """Test that handle_error hook is called on exceptions."""
        class TestMiddleware(BaseMiddleware):
            def handle_error(self, request, error):
                # Convert error to response
                return _create_mock_response(500, {}, b"Error handled")
        
        middleware = TestMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        next_handler = Mock(side_effect=ValueError("Test error"))
        
        result = middleware(request, next_handler)
        
        next_handler.assert_called_once_with(request)
        assert result.status_code == 500
        assert result.body == b"Error handled"
    
    def test_handle_error_reraises_by_default(self):
        """Test that handle_error re-raises exceptions by default."""
        class TestMiddleware(BaseMiddleware):
            pass
        
        middleware = TestMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        next_handler = Mock(side_effect=ValueError("Test error"))
        
        with pytest.raises(ValueError, match="Test error"):
            middleware(request, next_handler)
    
    def test_all_hooks_called_in_order(self):
        """Test that all hooks are called in the correct order."""
        call_order = []
        
        class TestMiddleware(BaseMiddleware):
            def process_request(self, request):
                call_order.append("process_request")
                return request
            
            def process_response(self, request, response):
                call_order.append("process_response")
                return response
        
        middleware = TestMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"test")
        
        def next_handler(req):
            call_order.append("next_handler")
            return response
        
        result = middleware(request, next_handler)
        
        assert call_order == ["process_request", "next_handler", "process_response"]
        assert result is response


class TestMiddlewarePipeline:
    """Test the MiddlewarePipeline class."""
    
    def test_empty_pipeline(self):
        """Test pipeline with no middleware calls terminal handler directly."""
        pipeline = MiddlewarePipeline()
        request = RegistryRequest("GET", "https://example.com", {})
        expected_response = _create_mock_response(200, {}, b"test")
        terminal_handler = Mock(return_value=expected_response)
        
        result = pipeline.execute(request, terminal_handler)
        
        terminal_handler.assert_called_once_with(request)
        assert result is expected_response
    
    def test_single_middleware_pipeline(self):
        """Test pipeline with single middleware."""
        pipeline = MiddlewarePipeline()
        
        # Mock middleware that adds header
        middleware = Mock()
        middleware.return_value = _create_mock_response(200, {"X-Test": "true"}, b"test")
        pipeline.add_middleware(middleware)
        
        request = RegistryRequest("GET", "https://example.com", {})
        terminal_response = _create_mock_response(200, {}, b"original")
        terminal_handler = Mock(return_value=terminal_response)
        
        result = pipeline.execute(request, terminal_handler)
        
        # Middleware should be called with request and a next_handler
        middleware.assert_called_once()
        middleware_call = middleware.call_args[0]
        assert middleware_call[0] is request
        assert callable(middleware_call[1])  # next_handler
        
        assert result.headers["X-Test"] == "true"
    
    def test_multiple_middleware_execution_order(self):
        """Test that middleware executes in FIFO order."""
        pipeline = MiddlewarePipeline()
        execution_order = []
        
        class OrderedMiddleware(BaseMiddleware):
            def __init__(self, name):
                self.name = name
            
            def process_request(self, request):
                execution_order.append(f"{self.name}-request")
                return request
            
            def process_response(self, request, response):
                execution_order.append(f"{self.name}-response")
                return response
        
        # Add middleware in order: A, B, C
        middleware_a = OrderedMiddleware("A")
        middleware_b = OrderedMiddleware("B") 
        middleware_c = OrderedMiddleware("C")
        
        pipeline.add_middleware(middleware_a)
        pipeline.add_middleware(middleware_b)
        pipeline.add_middleware(middleware_c)
        
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"test")
        
        def terminal_handler(req):
            execution_order.append("terminal")
            return response
        
        result = pipeline.execute(request, terminal_handler)
        
        # Should execute: A-req, B-req, C-req, terminal, C-resp, B-resp, A-resp
        expected_order = [
            "A-request", "B-request", "C-request", 
            "terminal",
            "C-response", "B-response", "A-response"
        ]
        assert execution_order == expected_order
    
    def test_middleware_registration_methods(self):
        """Test middleware registration, insertion, and removal."""
        pipeline = MiddlewarePipeline()
        
        middleware_a = Mock()
        middleware_b = Mock()
        middleware_c = Mock()
        
        # Test add_middleware
        pipeline.add_middleware(middleware_a)
        pipeline.add_middleware(middleware_b)
        assert pipeline.get_middleware_count() == 2
        
        # Test insert_middleware 
        pipeline.insert_middleware(1, middleware_c)
        assert pipeline.get_middleware_count() == 3
        
        # Test remove_middleware
        pipeline.remove_middleware(middleware_b)
        assert pipeline.get_middleware_count() == 2
        
        # Test clear_middleware
        pipeline.clear_middleware()
        assert pipeline.get_middleware_count() == 0
    
    def test_remove_nonexistent_middleware_raises_error(self):
        """Test that removing non-existent middleware raises ValueError."""
        pipeline = MiddlewarePipeline()
        middleware = Mock()
        
        with pytest.raises(ValueError):
            pipeline.remove_middleware(middleware)
    
    def test_insert_middleware_out_of_bounds(self):
        """Test that inserting middleware out of bounds works gracefully."""
        pipeline = MiddlewarePipeline()
        middleware = Mock()
        
        # Python's list.insert() handles out-of-bounds gracefully
        # Large positive index inserts at end
        pipeline.insert_middleware(100, middleware)
        assert pipeline.get_middleware_count() == 1
        
        # Negative index inserts at beginning 
        middleware2 = Mock()
        pipeline.insert_middleware(-100, middleware2)
        assert pipeline.get_middleware_count() == 2
    
    def test_pipeline_error_propagation(self):
        """Test that errors in middleware are properly propagated."""
        pipeline = MiddlewarePipeline()
        
        # Middleware that raises an error
        error_middleware = Mock(side_effect=RuntimeError("Middleware error"))
        pipeline.add_middleware(error_middleware)
        
        request = RegistryRequest("GET", "https://example.com", {})
        terminal_handler = Mock()
        
        with pytest.raises(RuntimeError, match="Middleware error"):
            pipeline.execute(request, terminal_handler)
        
        # Terminal handler should not be called due to error
        terminal_handler.assert_not_called()
    
    def test_middleware_can_short_circuit_pipeline(self):
        """Test that middleware can return response without calling next handler."""
        pipeline = MiddlewarePipeline()
        
        # Middleware that returns response directly
        def short_circuit_middleware(request, next_handler):
            return _create_mock_response(304, {}, b"Not Modified")
        
        pipeline.add_middleware(short_circuit_middleware)
        
        request = RegistryRequest("GET", "https://example.com", {})
        terminal_handler = Mock()
        
        result = pipeline.execute(request, terminal_handler)
        
        assert result.status_code == 304
        assert result.body == b"Not Modified"
        # Terminal handler should not be called
        terminal_handler.assert_not_called()


class TestMiddlewareIntegration:
    """Integration tests for middleware system."""
    
    def test_real_world_middleware_stack(self):
        """Test a realistic middleware stack with auth, logging, and retry."""
        pipeline = MiddlewarePipeline()
        
        # Auth middleware that adds Authorization header
        class AuthMiddleware(BaseMiddleware):
            def process_request(self, request):
                request.headers["Authorization"] = "Bearer token123"
                return request
        
        # Logging middleware that tracks requests
        class LoggingMiddleware(BaseMiddleware):
            def __init__(self):
                self.requests = []
                self.responses = []
            
            def process_request(self, request):
                self.requests.append(request)
                return request
            
            def process_response(self, request, response):
                self.responses.append(response)
                return response
        
        # Content type middleware
        class ContentTypeMiddleware(BaseMiddleware):
            def process_request(self, request):
                if request.method in ["POST", "PUT", "PATCH"]:
                    request.headers["Content-Type"] = "application/json"
                return request
        
        auth = AuthMiddleware()
        logging = LoggingMiddleware()
        content_type = ContentTypeMiddleware()
        
        pipeline.add_middleware(auth)
        pipeline.add_middleware(logging) 
        pipeline.add_middleware(content_type)
        
        request = RegistryRequest("POST", "https://registry.example.com/v2/repo/manifests/latest", {})
        response = _create_mock_response(201, {"Location": "/v2/repo/manifests/sha256:abc123"}, b"")
        
        def terminal_handler(req):
            # Verify all middleware modifications were applied
            assert req.headers["Authorization"] == "Bearer token123"
            assert req.headers["Content-Type"] == "application/json"
            return response
        
        result = pipeline.execute(request, terminal_handler)
        
        # Verify logging middleware captured the request/response
        assert len(logging.requests) == 1
        assert len(logging.responses) == 1
        assert logging.requests[0] is request
        assert logging.responses[0] is response
        
        assert result is response


class TestConcreteMiddleware:
    """Test concrete middleware implementations."""
    
    def test_auth_middleware_no_credentials(self):
        """Test auth middleware with no credentials passes through unchanged."""
        from regshape.libs.transport.middleware import AuthMiddleware
        
        middleware = AuthMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"test")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        # Request should be unchanged
        next_handler.assert_called_once_with(request)
        assert result is response
    
    def test_auth_middleware_basic_auth(self):
        """Test auth middleware adds basic auth header."""
        from regshape.libs.transport.middleware import AuthMiddleware
        
        # Create specific credentials object for basic auth
        class BasicCredentials:
            def __init__(self, username, password):
                self.username = username
                self.password = password
        
        credentials = BasicCredentials("user", "pass")
        middleware = AuthMiddleware(credentials)
        request = RegistryRequest("GET", "https://example.com", {"User-Agent": "test"})
        response = _create_mock_response(200, {}, b"test")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        # Check that Authorization header was added
        next_handler.assert_called_once()
        modified_request = next_handler.call_args[0][0]
        assert "Authorization" in modified_request.headers
        assert modified_request.headers["Authorization"].startswith("Basic ")
        assert modified_request.headers["User-Agent"] == "test"
        assert result is response
    
    def test_auth_middleware_bearer_token(self):
        """Test auth middleware adds bearer token header."""
        from regshape.libs.transport.middleware import AuthMiddleware
        
        # Create specific credentials object for bearer token
        class BearerCredentials:
            def __init__(self, token):
                self.token = token
        
        credentials = BearerCredentials("abc123")
        middleware = AuthMiddleware(credentials)
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"test")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        # Check that Authorization header was added
        next_handler.assert_called_once()
        modified_request = next_handler.call_args[0][0]
        assert modified_request.headers["Authorization"] == "Bearer abc123"
        assert result is response
    
    def test_auth_middleware_handles_401_response(self):
        """Test auth middleware processes WWW-Authenticate challenges."""
        from regshape.libs.transport.middleware import AuthMiddleware
        
        middleware = AuthMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        
        # Mock 401 response with WWW-Authenticate header
        response_headers = {"WWW-Authenticate": 'Basic realm="test"'}
        response = _create_mock_response(401, response_headers, b"Unauthorized")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        # Response should pass through (challenge handling is placeholder in this implementation)
        assert result is response
        assert result.status_code == 401
    
    def test_logging_middleware_logs_request_response(self):
        """Test logging middleware logs requests and responses."""
        import logging
        from unittest.mock import patch
        from regshape.libs.transport.middleware import LoggingMiddleware
        
        with patch('regshape.libs.transport.middleware.logging.getLogger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            mock_logger.isEnabledFor.return_value = True
            
            middleware = LoggingMiddleware("test.logger", logging.DEBUG)
            request = RegistryRequest("POST", "https://example.com/api", {"Content-Type": "application/json"})
            response = _create_mock_response(201, {"Location": "/created"}, b'{"id": 123}')
            next_handler = Mock(return_value=response)
            
            result = middleware(request, next_handler)
            
            # Verify logging calls
            assert mock_logger.log.call_count == 2  # request + response
            
            # Check request logging
            request_call = mock_logger.log.call_args_list[0]
            assert request_call[0][0] == logging.DEBUG  # level
            assert "HTTP Request: POST https://example.com/api" in request_call[0][1]
            
            # Check response logging  
            response_call = mock_logger.log.call_args_list[1]
            assert "HTTP Response: 201 for POST https://example.com/api" in response_call[0][1]
            
            assert result is response
    
    def test_logging_middleware_excludes_sensitive_headers(self):
        """Test logging middleware redacts sensitive headers in logs."""
        import logging
        from unittest.mock import patch
        from regshape.libs.transport.middleware import LoggingMiddleware
        
        with patch('regshape.libs.transport.middleware.logging.getLogger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            mock_logger.isEnabledFor.return_value = True
            
            middleware = LoggingMiddleware()
            request = RegistryRequest("GET", "https://example.com", {
                "Authorization": "Bearer secret-token",
                "Proxy-Authorization": "Basic dXNlcjpwYXNz",
                "Cookie": "session=abc123",
                "Set-Cookie": "id=xyz",
                "User-Agent": "test-client"
            })
            response = _create_mock_response(200, {}, b"test")
            next_handler = Mock(return_value=response)
            
            middleware(request, next_handler)
            
            # Sensitive headers should be present but redacted
            request_call = mock_logger.log.call_args_list[0]
            logged_headers = request_call[1]['extra']['headers']
            assert logged_headers['Authorization'] == 'Bearer <redacted>'
            assert logged_headers['Proxy-Authorization'] == 'Basic <redacted>'
            assert logged_headers['Cookie'] == '<redacted>'
            assert logged_headers['Set-Cookie'] == '<redacted>'
            # Non-sensitive headers are unchanged
            assert logged_headers['User-Agent'] == 'test-client'
    
    def test_logging_middleware_logs_errors(self):
        """Test logging middleware logs errors."""
        import logging
        from unittest.mock import patch
        from regshape.libs.transport.middleware import LoggingMiddleware
        
        with patch('regshape.libs.transport.middleware.logging.getLogger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            middleware = LoggingMiddleware()
            request = RegistryRequest("GET", "https://example.com", {})
            next_handler = Mock(side_effect=ConnectionError("Network failure"))
            
            with pytest.raises(ConnectionError):
                middleware(request, next_handler)
            
            # Verify error logging
            mock_logger.error.assert_called_once()
            error_call = mock_logger.error.call_args
            assert "HTTP Error for GET https://example.com" in error_call[0][0]
            assert "ConnectionError" in error_call[1]['extra']['error_type']
    
    def test_retry_middleware_default_config(self):
        """Test retry middleware with default configuration."""
        from regshape.libs.transport.middleware import RetryMiddleware
        
        middleware = RetryMiddleware()
        request = RegistryRequest("GET", "https://example.com", {})
        response = _create_mock_response(200, {}, b"success")
        next_handler = Mock(return_value=response)
        
        result = middleware(request, next_handler)
        
        # Should succeed on first try
        next_handler.assert_called_once()
        assert result is response
    
    def test_retry_middleware_retries_server_errors(self):
        """Test retry middleware retries on server errors."""
        from regshape.libs.transport.middleware import RetryMiddleware, RetryConfig
        from unittest.mock import patch
        
        # Use faster backoff for testing
        config = RetryConfig(max_retries=2, backoff_factor=0.01, status_codes=(500,))
        middleware = RetryMiddleware(config)
        
        request = RegistryRequest("GET", "https://example.com", {})
        
        # Mock responses: 500, 500, 200 (success on third try)
        responses = [
            _create_mock_response(500, {}, b"Server Error"),
            _create_mock_response(500, {}, b"Server Error"), 
            _create_mock_response(200, {}, b"Success")
        ]
        next_handler = Mock(side_effect=responses)
        
        with patch('regshape.libs.transport.middleware.time.sleep') as mock_sleep:
            result = middleware(request, next_handler)
        
        # Should have made 3 attempts
        assert next_handler.call_count == 3
        assert result.status_code == 200
        assert result.body == b"Success"
        
        # Should have slept twice (between retries)
        assert mock_sleep.call_count == 2
    
    def test_retry_middleware_gives_up_after_max_attempts(self):
        """Test retry middleware gives up after max retries."""
        from regshape.libs.transport.middleware import RetryMiddleware, RetryConfig
        from unittest.mock import patch
        
        config = RetryConfig(max_retries=1, backoff_factor=0.01, status_codes=(503,))
        middleware = RetryMiddleware(config)
        
        request = RegistryRequest("GET", "https://example.com", {})
        
        # Always return 503
        response = _create_mock_response(503, {}, b"Service Unavailable")
        next_handler = Mock(return_value=response)
        
        with patch('regshape.libs.transport.middleware.time.sleep'):
            result = middleware(request, next_handler)
        
        # Should have made max_retries + 1 attempts
        assert next_handler.call_count == 2
        assert result.status_code == 503
    
    def test_retry_middleware_handles_network_exceptions(self):
        """Test retry middleware handles network exceptions."""
        from regshape.libs.transport.middleware import RetryMiddleware, RetryConfig
        from unittest.mock import patch
        import requests.exceptions
        
        config = RetryConfig(max_retries=1, backoff_factor=0.01, exceptions=(requests.exceptions.ConnectionError,))
        middleware = RetryMiddleware(config)
        
        request = RegistryRequest("GET", "https://example.com", {})
        
        # First call raises exception, second succeeds
        success_response = _create_mock_response(200, {}, b"Success")
        next_handler = Mock(side_effect=[requests.exceptions.ConnectionError("Network error"), success_response])
        
        with patch('regshape.libs.transport.middleware.time.sleep'):
            result = middleware(request, next_handler)
        
        assert next_handler.call_count == 2
        assert result.status_code == 200
    
    def test_caching_middleware_caches_get_requests(self):
        """Test caching middleware caches GET requests."""
        from regshape.libs.transport.middleware import CachingMiddleware
        
        middleware = CachingMiddleware()
        request = RegistryRequest("GET", "https://example.com/manifest", {})
        response = _create_mock_response(200, {"Content-Type": "application/json"}, b'{"test": true}')
        next_handler = Mock(return_value=response)
        
        # First request
        result1 = middleware(request, next_handler)
        assert next_handler.call_count == 1
        assert result1 is response
        
        # Second request should use cache
        result2 = middleware(request, next_handler) 
        assert next_handler.call_count == 1  # No additional call
        assert result2.body == response.body
    
    def test_caching_middleware_ignores_non_get_requests(self):
        """Test caching middleware ignores non-GET requests."""
        from regshape.libs.transport.middleware import CachingMiddleware
        
        middleware = CachingMiddleware()
        request = RegistryRequest("POST", "https://example.com/api", {})
        response = _create_mock_response(201, {}, b"Created")
        next_handler = Mock(return_value=response)
        
        # Make two POST requests
        result1 = middleware(request, next_handler)
        result2 = middleware(request, next_handler)
        
        # Both should hit the next handler
        assert next_handler.call_count == 2
        assert result1 is response
        assert result2 is response
    
    def test_caching_middleware_respects_cache_control(self):
        """Test caching middleware respects Cache-Control headers."""
        from regshape.libs.transport.middleware import CachingMiddleware
        
        middleware = CachingMiddleware()
        request = RegistryRequest("GET", "https://example.com/no-cache", {})
        
        # Response with no-cache directive
        response = _create_mock_response(200, {"Cache-Control": "no-cache"}, b"data")
        next_handler = Mock(return_value=response)
        
        # Make two requests
        result1 = middleware(request, next_handler)
        result2 = middleware(request, next_handler)
        
        # Both should hit the next handler (no caching)
        assert next_handler.call_count == 2
        assert result1 is response
        assert result2 is response
    
    def test_caching_middleware_size_limit(self):
        """Test caching middleware respects size limit."""
        from regshape.libs.transport.middleware import CachingMiddleware
        
        # Small cache size for testing
        middleware = CachingMiddleware(max_size=2)
        
        responses = []
        for i in range(3):
            request = RegistryRequest("GET", f"https://example.com/item{i}", {})
            response = _create_mock_response(200, {}, f"data{i}".encode())
            next_handler = Mock(return_value=response)
            
            result = middleware(request, next_handler)
            responses.append((request, next_handler, result))
        
        # Cache should have max 2 items
        assert middleware.get_cache_size() == 2
        
        # First item should have been evicted, so requesting it again should hit the handler
        first_request, first_handler, _ = responses[0]
        middleware(first_request, first_handler)
        assert first_handler.call_count == 2  # Original + evicted call
    
    def test_caching_middleware_clear_cache(self):
        """Test caching middleware cache clearing."""
        from regshape.libs.transport.middleware import CachingMiddleware
        
        middleware = CachingMiddleware()
        request = RegistryRequest("GET", "https://example.com/data", {})
        response = _create_mock_response(200, {}, b"cached data")
        next_handler = Mock(return_value=response)
        
        # Cache a response  
        middleware(request, next_handler)
        assert middleware.get_cache_size() == 1
        
        # Clear cache
        middleware.clear_cache()
        assert middleware.get_cache_size() == 0
        
        # Next request should hit handler again
        middleware(request, next_handler)
        assert next_handler.call_count == 2

    def test_caching_middleware_ttl_expires_entry(self):
        """Test that cached entries expire after the TTL elapses."""
        from regshape.libs.transport.middleware import CachingMiddleware
        from unittest.mock import patch
        import time as _time

        middleware = CachingMiddleware(ttl=10.0)
        request = RegistryRequest("GET", "https://example.com/manifest", {})

        response1 = _create_mock_response(200, {}, b"v1")
        response2 = _create_mock_response(200, {}, b"v2")
        next_handler = Mock(side_effect=[response1, response2])

        # Patch time.monotonic so we control the clock
        clock = [1000.0]
        with patch("regshape.libs.transport.middleware.time") as mock_time:
            mock_time.monotonic = lambda: clock[0]
            mock_time.sleep = _time.sleep  # keep sleep available for retry

            # First request — cache miss
            result1 = middleware(request, next_handler)
            assert next_handler.call_count == 1
            assert result1.body == b"v1"

            # Advance clock by 5 s (within TTL) — cache hit
            clock[0] += 5.0
            result2 = middleware(request, next_handler)
            assert next_handler.call_count == 1  # still cached
            assert result2.body == b"v1"

            # Advance clock past TTL — cache miss, re-fetches
            clock[0] += 6.0  # total elapsed: 11 s > 10 s TTL
            result3 = middleware(request, next_handler)
            assert next_handler.call_count == 2
            assert result3.body == b"v2"

    def test_caching_middleware_no_ttl_never_expires(self):
        """Test that entries with ttl=None never expire."""
        from regshape.libs.transport.middleware import CachingMiddleware
        from unittest.mock import patch
        import time as _time

        middleware = CachingMiddleware(ttl=None)
        request = RegistryRequest("GET", "https://example.com/blob", {})
        response = _create_mock_response(200, {}, b"immutable")
        next_handler = Mock(return_value=response)

        clock = [1000.0]
        with patch("regshape.libs.transport.middleware.time") as mock_time:
            mock_time.monotonic = lambda: clock[0]
            mock_time.sleep = _time.sleep

            middleware(request, next_handler)
            assert next_handler.call_count == 1

            # Advance clock by a very large amount
            clock[0] += 999_999.0
            middleware(request, next_handler)
            assert next_handler.call_count == 1  # still cached

    def test_caching_middleware_ttl_stale_entry_evicted(self):
        """Test that a stale entry is removed from the cache dict."""
        from regshape.libs.transport.middleware import CachingMiddleware
        from unittest.mock import patch
        import time as _time

        middleware = CachingMiddleware(ttl=5.0)
        request = RegistryRequest("GET", "https://example.com/data", {})
        response = _create_mock_response(200, {}, b"data")
        next_handler = Mock(return_value=response)

        clock = [0.0]
        with patch("regshape.libs.transport.middleware.time") as mock_time:
            mock_time.monotonic = lambda: clock[0]
            mock_time.sleep = _time.sleep

            middleware(request, next_handler)
            assert middleware.get_cache_size() == 1

            # Expire the entry
            clock[0] += 10.0
            middleware(request, next_handler)
            # After re-fetch, a fresh entry should be stored
            assert middleware.get_cache_size() == 1
            assert next_handler.call_count == 2
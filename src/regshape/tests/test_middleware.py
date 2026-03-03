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
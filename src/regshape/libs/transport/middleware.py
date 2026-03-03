#!/usr/bin/env python3

"""
:mod:`regshape.libs.transport.middleware` - Transport Middleware Protocol  
=====================================================================

.. module:: regshape.libs.transport.middleware
   :platform: Unix, Windows
   :synopsis: Defines the middleware protocol for the transport pipeline.
              Middleware components can inspect and modify both requests
              and responses as they flow through the pipeline.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from abc import ABC, abstractmethod
from typing import Protocol, Callable

from regshape.libs.transport.models import RegistryRequest, RegistryResponse


class Middleware(Protocol):
    """Protocol for transport layer middleware.
    
    Middleware components receive a RegistryRequest and a next_handler 
    callable, and must return a RegistryResponse. This allows each 
    middleware to:
    
    1. Inspect/modify the request before calling next_handler 
    2. Call next_handler to continue the pipeline
    3. Inspect/modify the response before returning it
    
    Example middleware implementation:
    
        class LoggingMiddleware:
            def __call__(self, request, next_handler):
                print(f"Request: {request.method} {request.url}")
                response = next_handler(request)  
                print(f"Response: {response.status_code}")
                return response
    """
    
    def __call__(
        self, 
        request: RegistryRequest,
        next_handler: Callable[[RegistryRequest], RegistryResponse]
    ) -> RegistryResponse:
        """Process a request through this middleware.
        
        :param request: The outgoing HTTP request
        :param next_handler: Callable to continue the middleware pipeline
        :returns: The HTTP response (may be modified by this middleware)
        """
        ...


class BaseMiddleware(ABC):
    """Abstract base class for middleware implementations.
    
    Provides a structured approach to middleware development with 
    separate hooks for request preprocessing, response postprocessing,
    and error handling.
    """
    
    def __call__(
        self,
        request: RegistryRequest, 
        next_handler: Callable[[RegistryRequest], RegistryResponse]
    ) -> RegistryResponse:
        """Main middleware entry point."""
        try:
            # Allow subclasses to modify the request
            processed_request = self.process_request(request)
            
            # Continue the pipeline
            response = next_handler(processed_request)
            
            # Allow subclasses to modify the response  
            processed_response = self.process_response(request, response)
            
            return processed_response
            
        except Exception as exc:
            # Allow subclasses to handle errors
            return self.handle_error(request, exc)
    
    def process_request(self, request: RegistryRequest) -> RegistryRequest:
        """Hook for request preprocessing.
        
        Subclasses can override this to inspect/modify the request
        before it continues through the pipeline.
        
        :param request: The incoming request
        :returns: The processed request (may be modified)
        """
        return request
    
    def process_response(
        self, 
        request: RegistryRequest, 
        response: RegistryResponse
    ) -> RegistryResponse:
        """Hook for response postprocessing.
        
        Subclasses can override this to inspect/modify the response
        after it returns from the pipeline.
        
        :param request: The original request 
        :param response: The response from downstream middleware
        :returns: The processed response (may be modified)
        """
        return response
    
    def handle_error(
        self, 
        request: RegistryRequest, 
        error: Exception
    ) -> RegistryResponse:
        """Hook for error handling.
        
        Subclasses can override this to handle errors that occur
        in downstream middleware. Default behavior is to re-raise.
        
        :param request: The request that caused the error
        :param error: The exception that occurred
        :returns: A response (if error can be recovered)
        :raises: Re-raises the error if not handled
        """
        raise error


# Type alias for the next handler function signature
NextHandler = Callable[[RegistryRequest], RegistryResponse]


class MiddlewarePipeline:
    """Manages and executes a pipeline of middleware components.
    
    The pipeline processes requests through middleware in FIFO order,
    with each middleware having the opportunity to modify the request
    before passing it to the next middleware, and modify the response
    on the way back.
    
    Example usage:
    
        pipeline = MiddlewarePipeline()
        pipeline.add_middleware(AuthMiddleware())
        pipeline.add_middleware(LoggingMiddleware())
        
        # Execute pipeline with a terminal handler
        response = pipeline.execute(request, terminal_handler)
    """
    
    def __init__(self):
        """Initialize empty middleware pipeline."""
        self._middleware: list[Middleware] = []
    
    def add_middleware(self, middleware: Middleware) -> None:
        """Add middleware to the end of the pipeline.
        
        Middleware is executed in the order it's added (FIFO).
        
        :param middleware: Middleware component to add
        """
        self._middleware.append(middleware)
    
    def insert_middleware(self, index: int, middleware: Middleware) -> None:
        """Insert middleware at a specific position in the pipeline.
        
        :param index: Position to insert at (0 = first to execute)
        :param middleware: Middleware component to insert
        :raises IndexError: If index is out of bounds
        """
        self._middleware.insert(index, middleware)
    
    def remove_middleware(self, middleware: Middleware) -> None:
        """Remove middleware from the pipeline.
        
        :param middleware: Middleware component to remove
        :raises ValueError: If middleware is not in pipeline
        """
        self._middleware.remove(middleware)
    
    def clear_middleware(self) -> None:
        """Remove all middleware from the pipeline."""
        self._middleware.clear()
    
    def get_middleware_count(self) -> int:
        """Get the number of middleware components in the pipeline."""
        return len(self._middleware)
    
    def execute(
        self,
        request: RegistryRequest,
        terminal_handler: Callable[[RegistryRequest], RegistryResponse]
    ) -> RegistryResponse:
        """Execute the request through the middleware pipeline.
        
        The pipeline processes middleware in FIFO order, with each
        middleware having the opportunity to modify the request/response.
        
        :param request: The initial request to process
        :param terminal_handler: Final handler that produces the response
        :returns: The response after processing through all middleware
        """
        if not self._middleware:
            # No middleware, call terminal handler directly
            return terminal_handler(request)
        
        # Build the handler chain by wrapping each middleware
        handler = terminal_handler
        
        # Work backwards through middleware list to build proper chain
        for middleware in reversed(self._middleware):
            handler = self._create_middleware_handler(middleware, handler)
        
        # Execute the chain starting with the first middleware
        return handler(request)
    
    def _create_middleware_handler(
        self,
        middleware: Middleware,
        next_handler: NextHandler
    ) -> NextHandler:
        """Create a handler that wraps middleware with the next handler.
        
        :param middleware: The middleware to wrap
        :param next_handler: The handler to call next
        :returns: A new handler that executes middleware then next_handler
        """
        def wrapped_handler(request: RegistryRequest) -> RegistryResponse:
            return middleware(request, next_handler)
        
        return wrapped_handler


# Concrete Middleware Implementations
# ===================================


import time
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class RetryConfig:
    """Configuration for retry middleware."""
    max_retries: int = 3
    backoff_factor: float = 1.0
    status_codes: tuple = (500, 502, 503, 504)
    exceptions: tuple = (ConnectionError, TimeoutError)


class AuthMiddleware(BaseMiddleware):
    """Middleware that adds authentication headers to requests.
    
    Supports both basic auth and bearer token authentication.
    Can handle WWW-Authenticate challenges by updating credentials.
    """
    
    def __init__(self, credentials: Optional[Any] = None):
        """Initialize auth middleware.
        
        :param credentials: BasicCredentials, BearerCredentials, or None for no auth
        """
        self.credentials = credentials
    
    def process_request(self, request: RegistryRequest) -> RegistryRequest:
        """Add authentication headers to the request."""
        if self.credentials is None:
            return request
        
        # Create a copy with auth headers
        auth_headers = dict(request.headers)
        
        if hasattr(self.credentials, 'token'):
            # Bearer token (check token first, higher priority)
            auth_headers["Authorization"] = f"Bearer {self.credentials.token}"
        elif hasattr(self.credentials, 'username') and hasattr(self.credentials, 'password'):
            # Basic auth
            import base64
            auth_string = f"{self.credentials.username}:{self.credentials.password}"
            encoded = base64.b64encode(auth_string.encode()).decode()
            auth_headers["Authorization"] = f"Basic {encoded}"
        
        return RegistryRequest(
            method=request.method,
            url=request.url,
            headers=auth_headers,
            body=request.body,
            stream=request.stream,
            params=request.params,
            timeout=request.timeout
        )
    
    def process_response(self, request: RegistryRequest, response: RegistryResponse) -> RegistryResponse:
        """Handle WWW-Authenticate challenges."""
        if response.status_code == 401 and "WWW-Authenticate" in response.headers:
            # In a real implementation, this would parse the challenge
            # and potentially update credentials or trigger re-authentication
            pass
        
        return response


class LoggingMiddleware(BaseMiddleware):
    """Middleware that logs HTTP requests and responses.
    
    Supports configurable log levels and detailed request/response logging.
    """
    
    def __init__(self, logger_name: str = "regshape.transport", level: int = logging.INFO):
        """Initialize logging middleware.
        
        :param logger_name: Name of the logger to use
        :param level: Logging level (DEBUG, INFO, WARNING, ERROR)
        """
        self.logger = logging.getLogger(logger_name)
        self.level = level
    
    def process_request(self, request: RegistryRequest) -> RegistryRequest:
        """Log the outgoing request."""
        if self.logger.isEnabledFor(self.level):
            # Log request details (avoid logging sensitive headers)
            safe_headers = {k: v for k, v in request.headers.items() 
                          if k.lower() not in ('authorization', 'cookie')}
            
            self.logger.log(
                self.level,
                f"HTTP Request: {request.method} {request.url}",
                extra={
                    'method': request.method,
                    'url': request.url,  
                    'headers': safe_headers,
                    'has_body': request.body is not None
                }
            )
        
        return request
    
    def process_response(self, request: RegistryRequest, response: RegistryResponse) -> RegistryResponse:
        """Log the incoming response."""
        if self.logger.isEnabledFor(self.level):
            self.logger.log(
                self.level,
                f"HTTP Response: {response.status_code} for {request.method} {request.url}",
                extra={
                    'status_code': response.status_code,
                    'method': request.method,
                    'url': request.url,
                    'content_length': len(response.body)
                }
            )
        
        return response
    
    def handle_error(self, request: RegistryRequest, error: Exception) -> RegistryResponse:
        """Log errors that occur during request processing."""
        self.logger.error(
            f"HTTP Error for {request.method} {request.url}: {error}",
            extra={
                'method': request.method,
                'url': request.url,
                'error_type': type(error).__name__,
                'error_message': str(error)
            },
            exc_info=True
        )
        
        # Re-raise the error after logging
        raise error


class RetryMiddleware(BaseMiddleware):
    """Middleware that retries failed requests with exponential backoff.
    
    Retries requests that fail due to network errors or specific HTTP status codes.
    Uses exponential backoff to avoid overwhelming the server.
    """
    
    def __init__(self, config: Optional[RetryConfig] = None):
        """Initialize retry middleware.
        
        :param config: RetryConfig instance, or None for default settings
        """
        self.config = config or RetryConfig()
    
    def __call__(self, request: RegistryRequest, next_handler: NextHandler) -> RegistryResponse:
        """Execute request with retry logic."""
        last_exception = None
        
        for attempt in range(self.config.max_retries + 1):
            try:
                # Process request through parent hooks
                processed_request = self.process_request(request)
                
                # Execute the request
                response = next_handler(processed_request)
                
                # Check if we should retry based on status code
                if response.status_code in self.config.status_codes and attempt < self.config.max_retries:
                    self._wait_backoff(attempt)
                    continue
                
                # Process response and return
                return self.process_response(request, response)
                
            except self.config.exceptions as exc:
                last_exception = exc
                
                if attempt < self.config.max_retries:
                    self._wait_backoff(attempt)
                    continue
                else:
                    return self.handle_error(request, exc)
        
        # This shouldn't be reached, but handle it gracefully
        if last_exception:
            return self.handle_error(request, last_exception)
        
        # Fallback - should never happen
        raise RuntimeError("Retry logic reached unexpected state")
    
    def _wait_backoff(self, attempt: int) -> None:
        """Wait for exponential backoff delay."""
        delay = self.config.backoff_factor * (2 ** attempt)
        time.sleep(delay)


class CachingMiddleware(BaseMiddleware):
    """Middleware that caches GET responses for immutable content.
    
    Caches responses based on URL and Cache-Control headers.
    Suitable for registry manifests and blobs that are immutable.
    """
    
    def __init__(self, max_size: int = 1000):
        """Initialize caching middleware.
        
        :param max_size: Maximum number of cached responses
        """
        self.cache: Dict[str, RegistryResponse] = {}
        self.max_size = max_size
    
    def __call__(self, request: RegistryRequest, next_handler: NextHandler) -> RegistryResponse:
        """Execute request with caching logic."""
        # Only cache GET requests
        if request.method != "GET":
            return super().__call__(request, next_handler)
        
        cache_key = self._get_cache_key(request)
        
        # Check cache first
        if cache_key in self.cache:
            cached_response = self.cache[cache_key]
            # In a real implementation, check expiration here
            return cached_response
        
        # Execute request
        processed_request = self.process_request(request)
        response = next_handler(processed_request)
        processed_response = self.process_response(request, response)
        
        # Cache successful responses for immutable content
        if self._should_cache(processed_response):
            self._add_to_cache(cache_key, processed_response)
        
        return processed_response
    
    def _get_cache_key(self, request: RegistryRequest) -> str:
        """Generate cache key from request."""
        return f"{request.method}:{request.url}"
    
    def _should_cache(self, response: RegistryResponse) -> bool:
        """Determine if response should be cached."""
        # Cache successful responses
        if not response.ok:
            return False
        
        # Check Cache-Control header
        cache_control = response.headers.get("Cache-Control", "")
        if "no-cache" in cache_control or "no-store" in cache_control:
            return False
        
        # Cache registry content (manifests, blobs) which are immutable
        return True
    
    def _add_to_cache(self, key: str, response: RegistryResponse) -> None:
        """Add response to cache with size limit."""
        if len(self.cache) >= self.max_size:
            # Simple LRU: remove oldest entry  
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
        
        self.cache[key] = response
    
    def clear_cache(self) -> None:
        """Clear all cached responses."""
        self.cache.clear()
    
    def get_cache_size(self) -> int:
        """Get current number of cached responses."""
        return len(self.cache)
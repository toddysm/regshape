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
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

from abc import ABC
from typing import Protocol, Callable

from regshape.libs.auth import registryauth
from regshape.libs.errors import AuthError
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
        processed_request = request
        try:
            # Allow subclasses to modify the request
            processed_request = self.process_request(request)
            
            # Continue the pipeline
            response = next_handler(processed_request)
            
            # Allow subclasses to modify the response  
            processed_response = self.process_response(processed_request, response)
            
            return processed_response
            
        except Exception as exc:
            # Allow subclasses to handle errors
            return self.handle_error(processed_request, exc)
    
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
        
        :param request: The processed request (after :meth:`process_request`)
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
        
        :param request: The processed request (after :meth:`process_request`)
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
        
        Out-of-range indexes are clamped: a large positive index appends
        to the end, and a large negative index prepends to the beginning.
        
        :param index: Position to insert at (0 = first to execute)
        :param middleware: Middleware component to insert
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
from dataclasses import dataclass, field

import requests.exceptions


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_www_authenticate(www_auth: str) -> tuple[str, str]:
    """Normalize a WWW-Authenticate header value.

    - Capitalises basic / bearer scheme names so that
      registryauth.authenticate receives the expected casing.
    - Strips whitespace from each comma-separated parameter to prevent parse
      failures when a registry emits
      Bearer realm="...", service="..." (space after the comma).

    :param www_auth: Raw WWW-Authenticate header value.
    :returns: (normalized_www_auth, normalized_scheme) tuple where
        normalized_www_auth is the normalised full value and
        normalized_scheme is the scheme token used to build the
        Authorization header (e.g. "Bearer").
    """
    scheme, sep, params = www_auth.partition(" ")
    normalized_scheme = (
        scheme.capitalize() if scheme.lower() in ("basic", "bearer") else scheme
    )
    if sep and params:
        cleaned_params = ",".join(part.strip() for part in params.split(","))
        normalized_www_auth = f"{normalized_scheme} {cleaned_params}"
    else:
        normalized_www_auth = normalized_scheme
    return normalized_www_auth, normalized_scheme


@dataclass
class RetryConfig:
    """Configuration for retry middleware.

    :param max_retries: Maximum number of retry attempts.
    :param backoff_factor: Multiplier for exponential backoff delay.
    :param status_codes: HTTP status codes that trigger a retry.
    :param exceptions: Exception types that trigger a retry.  Defaults to
        ``requests.exceptions.ConnectionError`` and
        ``requests.exceptions.Timeout`` which are the exceptions actually
        raised by the ``requests`` library on network failures.
    """
    max_retries: int = 3
    backoff_factor: float = 1.0
    status_codes: tuple = (500, 502, 503, 504)
    exceptions: tuple = field(
        default_factory=lambda: (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        )
    )


class AuthMiddleware(BaseMiddleware):
    """Middleware that implements the full OCI authentication cycle.

    On the first request the middleware lets the call through without
    injecting an ``Authorization`` header (matching the behaviour of the
    legacy ``_legacy_authenticate_and_retry`` path).  When the registry
    replies with **401 Unauthorized** and a ``WWW-Authenticate`` header,
    the middleware:

    1. Parses and normalises the challenge via
       :func:`_normalize_www_authenticate`.
    2. Calls :func:`registryauth.authenticate` to perform the actual
       token / credential exchange (Basic base64 encoding *or* Bearer
       token fetch, including the anonymous-token flow when no
       credentials are supplied).
    3. Retries the request with the resulting ``Authorization`` header.

    This supports Basic, Bearer (authenticated), and Bearer (anonymous)
    registry flows.

    :param username: Username for authentication, or ``None``.
    :param password: Password for authentication, or ``None``.
    :param registry: Registry hostname, used only in error messages.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        registry: str = "<unknown>",
    ):
        self._username = username
        self._password = password
        self._registry = registry

    # -- Override __call__ to get access to next_handler for the retry ------

    def __call__(
        self,
        request: RegistryRequest,
        next_handler: Callable[[RegistryRequest], RegistryResponse],
    ) -> RegistryResponse:
        """Execute the request and handle 401 challenges."""
        processed_request = request
        try:
            processed_request = self.process_request(request)
            response = next_handler(processed_request)
        except Exception as exc:
            return self.handle_error(processed_request, exc)

        if response.status_code != 401:
            return response

        # ---- 401 handling ------------------------------------------------
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise AuthError(
                "Authentication failed",
                f"registry {self._registry!r} returned 401 without "
                "a WWW-Authenticate header",
            )

        auth_scheme = www_auth.split(" ", 1)[0]
        if auth_scheme.lower() == "basic" and (
            self._username is None or self._password is None
        ):
            raise AuthError(
                "Authentication failed",
                "Registry requested Basic authentication but no credentials "
                f"are available for {self._registry!r}. "
                "Run 'regshape auth login' first.",
            )

        normalized_www_auth, normalized_scheme = _normalize_www_authenticate(
            www_auth
        )
        auth_value = registryauth.authenticate(
            normalized_www_auth, self._username, self._password
        )

        # Build the retry request with the negotiated Authorization header.
        retry_headers = dict(processed_request.headers)
        retry_headers["Authorization"] = f"{normalized_scheme} {auth_value}"
        retry_request = RegistryRequest(
            method=processed_request.method,
            url=processed_request.url,
            headers=retry_headers,
            body=processed_request.body,
            stream=processed_request.stream,
            params=processed_request.params,
            timeout=processed_request.timeout,
        )

        return next_handler(retry_request)


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
            from regshape.libs.decorators.sanitization import redact_headers

            self.logger.log(
                self.level,
                f"HTTP Request: {request.method} {request.url}",
                extra={
                    'method': request.method,
                    'url': request.url,  
                    'headers': redact_headers(request.headers),
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
                    'content_length': len(response.body) if response.body is not None else None
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
        processed_request = request
        
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
                return self.process_response(processed_request, response)
                
            except self.config.exceptions as exc:
                last_exception = exc
                
                if attempt < self.config.max_retries:
                    self._wait_backoff(attempt)
                    continue
                else:
                    return self.handle_error(processed_request, exc)
        
        # This shouldn't be reached, but handle it gracefully
        if last_exception:
            return self.handle_error(processed_request, last_exception)
        
        # Fallback - should never happen
        raise RuntimeError("Retry logic reached unexpected state")
    
    def _wait_backoff(self, attempt: int) -> None:
        """Wait for exponential backoff delay."""
        delay = self.config.backoff_factor * (2 ** attempt)
        time.sleep(delay)


class CachingMiddleware(BaseMiddleware):
    """Middleware that caches GET responses for immutable content.
    
    Caches responses based on URL, Accept header, and query parameters.
    Suitable for registry manifests and blobs that are content-addressed
    and therefore immutable.

    Each cached entry carries a timestamp.  When *ttl* is set, entries
    older than *ttl* seconds are treated as stale and transparently
    re-fetched.  When *ttl* is ``None`` (the default) entries never
    expire — appropriate for content-addressed registry objects.
    
    :param max_size: Maximum number of cached responses.
    :param ttl: Time-to-live in seconds for cached entries.  ``None``
        means entries never expire.
    """
    
    def __init__(self, max_size: int = 1000, ttl: Optional[float] = None):
        """Initialize caching middleware.
        
        :param max_size: Maximum number of cached responses
        :param ttl: Time-to-live in seconds for cached entries, or
            ``None`` for no expiration (default)
        """
        self.cache: Dict[str, tuple[float, RegistryResponse]] = {}
        self.max_size = max_size
        self.ttl = ttl
    
    def __call__(self, request: RegistryRequest, next_handler: NextHandler) -> RegistryResponse:
        """Execute request with caching logic."""
        # Only cache GET requests
        if request.method != "GET":
            return super().__call__(request, next_handler)
        
        cache_key = self._get_cache_key(request)
        
        # Check cache first
        if cache_key in self.cache:
            cached_time, cached_response = self.cache[cache_key]
            if self.ttl is None or (time.monotonic() - cached_time) < self.ttl:
                return cached_response
            # Entry is stale — evict and re-fetch
            del self.cache[cache_key]
        
        # Execute request
        processed_request = self.process_request(request)
        response = next_handler(processed_request)
        processed_response = self.process_response(request, response)
        
        # Cache successful responses for immutable content
        if self._should_cache(processed_response):
            self._add_to_cache(cache_key, processed_response)
        
        return processed_response
    
    def _get_cache_key(self, request: RegistryRequest) -> str:
        """Generate cache key from request.
        
        Includes HTTP method, URL, relevant headers (e.g. Accept),
        and query parameters (if available) to avoid collisions
        between requests that can yield different representations.
        """
        # Base components
        method = getattr(request, "method", "")
        url = getattr(request, "url", "")

        # Include Accept header if headers are available
        accept = ""
        headers = getattr(request, "headers", None)
        if isinstance(headers, dict):
            accept = headers.get("Accept", "") or ""

        # Include query parameters if present, normalized by sorting
        params_component = ""
        params = getattr(request, "params", None)
        if isinstance(params, dict):
            # Sort for stable ordering so equivalent param sets
            # produce identical cache keys.
            sorted_items = sorted(params.items())
            params_component = "&".join(f"{k}={v}" for k, v in sorted_items)

        return f"{method}:{url}:accept={accept}:params={params_component}"
    
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
        """Add response to cache with size limit.

        Each entry is stored as a ``(timestamp, response)`` tuple so
        that TTL expiration can be checked on lookup.
        """
        if len(self.cache) >= self.max_size:
            # Simple LRU: remove oldest entry  
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
        
        self.cache[key] = (time.monotonic(), response)
    
    def clear_cache(self) -> None:
        """Clear all cached responses."""
        self.cache.clear()
    
    def get_cache_size(self) -> int:
        """Get current number of cached responses."""
        return len(self.cache)
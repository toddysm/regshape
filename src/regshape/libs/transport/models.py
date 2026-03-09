#!/usr/bin/env python3

"""
:mod:`regshape.libs.transport.models` - Transport Layer Data Models
====================================================================

.. module:: regshape.libs.transport.models
   :platform: Unix, Windows
   :synopsis: Internal data models for the transport middleware pipeline.
              RegistryRequest and RegistryResponse represent HTTP traffic
              flowing through middleware handlers.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from dataclasses import dataclass
from typing import Optional, Union, Iterable, Dict, Any
from collections.abc import Mapping

import requests


@dataclass
class RegistryRequest:
    """Internal representation of an outgoing HTTP request.
    
    Used by the middleware pipeline to pass request data between handlers.
    The `body` field supports both buffered bytes and streaming iterables
    for efficient handling of large payloads.
    
    :param method: HTTP method (GET, POST, PUT, etc.)
    :param url: Complete request URL
    :param headers: Request headers as key-value dict
    :param body: Request body - bytes for buffered, iterable for streaming
    :param stream: Whether to stream the response body
    :param params: Query parameters as key-value dict
    :param timeout: Request timeout in seconds
    """
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[Union[bytes, Iterable[bytes]]] = None
    stream: bool = False
    params: Optional[Dict[str, Any]] = None
    timeout: Optional[int] = None

    def __post_init__(self) -> None:
        """Validate request fields."""
        if not self.method:
            raise ValueError("RegistryRequest.method must not be empty")
        if not self.url:  
            raise ValueError("RegistryRequest.url must not be empty")
        if not isinstance(self.headers, dict):
            raise TypeError("RegistryRequest.headers must be a dict")


@dataclass
class RegistryResponse:
    """Internal representation of an HTTP response.
    
    Used by the middleware pipeline to pass response data between handlers.
    Preserves the original ``requests.Response`` for streaming access while
    providing a clean interface for middleware processing.

    For **non-streaming** responses the ``body`` field contains the full
    response payload as ``bytes``.  For **streaming** responses (blob
    downloads using ``stream=True``) ``body`` is ``None`` so that the
    response iterator is never consumed eagerly.  Callers that need the
    raw streaming iterator should use :meth:`iter_content` or access
    :attr:`raw_response` directly.
    
    :param status_code: HTTP status code
    :param headers: Response headers as key-value dict  
    :param body: Response body as bytes, or ``None`` for streaming responses
    :param raw_response: Original requests.Response for streaming access
    """
    status_code: int
    headers: Dict[str, str]
    body: Optional[bytes]
    raw_response: requests.Response

    def __post_init__(self) -> None:
        """Validate response fields."""
        if not isinstance(self.status_code, int):
            raise TypeError("RegistryResponse.status_code must be an int")
        if not isinstance(self.headers, Mapping):
            raise TypeError("RegistryResponse.headers must be a dict")
        if self.body is not None and not isinstance(self.body, bytes):
            raise TypeError("RegistryResponse.body must be bytes or None")

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_requests_response(
        cls,
        response: requests.Response,
        *,
        stream: bool = False,
    ) -> 'RegistryResponse':
        """Create a RegistryResponse from a ``requests.Response``.

        When *stream* is ``True`` the response body is **not** read so
        that the streaming iterator remains available to the caller.
        The ``body`` attribute will be ``None`` in that case.

        :param response: The ``requests.Response`` to convert.
        :param stream: If ``True``, skip reading ``response.content``
            to preserve streaming.  Defaults to ``False``.
        :returns: A :class:`RegistryResponse` instance.
        """
        # Preserve the CaseInsensitiveDict from requests.Response so
        # that header lookups in middleware are case-insensitive per
        # HTTP spec (RFC 7230 §3.2).
        return cls(
            status_code=response.status_code,
            headers=response.headers,
            body=None if stream else response.content,
            raw_response=response,
        )

    # ------------------------------------------------------------------
    # Convenience properties / methods
    # ------------------------------------------------------------------

    @property
    def is_streaming(self) -> bool:
        """``True`` when the body was not buffered (streaming response)."""
        return self.body is None

    @property
    def content(self) -> bytes:
        """Buffered response body.

        For non-streaming responses this returns the stored ``body``.
        For streaming responses this **eagerly reads** the full body from
        the underlying ``raw_response`` (and caches it in ``body``) so
        subsequent accesses are free.  Prefer :meth:`iter_content` when
        working with large payloads.

        :returns: The full response body as ``bytes``.
        """
        if self.body is None:
            # Materialise the body from the raw response.  This is a
            # one-time operation; the result is cached.
            object.__setattr__(self, 'body', self.raw_response.content)
        return self.body  # type: ignore[return-value]

    @property
    def text(self) -> str:
        """Response body as decoded text (convenience property)."""
        return self.raw_response.text

    @property 
    def ok(self) -> bool:
        """True if status code indicates success (200-299)."""
        return 200 <= self.status_code < 300

    def iter_content(self, chunk_size: int = 65536) -> Iterable[bytes]:
        """Iterate over the response body in chunks.

        Delegates to ``raw_response.iter_content`` so that streaming
        responses are consumed lazily without buffering the full payload
        in memory.

        :param chunk_size: Size of each chunk in bytes (default ``65536``).
        :returns: An iterable of ``bytes`` chunks.
        """
        return self.raw_response.iter_content(chunk_size=chunk_size)
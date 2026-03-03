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
    Preserves the original `requests.Response` for streaming access while
    providing a clean interface for middleware processing.
    
    :param status_code: HTTP status code
    :param headers: Response headers as key-value dict  
    :param body: Response body as bytes
    :param raw_response: Original requests.Response for streaming access
    """
    status_code: int
    headers: Dict[str, str]
    body: bytes
    raw_response: requests.Response

    def __post_init__(self) -> None:
        """Validate response fields."""
        if not isinstance(self.status_code, int):
            raise TypeError("RegistryResponse.status_code must be an int")
        if not isinstance(self.headers, dict):
            raise TypeError("RegistryResponse.headers must be a dict")
        if not isinstance(self.body, bytes):
            raise TypeError("RegistryResponse.body must be bytes")

    @classmethod
    def from_requests_response(cls, response: requests.Response) -> 'RegistryResponse':
        """Create a RegistryResponse from a requests.Response object.
        
        :param response: The requests.Response to convert
        :returns: A RegistryResponse instance
        """
        return cls(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.content,
            raw_response=response
        )

    @property
    def text(self) -> str:
        """Response body as decoded text (convenience property)."""
        return self.raw_response.text

    @property 
    def ok(self) -> bool:
        """True if status code indicates success (200-299)."""
        return 200 <= self.status_code < 300
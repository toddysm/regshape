#!/usr/bin/env python3

"""
:mod: `call_details` - Decorator for logging HTTP request/response headers
===========================================================================

    module:: call_details
    :platform: Unix, Windows
    :synopsis: Provides the ``@debug_call`` decorator and the
               :func:`format_curl_debug` helper. When ``--debug-calls`` is
               enabled, prints each HTTP round-trip in ``curl -v`` style to
               stderr for every HTTP call made through the transport layer.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import functools
import inspect
import sys

import requests

from typing import IO
from urllib.parse import urlparse

from regshape.libs.decorators.sanitization import redact_headers


def format_curl_debug(
    method: str,
    url: str,
    req_headers: dict,
    status_code: int,
    reason: str,
    resp_headers: dict,
    out: IO = None,
) -> None:
    """
    Print one HTTP round-trip in ``curl -v`` style.

    The ``* Connected to`` line is always emitted using the host and port
    parsed from *url*. Request lines are prefixed with ``>``, response lines
    with ``<``, and each block is terminated by a bare ``>`` / ``<`` separator.
    Sensitive header values are redacted via
    :func:`~regshape.libs.decorators.sanitization.redact_headers`.

    Example output::

        * Connected to registry.example.com port 443
        > GET /v2/ HTTP/1.1
        > Host: registry.example.com
        > User-Agent: regshape/0.1
        >
        < HTTP/1.1 401 Unauthorized
        < Www-Authenticate: Bearer realm="https://auth.example.com/token"
        <

    :param method: HTTP method string (e.g. ``"GET"``).
    :param url: Full request URL (scheme + host + path + query).
    :param req_headers: Request headers dict. ``Host`` is emitted from the
        parsed URL and filtered from this dict to avoid duplication.
    :param status_code: HTTP response status code.
    :param reason: HTTP reason phrase (e.g. ``"OK"``, ``"Unauthorized"``).
        Non-string or empty values are silently omitted.
    :param resp_headers: Response headers dict.
    :param out: Writable stream for output. Defaults to ``sys.stderr``.
    """
    if out is None:
        out = sys.stderr

    parsed = urlparse(url)
    host = parsed.hostname or url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    safe_req = redact_headers(dict(req_headers) if req_headers else {})
    safe_resp = redact_headers(dict(resp_headers) if resp_headers else {})

    # Connection info
    print(f"* Connected to {host} port {port}", file=out)

    # Request block
    print(f"> {method} {path} HTTP/1.1", file=out)
    print(f"> Host: {host}", file=out)
    for key, value in safe_req.items():
        if key.lower() != "host":
            print(f"> {key}: {value}", file=out)
    print(">", file=out)

    # Response block
    reason_str = reason if isinstance(reason, str) and reason else ""
    status_line = f"< HTTP/1.1 {status_code}"
    if reason_str:
        status_line = f"{status_line} {reason_str}"
    print(status_line, file=out)
    for key, value in safe_resp.items():
        print(f"< {key}: {value}", file=out)
    print("<", file=out)


def debug_call(func):
    """
    Decorator that prints each HTTP round-trip in ``curl -v`` style when
    ``--debug-calls`` is enabled.

    Designed to wrap ``RegistryClient.request()`` (or any function whose
    signature contains ``method``, ``path``/``url``, and ``headers`` and that
    returns an object with ``status_code``, ``reason``, and ``headers``
    attributes).

    When the decorated function is a bound method and ``self`` exposes a
    ``config.base_url`` (or a top-level ``base_url``) attribute, the path is
    combined with that base URL so that :func:`format_curl_debug` can always
    emit a ``* Connected to`` line with the correct host and port.

    When disabled, the decorator is a lightweight passthrough.

    :param func: The function to wrap.
    :type func: callable
    :return: The wrapped function.
    :rtype: callable
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Import here to avoid circular import at module load time
        from regshape.libs.decorators import get_telemetry_config
        config = get_telemetry_config()
        if not config.debug_calls_enabled:
            return func(*args, **kwargs)

        # Extract request details from the bound function arguments
        try:
            sig = inspect.signature(func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            params = bound.arguments
            method = params.get('method', 'UNKNOWN')
            url = params.get('url', params.get('path', 'UNKNOWN'))
            req_headers = params.get('headers') or {}

            # If only a relative path was found, try to prepend the base URL
            # from the RegistryClient instance (first positional arg = self).
            if not url.startswith(('http://', 'https://')):
                self_obj = next(iter(params.values()), None)
                base_url = None
                if hasattr(self_obj, 'config') and hasattr(self_obj.config, 'base_url'):
                    base_url = self_obj.config.base_url.rstrip('/')
                elif hasattr(self_obj, 'base_url'):
                    base_url = str(self_obj.base_url).rstrip('/')
                if base_url:
                    url = f"{base_url}{url}"
        except (ValueError, TypeError):
            method, url, req_headers = 'UNKNOWN', 'UNKNOWN', {}

        result = func(*args, **kwargs)

        if hasattr(result, 'status_code') and hasattr(result, 'headers'):
            format_curl_debug(
                method,
                url,
                req_headers,
                result.status_code,
                getattr(result, 'reason', ''),
                dict(result.headers),
                config.output,
            )

        return result
    return wrapper


@debug_call
def http_request(url: str, method: str = "GET", headers: dict = None, **kwargs):
    """Thin wrapper around :func:`requests.request` decorated with
    ``@debug_call``.

    Use this instead of calling ``requests.get`` / ``requests.request``
    directly whenever ``--debug-calls`` output is desired for that call site.
    The parameter names (``url``, ``method``, ``headers``) match what
    ``@debug_call`` introspects, so request/response details are logged
    automatically without any manual instrumentation.

    This is a temporary helper until the ``RegistryClient`` transport layer is
    implemented, at which point ``@debug_call`` is applied directly to
    ``RegistryClient.request()`` and this function is no longer needed.

    :param url: Full request URL.
    :param method: HTTP method (default ``"GET"``).
    :param headers: Optional request headers dict.
    :param kwargs: Additional keyword arguments forwarded to
        :func:`requests.request` (e.g. ``timeout``).
    :return: :class:`requests.Response`
    """
    return requests.request(method, url, headers=headers or {}, **kwargs)

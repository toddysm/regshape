#!/usr/bin/env python3

"""
:mod: `call_details` - Decorator for logging HTTP request/response headers
===========================================================================

    module:: call_details
    :platform: Unix, Windows
    :synopsis: Provides the @debug_call decorator. When --debug-calls is
                enabled, prints the request method, path, and headers plus
                the response status code and headers to stderr for every
                HTTP call made through the transport layer.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import functools
import inspect


def debug_call(func):
    """
    Decorator that prints request and response headers when ``--debug-calls``
    is enabled.

    Designed to wrap ``RegistryClient.request()`` (or any function with
    ``method``, ``path``/``url``, and ``headers`` parameters that returns an
    object with ``status_code`` and ``headers`` attributes).

    When disabled, the decorator is a lightweight passthrough.

    Output format::

        [CALL] <method> <path>
        [REQUEST HEADERS]
          Header-Name: value
          ...
        [RESPONSE HEADERS] <status_code>
          Header-Name: value
          ...

    :param func: The function to wrap
    :type func: callable
    :return: The wrapped function
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
            path = params.get('path', params.get('url', 'UNKNOWN'))
            req_headers = params.get('headers') or {}
        except (ValueError, TypeError):
            method, path, req_headers = 'UNKNOWN', 'UNKNOWN', {}

        result = func(*args, **kwargs)

        out = config.output
        print(f"[CALL] {method} {path}", file=out)
        print("[REQUEST HEADERS]", file=out)
        for key, value in req_headers.items():
            print(f"  {key}: {value}", file=out)

        if hasattr(result, 'status_code') and hasattr(result, 'headers'):
            print(f"[RESPONSE HEADERS] {result.status_code}", file=out)
            for key, value in result.headers.items():
                print(f"  {key}: {value}", file=out)

        return result
    return wrapper

#!/usr/bin/env python3

"""
:mod: `call_details` - Decorator for logging HTTP request/response details
===========================================================================

    module:: call_details
    :platform: Unix, Windows
    :synopsis: Provides the ``@debug_call`` decorator and the
               :func:`format_curl_debug` helper. When ``--debug-calls`` is
               enabled, prints each HTTP round-trip in ``curl -v`` style to
               stderr for every HTTP call made through the transport layer.

               Enhanced with per-call elapsed time, response body preview,
               content-length summary, and metrics recording.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import functools
import inspect
import json
import sys
import time

import requests

from datetime import datetime, timezone
from typing import IO, Optional
from urllib.parse import urlparse

from regshape.libs.decorators.sanitization import redact_headers


# Body preview limit at verbosity level 1; unlimited at level 2.
_BODY_PREVIEW_LIMIT = 200


def _body_preview(body: Optional[bytes], content_type: str, verbosity: int) -> str:
    """Produce a body preview string based on content type and verbosity.

    :param body: Raw response body bytes, or ``None``.
    :param content_type: Content-Type header value.
    :param verbosity: Telemetry verbosity level.
    :return: Human-readable body preview string.
    """
    if body is None or len(body) == 0:
        return "(empty)"

    ct_lower = content_type.lower() if content_type else ""
    is_text = (
        "application/json" in ct_lower
        or "+json" in ct_lower
        or ct_lower.startswith("text/")
    )

    if not is_text:
        return f"<binary, {len(body)} bytes>"

    limit = None if verbosity >= 2 else _BODY_PREVIEW_LIMIT
    try:
        decoded = body.decode("utf-8", errors="replace")
    except Exception:
        return f"<binary, {len(body)} bytes>"

    if limit is not None and len(decoded) > limit:
        return decoded[:limit] + "..."
    return decoded


def format_curl_debug(
    method: str,
    url: str,
    req_headers: dict,
    status_code: int,
    reason: str,
    resp_headers: dict,
    out: IO = None,
    *,
    elapsed: Optional[float] = None,
    resp_body: Optional[bytes] = None,
    req_content_length: Optional[int] = None,
    verbosity: int = 1,
    log_file: IO = None,
) -> None:
    """
    Print one HTTP round-trip in ``curl -v`` style.

    The ``* Connected to`` line is always emitted using the host and port
    parsed from *url*. Request lines are prefixed with ``>``, response lines
    with ``<``, and each block is terminated by a bare ``>`` / ``<`` separator.
    Sensitive header values are redacted via
    :func:`~regshape.libs.decorators.sanitization.redact_headers`.

    :param method: HTTP method string (e.g. ``"GET"``).
    :param url: Full request URL (scheme + host + path + query).
    :param req_headers: Request headers dict.
    :param status_code: HTTP response status code.
    :param reason: HTTP reason phrase.
    :param resp_headers: Response headers dict.
    :param out: Writable stream for output. Defaults to ``sys.stderr``.
    :param elapsed: Per-call elapsed time in seconds, or ``None``.
    :param resp_body: Response body bytes for preview, or ``None``.
    :param req_content_length: Request body content-length, or ``None``.
    :param verbosity: Telemetry verbosity level (1 or 2).
    :param log_file: Secondary log file stream, or ``None``.
    """
    from regshape.libs.decorators.output import telemetry_write

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
    telemetry_write(f"* Connected to {host} port {port}", out, log_file)

    # Request block
    telemetry_write(f"> {method} {path} HTTP/1.1", out, log_file)
    telemetry_write(f"> Host: {host}", out, log_file)
    for key, value in safe_req.items():
        if key.lower() != "host":
            telemetry_write(f"> {key}: {value}", out, log_file)
    if req_content_length is not None:
        # Only add Content-Length line if not already in headers
        if not any(k.lower() == "content-length" for k in safe_req):
            telemetry_write(f"> Content-Length: {req_content_length}", out, log_file)
    telemetry_write(">", out, log_file)

    # Response block
    reason_str = reason if isinstance(reason, str) and reason else ""
    status_line = f"< HTTP/1.1 {status_code}"
    if reason_str:
        status_line = f"{status_line} {reason_str}"
    telemetry_write(status_line, out, log_file)
    for key, value in safe_resp.items():
        telemetry_write(f"< {key}: {value}", out, log_file)
    telemetry_write("<", out, log_file)

    # Per-call elapsed time
    if elapsed is not None:
        telemetry_write(f"* Elapsed: {elapsed:.3f}s", out, log_file)

    # Body preview
    if verbosity >= 1:
        resp_ct = ""
        for k, v in (resp_headers or {}).items():
            if k.lower() == "content-type":
                resp_ct = v
                break
        preview = _body_preview(resp_body, resp_ct, verbosity)
        telemetry_write(f"* Body: {preview}", out, log_file)

    # Separator between calls
    telemetry_write("", out, log_file)


def format_curl_debug_json(
    method: str,
    url: str,
    req_headers: dict,
    status_code: int,
    reason: str,
    resp_headers: dict,
    out: IO = None,
    *,
    elapsed: Optional[float] = None,
    resp_body: Optional[bytes] = None,
    req_content_length: Optional[int] = None,
    verbosity: int = 1,
    log_file: IO = None,
) -> None:
    """Emit a single debug_call event as an NDJSON line.

    :param method: HTTP method string.
    :param url: Full request URL.
    :param req_headers: Request headers dict.
    :param status_code: HTTP response status code.
    :param reason: HTTP reason phrase.
    :param resp_headers: Response headers dict.
    :param out: Writable stream for output.
    :param elapsed: Per-call elapsed time in seconds, or ``None``.
    :param resp_body: Response body bytes for preview, or ``None``.
    :param req_content_length: Request body content-length, or ``None``.
    :param verbosity: Telemetry verbosity level.
    :param log_file: Secondary log file stream, or ``None``.
    """
    from regshape.libs.decorators.output import telemetry_write

    if out is None:
        out = sys.stderr

    safe_req = redact_headers(dict(req_headers) if req_headers else {})
    safe_resp = redact_headers(dict(resp_headers) if resp_headers else {})

    resp_ct = ""
    for k, v in (resp_headers or {}).items():
        if k.lower() == "content-type":
            resp_ct = v
            break

    body_size = len(resp_body) if resp_body is not None else 0
    preview = _body_preview(resp_body, resp_ct, verbosity) if resp_body is not None else None

    event = {
        "type": "debug_call",
        "request": {
            "method": method,
            "url": url,
            "headers": safe_req,
        },
        "response": {
            "status_code": status_code,
            "reason": reason if isinstance(reason, str) else "",
            "headers": safe_resp,
            "body_preview": preview,
            "body_size": body_size,
        },
        "elapsed_s": round(elapsed, 6) if elapsed is not None else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if req_content_length is not None:
        event["request"]["content_length"] = req_content_length

    telemetry_write(json.dumps(event, separators=(",", ":")), out, log_file)


def debug_call(func):
    """
    Decorator that prints each HTTP round-trip in ``curl -v`` style when
    ``--debug-calls`` is enabled, and records metrics when ``--metrics`` is
    enabled.

    Enhanced with per-call elapsed time measurement, response body preview,
    and automatic metrics recording.

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

        if not config.debug_calls_enabled and not config.metrics_enabled:
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

        # Determine request content length from kwargs
        req_content_length = None
        data = kwargs.get('data') or params.get('data') if 'params' in dir() else None
        if data is not None:
            if isinstance(data, (bytes, str)):
                req_content_length = len(data) if isinstance(data, bytes) else len(data.encode('utf-8'))

        # Measure elapsed time
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start

        if hasattr(result, 'status_code') and hasattr(result, 'headers'):
            # Extract response body for preview (limited read)
            resp_body = None
            if hasattr(result, 'content'):
                resp_body = result.content

            # Parse Content-Length defensively; fall back to body length if needed
            raw_content_length = result.headers.get('Content-Length')
            resp_content_length = None
            if raw_content_length is not None:
                try:
                    resp_content_length = int(raw_content_length)
                except (ValueError, TypeError):
                    resp_content_length = None

            # Record metrics if enabled
            if config.metrics_enabled:
                config.metrics.record_request(
                    status_code=result.status_code,
                    bytes_sent=req_content_length or 0,
                    bytes_received=resp_content_length or len(resp_body or b""),
                    elapsed=elapsed,
                )

            # Emit debug output if enabled
            if config.debug_calls_enabled:
                if config.output_format == "json":
                    format_curl_debug_json(
                        method, url, req_headers,
                        result.status_code,
                        getattr(result, 'reason', ''),
                        dict(result.headers),
                        config.output,
                        elapsed=elapsed,
                        resp_body=resp_body,
                        req_content_length=req_content_length,
                        verbosity=config.verbosity,
                        log_file=config.log_file,
                    )
                else:
                    format_curl_debug(
                        method, url, req_headers,
                        result.status_code,
                        getattr(result, 'reason', ''),
                        dict(result.headers),
                        config.output,
                        elapsed=elapsed,
                        resp_body=resp_body,
                        req_content_length=req_content_length,
                        verbosity=config.verbosity,
                        log_file=config.log_file,
                    )

        return result
    return wrapper


@debug_call
def http_request(url: str, method: str = "GET", headers: dict = None, **kwargs):
    """Thin wrapper around :func:`requests.request` decorated with
    ``@debug_call``.

    Use this instead of calling ``requests.get`` / ``requests.request``
    directly whenever ``--debug-calls`` output is desired for that call site.

    :param url: Full request URL.
    :param method: HTTP method (default ``"GET"``).
    :param headers: Optional request headers dict.
    :param kwargs: Additional keyword arguments forwarded to
        :func:`requests.request` (e.g. ``timeout``).
    :return: :class:`requests.Response`
    """
    return requests.request(method, url, headers=headers or {}, **kwargs)

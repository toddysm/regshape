#!/usr/bin/env python3

"""Shared URI resolution and origin checks for pagination, probes and authentication.

Redirect users share the same 30-hop safety budget and supported status codes.
Successful off-origin downloads remain allowed. A redirected authentication
response may never select where configured registry credentials are negotiated.
"""

from urllib.parse import urlsplit, urlunsplit

import requests

from regshape.libs._http import close_response
from regshape.libs.errors import AuthError

MAX_REDIRECTS = 30
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def require_authentication_origin(response: requests.Response, base_url: str) -> None:
    """Require the actual response origin before interpreting a 401 or probing for a challenge.

    Compare scheme, ASCII hostname and effective port with the configured
    registry. Missing/malformed response URLs fail closed; the requested URL is
    not evidence of where Requests ultimately received a redirected response.
    This validates the challenge issuer, NOT its token realm: a trusted registry
    may legitimately delegate token issuance to another origin.

    :param response: Owned response whose status is 401; no body is read.
    :param base_url: Configured registry origin, or an explicit trusted origin
        supplied to standalone authentication middleware.
    :raises AuthError: If origin cannot be verified. The rejected response is
        closed, preserving this primary failure if cleanup/logging also fails.
        A valid response remains caller-owned and is not closed here.
    """
    try:
        actual = getattr(response, "url", None)
        if (
            not isinstance(base_url, str) or not base_url
            or not isinstance(actual, str) or not actual
            or "\\" in actual
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in actual)
            or _url_origin(actual) != _url_origin(base_url)
        ):
            raise ValueError("Unverified authentication response origin")
    except ValueError:
        error = AuthError("Refusing authentication from an unverified registry response origin")
        close_response(response, primary_error=error)
        raise error from None


def resolve_registry_url(base_url: str, target: str, context_url: str) -> str:
    """Resolve a credential-free HTTP reference on the configured registry origin.

    Preserve URI path/query semantics, remove fragments, and reject scheme or
    origin changes before dispatch. Authorities must be ASCII; international
    domain names must use their punycode form to avoid differing IDNA mappings
    between URL validation and the HTTP client. Invalid references raise a
    sanitized error without retaining a credential-bearing exception chain.

    :param base_url: Configured registry origin.
    :param target: Absolute or relative URI reference from a response.
    :param context_url: Actual response/request URL used as the reference base.
    :returns: An absolute URL with the same scheme, host, and effective port.
    :raises ValueError: For invalid, credential-bearing, or cross-origin URLs.
    """
    try:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in target):
            raise ValueError("Invalid URI characters")
        if "\\" in target:
            raise ValueError("Invalid URI backslash")
        resolved = _resolve_reference(context_url, target)
        if _url_origin(resolved) != _url_origin(base_url):
            raise ValueError("Registry origin changed")
        parts = urlsplit(resolved)
        return urlunsplit(parts._replace(path=parts.path or "/"))
    except ValueError:
        raise ValueError(
            "URLs must use a valid ASCII authority, contain no credentials, "
            "and stay on the configured registry origin"
        ) from None


def _url_origin(url: str) -> tuple[str, str, int]:
    """Return an ASCII HTTP origin; reject Unicode authorities, userinfo, and bad ports."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("HTTP origin required")
    if not parts.netloc.isascii():
        raise ValueError("ASCII URI authority required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URI credentials are not allowed")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname, port


def _resolve_reference(context_url: str, target: str) -> str:
    """Resolve RFC 3986 references without collapsing empty path segments.

    Distinguish an absent query from an explicitly empty query. Fragments are
    removed because they are not part of an HTTP request target.
    """
    base = urlsplit(context_url)
    reference = urlsplit(target)
    if reference.scheme:
        resolved = reference._replace(path=_remove_dot_segments(reference.path))
    elif reference.netloc or target.startswith("//"):
        resolved = reference._replace(
            scheme=base.scheme,
            path=_remove_dot_segments(reference.path),
        )
    else:
        if not reference.path:
            path = base.path
            query = reference.query if "?" in target.split("#", 1)[0] else base.query
        else:
            path = reference.path
            if not path.startswith("/"):
                path = base.path.rpartition("/")[0] + "/" + path
            path = _remove_dot_segments(path)
            query = reference.query
        resolved = reference._replace(
            scheme=base.scheme, netloc=base.netloc, path=path, query=query,
        )
    return urlunsplit(resolved._replace(fragment=""))


def _remove_dot_segments(path: str) -> str:
    """Apply RFC 3986 section 5.2.4 while retaining ordinary empty path segments."""
    output = ""
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./"):
            path = path[2:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = path[3:]
            output = output.rpartition("/")[0]
        elif path == "/..":
            path = "/"
            output = output.rpartition("/")[0]
        elif path in (".", ".."):
            path = ""
        else:
            boundary = path.find("/", 1 if path.startswith("/") else 0)
            if boundary == -1:
                output += path
                path = ""
            else:
                output += path[:boundary]
                path = path[boundary:]
    return output

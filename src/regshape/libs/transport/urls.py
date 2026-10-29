#!/usr/bin/env python3

"""Shared URI resolution and origin checks for registry pagination and auth probes.

Redirect users share the same 30-hop safety budget and supported status codes.
These client policies do not change the default behavior of ordinary requests.
"""

from urllib.parse import urlsplit, urlunsplit

MAX_REDIRECTS = 30
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def resolve_registry_url(base_url: str, target: str, context_url: str) -> str:
    """Resolve a credential-free HTTP reference on the configured registry origin.

    Preserve URI path/query semantics, remove fragments, and reject scheme or
    origin changes before dispatch. Invalid references raise a sanitized error
    without retaining a printable credential-bearing parser exception chain.

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
            "URLs must be valid, credential-free, and on the configured registry origin"
        ) from None


def _url_origin(url: str) -> tuple[str, str, int]:
    """Return a normalized HTTP origin; reject userinfo, bad ports, and schemes."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("HTTP origin required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URI credentials are not allowed")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname.encode("idna").decode("ascii").lower(), port


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

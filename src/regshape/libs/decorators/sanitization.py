#!/usr/bin/env python3

"""
:mod: `sanitization` - Header sanitization utilities for telemetry output
==========================================================================

    module:: sanitization
    :platform: Unix, Windows
    :synopsis: Shared utilities for redacting sensitive HTTP header values
               before they are written to any telemetry or debug output.
               Used by both the ``@debug_call`` decorator and the
               ``_debug_http`` helper in ``cli/auth.py``.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from typing import Dict

# Headers whose values must never appear in logs.
SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
})

# Known HTTP authentication scheme names (RFC 7235 / IANA registry).
# Only schemes in this list are preserved when redacting auth headers.
# Comparison is case-insensitive; extend as new schemes are encountered.
_AUTH_SCHEMES: frozenset[str] = frozenset({
    "basic",
    "bearer",
    "digest",
    "ntlm",
    "negotiate",
    "aws4-hmac-sha256",
})


def redact_header_value(name: str, value: str) -> str:
    """
    Return a safe representation of a single header value for logging.

    ``Authorization`` and ``Proxy-Authorization`` headers retain the scheme
    token (e.g., ``Bearer``, ``Basic``) for diagnostic utility while the
    credentials are replaced with ``<redacted>``.

    The scheme token is only preserved when **both** conditions hold:

    * The value contains a space separator (i.e. it is not a bare token).
    * The extracted scheme matches a name in ``_AUTH_SCHEMES``
      (case-insensitive).

    If either condition fails the entire value is replaced with
    ``<redacted>`` so that bare credentials are never leaked.
    ``Cookie`` and ``Set-Cookie`` values are always fully redacted.

    Non-sensitive headers are returned unchanged.

    :param name: Header name (case-insensitive).
    :type name: str
    :param value: Raw header value.
    :type value: str
    :returns: Safe value suitable for logging.
    :rtype: str
    """
    name_lower = name.lower()
    if name_lower not in SENSITIVE_HEADERS:
        return value
    if name_lower in ("cookie", "set-cookie"):
        return "<redacted>"
    # authorization / proxy-authorization: keep only the scheme token.
    # Security: require (a) a real space separator so that a bare credential
    # with no space is never treated as a scheme, and (b) the token must be
    # a known auth scheme name so that an unknown/long credential token is
    # not exposed even when a space happens to be present.
    scheme, sep, _ = value.partition(" ")
    if sep and scheme.lower() in _AUTH_SCHEMES:
        return f"{scheme} <redacted>"
    return "<redacted>"


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Return a copy of *headers* with sensitive values redacted.

    Iterates over all entries and applies :func:`redact_header_value` to
    each one, producing a new dict that is safe to pass to any logging or
    print statement.

    :param headers: Original headers dict.
    :type headers: dict[str, str]
    :returns: New dict with sensitive values replaced.
    :rtype: dict[str, str]
    """
    return {k: redact_header_value(k, v) for k, v in headers.items()}

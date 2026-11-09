"""Bounded, instrumented token negotiation for an operation-owned download.

Tokens retain expiry metadata and use the worker's owned session with finite
timeouts and no token-endpoint redirects.
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import requests

from regshape.libs._http import ClosingResponse, close_response
from regshape.libs.auth.registryauth import (
    _ACR_REFRESH_TOKEN_USERNAME,
    _TOKEN_CLIENT_ID,
    _get_basic_auth,
    _parse_auth_header,
)
from regshape.libs.decorators.call_details import http_request
from regshape.libs.errors import AuthError, ManifestError
from regshape.libs.manifests._stream import entity_chunks
from regshape.libs.transport.urls import REDIRECT_STATUSES, _url_origin


@dataclass(frozen=True)
class DownloadToken:
    """An origin/scope-bound Authorization value with optional monotonic expiry.

    :param authorization: Negotiated Basic/Bearer header, excluded from repr.
    :param expires_at: Monotonic deadline, or None if no expiry was supplied.
    :param realm: Validated token endpoint, empty for Basic.
    :param service: Registry-issued service audience, empty for Basic.
    :param scope: Repository action scope used for this negotiation.
    """

    authorization: str = field(repr=False)
    expires_at: float | None
    realm: str = field(repr=False)
    service: str
    scope: str


def _token_payload(response: requests.Response) -> dict:
    """Decode at most 1 MiB of token JSON; reject malformed or unsuccessful responses.

    The caller owns/closes the response. Error messages exclude endpoint URLs,
    payloads and credentials.
    """
    if response.status_code != 200:
        raise AuthError("Token request failed", f"Status code: {response.status_code}")
    payload = bytearray()
    try:
        for chunk in entity_chunks(response, 65_536):
            if len(payload) + len(chunk) > 1_048_576:
                raise AuthError("Token response exceeds the authentication byte limit")
            payload.extend(chunk)
    except ManifestError:
        raise AuthError("Invalid token response encoding") from None
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise AuthError("Invalid token response JSON") from None
    if not isinstance(value, dict):
        raise AuthError("Token response must be a JSON object")
    return value


def _reject_token_redirect(response: requests.Response, **kwargs: object) -> requests.Response:
    """Close and reject token redirects before Requests buffers bodies for Response.next.

    Successful/non-redirect responses remain caller-owned. A cleanup failure is
    reported without replacing the sanitized authentication failure.
    """
    if response.status_code in REDIRECT_STATUSES:
        error = AuthError("Token endpoint redirects are not allowed")
        close_response(response, primary_error=error)
        raise error
    return response


def negotiate_download_token(
    challenge: str,
    username: str | None,
    password: str | None,
    *,
    base_url: str,
    scope: str,
    session: requests.Session,
    timeout: int,
) -> DownloadToken:
    """Negotiate Basic or Bearer auth from an already origin-validated challenge.

    Bearer realms may be external but must be credential-free HTTP(S) URLs,
    with no TLS downgrade. Missing scope is bound to the requested repository.
    Challenged scopes must cover pull on the same repository; token requests
    still ask only for the expected scope. The service audience is optional.
    Only the ACR sentinel username with a nonempty password enables the
    refresh-token POST fallback; failures retain the original GET 401.
    Both exchanges request identity encoding and reject redirects in a response
    hook before redirect-body buffering; token JSON remains bounded to 1 MiB.

    :param challenge: Registry-issued WWW-Authenticate value.
    :param username: Resolved credential snapshot, possibly anonymous.
    :param password: Resolved password/refresh token, possibly absent.
    :param base_url: Trusted configured registry origin.
    :param scope: Expected repository pull scope, or empty for a base probe.
    :param session: Worker-local session on the operation-owned pool.
    :param timeout: Finite per-request timeout in seconds.
    :returns: Immutable header and expiry metadata.
    :raises AuthError: On unsafe challenge, invalid metadata, or exchange failure.
    """
    parsed = _parse_auth_header(challenge)
    scheme = parsed["scheme"].lower()
    if scheme == "basic":
        if username is None or password is None:
            raise AuthError("Basic authentication requires stored credentials")
        return DownloadToken("Basic " + _get_basic_auth(username, password), None, "", "", scope)
    if scheme != "bearer":
        raise AuthError("Unsupported download authentication scheme")
    realm = parsed.get("realm", "")
    service = parsed.get("service", "")
    challenged_scope = parsed.get("scope", scope)
    try:
        realm_origin = _url_origin(realm)
        if (
            "\\" in realm or urlsplit(realm).fragment
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in realm)
            or (_url_origin(base_url)[0] == "https" and realm_origin[0] != "https")
        ):
            raise ValueError("Unsafe token realm")
    except ValueError:
        raise AuthError("Invalid download authentication realm") from None
    if challenged_scope != scope:
        repository, _, actions = challenged_scope.rpartition(":")
        expected_repository, _, expected_actions = scope.rpartition(":")
        if (
            not scope or repository != expected_repository
            or not set(expected_actions.split(",")).issubset(actions.split(","))
        ):
            raise AuthError("Invalid download authentication repository scope")
    params = {"service": service} if service else {}
    if scope:
        params["scope"] = scope
    credentials = (username, password) if username is not None and password is not None else None
    try:
        with ClosingResponse(http_request(
            realm, "GET", session=session, params=params, auth=credentials,
            headers={"Accept-Encoding": "identity"},
            timeout=timeout, stream=True, allow_redirects=False,
            hooks={"response": _reject_token_redirect},
        )) as response:
            refresh = (
                response.status_code == 401
                and username == _ACR_REFRESH_TOKEN_USERNAME and bool(password)
            )
            if not refresh:
                payload = _token_payload(response)
        if refresh:
            try:
                with ClosingResponse(http_request(
                    realm, "POST", session=session,
                    headers={"Accept-Encoding": "identity"},
                    data={
                        "grant_type": "refresh_token", "refresh_token": password,
                        "client_id": _TOKEN_CLIENT_ID, **params,
                    },
                    timeout=timeout, stream=True, allow_redirects=False,
                    hooks={"response": _reject_token_redirect},
                )) as response:
                    payload = _token_payload(response)
            except (AuthError, requests.exceptions.RequestException):
                logging.getLogger(__name__).warning(
                    "ACR refresh-token exchange failed; retaining original token rejection"
                )
                raise AuthError("Token request failed", "Status code: 401") from None
    except requests.exceptions.RequestException:
        raise AuthError("Download token transport failed") from None
    token = payload.get("access_token") or payload.get("token")
    if (
        not isinstance(token, str) or not token or not token.isascii()
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)
    ):
        raise AuthError("Token response missing a valid token")
    expires_at = None
    if "expires_in" in payload:
        lifetime = payload["expires_in"]
        if type(lifetime) not in (int, float) or not 0 <= lifetime <= 2**31 or not math.isfinite(lifetime):
            raise AuthError("Invalid token expiry metadata")
        remaining = lifetime
        if "issued_at" in payload:
            issued = payload["issued_at"]
            try:
                if not isinstance(issued, str):
                    raise TypeError("Invalid issuance time")
                # Python 3.10 accepts only millisecond/microsecond ISO fractions.
                issued = re.sub(
                    r"\.([0-9]+)(?=Z$|[+-][0-9]{2}:[0-9]{2}$)",
                    lambda match: "." + match[1][:6].ljust(6, "0"), issued,
                )
                timestamp = datetime.fromisoformat(issued.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("Issuance time requires a timezone")
                remaining = max(0.0, min(lifetime, timestamp.timestamp() + lifetime - time.time()))
            except (TypeError, ValueError, OverflowError):
                raise AuthError("Invalid token issuance metadata") from None
        expires_at = time.monotonic() + remaining
    return DownloadToken("Bearer " + token, expires_at, realm, service, scope)

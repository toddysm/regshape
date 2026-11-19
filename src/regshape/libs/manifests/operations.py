#!/usr/bin/env python3

"""
:mod:`regshape.libs.manifests.operations` - OCI manifest operations
====================================================================

.. module:: regshape.libs.manifests.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for GET, HEAD, PUT, and DELETE
              manifest operations against OCI Distribution-compliant
              registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting (``sys.exit``, ``click.echo``) is the caller's
responsibility.
"""

import hashlib
import json

import requests

from regshape.libs._http import ClosingResponse, download_total
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, ManifestError, ReferenceValidationError
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.models.mediatype import (
    ALL_MANIFEST_MEDIA_TYPES,
    INDEX_MEDIA_TYPES,
    MANIFEST_MEDIA_TYPES,
    OCI_IMAGE_MANIFEST,
)
from regshape.libs.progress import (
    ManifestKind,
    ManifestPhase,
    ManifestProgressCallback,
    _ManifestProgress,
)
from regshape.libs.refs import format_ref, validate_digest
from regshape.libs.transport import RegistryClient

_MANIFEST_CHUNK_SIZE = 64 * 1024
DEFAULT_MANIFEST_ACCEPT = ",".join(sorted(ALL_MANIFEST_MEDIA_TYPES))


class _BufferedManifestText(requests.Response):
    """Reuse Requests' text decoder over collected bytes without owning a network response."""

    def __init__(self, payload: bytes, encoding: str | None) -> None:
        """Borrow the payload and charset; inherited text handles detection and fallback."""
        super().__init__()
        self._payload = payload
        self.encoding = encoding

    @property
    def content(self) -> bytes:
        """Expose the collected payload to Requests' public text/apparent-encoding properties."""
        return self._payload


def _manifest_kind(content_type: str) -> ManifestKind:
    """Classify advertised media types without parsing or validating the response body."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in INDEX_MEDIA_TYPES:
        return ManifestKind.INDEX
    if media_type in MANIFEST_MEDIA_TYPES:
        return ManifestKind.MANIFEST
    return ManifestKind.UNKNOWN


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def get_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str | None = None,
    *,
    progress_callback: ManifestProgressCallback | None = None,
) -> tuple[str, str, str]:
    """Fetch the manifest for *repo*/*reference*.

    Issues a GET request to ``/v2/{repo}/manifests/{reference}``.  The
    401→auth→retry cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param reference: Tag or digest (e.g. ``latest`` or
                      ``sha256:abc...``).
    :param accept: ``Accept`` header override, sent verbatim (including empty
        strings). Omitted/``None`` uses :data:`DEFAULT_MANIFEST_ACCEPT`.
    :param progress_callback: Optional synchronous byte observer. Enables streaming
        in 64 KiB reads; RECEIVED follows decoding and response cleanup, not
        model/digest validation or caller output. Omission retains buffered decoding.
    :returns: ``(body_str, content_type, digest)`` with unchanged Requests text
        decoding and empty strings for absent headers. The complete result is
        buffered in memory even when byte observations are enabled.
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    :raises TypeError: If the supplied observer is not callable.

    Observer and cleanup exceptions propagate unchanged. The owned response is
    closed on every path; secondary cleanup/notification failures never replace
    an active error. Length metadata is advisory, not an integrity guarantee.
    """
    if accept is None:
        accept = DEFAULT_MANIFEST_ACCEPT
    path = f"/v2/{repo}/manifests/{reference}"
    with _ManifestProgress(progress_callback) as progress:
        stream_options = {"stream": True} if progress_callback is not None else {}
        with ClosingResponse(client.get(path, headers={"Accept": accept}, **stream_options)) as response:
            _raise_for_manifest_error(response, client.config.registry, repo, reference)
            content_type = response.headers.get("Content-Type", "")
            digest = response.headers.get("Docker-Content-Digest", "")
            if progress_callback is None:
                body = response.text
            else:
                progress.kind = _manifest_kind(content_type)
                progress.total_bytes = download_total(response.headers)
                progress.emit(ManifestPhase.TRANSFERRING)
                chunks: list[bytes] = []
                for chunk in response.iter_content(chunk_size=_MANIFEST_CHUNK_SIZE):
                    if chunk:
                        chunks.append(chunk)
                        progress.advance(len(chunk))
                progress.finish_body()
                payload = b"".join(chunks)
                chunks.clear()
                body = _BufferedManifestText(payload, response.encoding).text
        progress.emit(ManifestPhase.RECEIVED)
        return body, content_type, digest


@track_time
def head_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str | None = None,
) -> tuple[str, str, int]:
    """Return metadata for *repo*/*reference* without downloading the body.

    Issues a HEAD request to ``/v2/{repo}/manifests/{reference}``.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param reference: Tag or digest.
    :param accept: ``Accept`` header override, sent verbatim (including empty
        strings). Omitted/``None`` uses :data:`DEFAULT_MANIFEST_ACCEPT`.
    :returns: ``(digest, media_type, size)``
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    if accept is None:
        accept = DEFAULT_MANIFEST_ACCEPT
    path = f"/v2/{repo}/manifests/{reference}"
    response = client.head(path, headers={"Accept": accept})
    _raise_for_manifest_error(response, client.config.registry, repo, reference)
    digest = response.headers.get("Docker-Content-Digest", "")
    media_type = response.headers.get("Content-Type", "")
    try:
        size = int(response.headers.get("Content-Length", "0"))
    except ValueError:
        size = 0
    return digest, media_type, size


@track_time
def push_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    body: bytes,
    content_type: str | None = None,
    *,
    expected_digest: str | None = None,
) -> str:
    """Push a manifest to *repo* as *reference*.

    Issues a PUT request to ``/v2/{repo}/manifests/{reference}``.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param reference: Tag or digest to create/overwrite.
    :param body: Raw manifest JSON bytes.
    :param content_type: Header override, sent verbatim (including empty
        strings). Omitted/``None`` uses the JSON object's ``mediaType`` field,
        falling back to ``OCI_IMAGE_MANIFEST`` for a missing field, non-object
        JSON or undecodable/invalid JSON. The body bytes are never rewritten.
    :param expected_digest: Optional strict SHA-256/SHA-512 payload identity.
        When supplied, verify bytes before sending, require 201 Created, and
        verify any reported digest (including a different supported algorithm).
        A digest reference is independently verified against the same bytes.
        The final response must identify a prepared PUT carrying those exact
        bytes; method/body-changing redirects or missing request evidence fail.
    :returns: The reported header for legacy callers; with expected_digest,
        the locally verified expected identity, including when no header exists.
    :raises AuthError: On authentication failure.
    :raises ManifestError: On registry errors or strict acknowledgement mismatch.
    :raises ValueError: On malformed strict-mode inputs, before network effects.
    :raises requests.exceptions.RequestException: On transport errors.

    Owns/closes every acquired response. A secondary close failure never replaces
    an active error. Omitting expected_digest retains legacy 2xx/header behavior.
    """
    from regshape.libs.manifests.download import _digest_algorithm, _validate_inputs

    if content_type is None:
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            content_type = OCI_IMAGE_MANIFEST
        else:
            content_type = (
                obj.get("mediaType", OCI_IMAGE_MANIFEST)
                if isinstance(obj, dict) else OCI_IMAGE_MANIFEST
            )

    if expected_digest is not None:
        requested = _validate_inputs(repo, reference, content_type, expected_digest, None, max(1, len(body)))
        for digest in (expected_digest, requested):
            if digest is not None:
                algorithm = _digest_algorithm(digest)
                if f"{algorithm}:{hashlib.new(algorithm, body).hexdigest()}" != digest:
                    raise ManifestError("Submitted manifest does not match expected/requested digest")
    path = f"/v2/{repo}/manifests/{reference}"
    with ClosingResponse(client.put(path, headers={"Content-Type": content_type}, data=body)) as response:
        _raise_for_manifest_error(response, client.config.registry, repo, reference)
        reported = response.headers.get("Docker-Content-Digest", "")
        if expected_digest is None:
            return reported
        if response.status_code != 201:
            raise ManifestError("Manifest publication requires HTTP 201 Created")
        prepared = response.request
        if (
            prepared is None or prepared.method != "PUT"
            or prepared.body != body
        ):
            raise ManifestError(
                "Manifest acknowledgement does not identify a PUT with the verified payload",
                "a redirect may have changed the method or removed the body",
            )
        if "Docker-Content-Digest" in response.headers:
            try:
                algorithm = _digest_algorithm(reported)
            except ValueError as error:
                raise ManifestError("Registry reported an invalid manifest digest") from error
            if f"{algorithm}:{hashlib.new(algorithm, body).hexdigest()}" != reported:
                raise ManifestError("Registry reported a digest different from the submitted manifest")
        return expected_digest


@track_time
def delete_manifest(
    client: RegistryClient,
    repo: str,
    digest: str,
    *,
    strict: bool = True,
) -> None:
    """Delete the manifest identified by *digest* from *repo*.

    Issues a DELETE request to ``/v2/{repo}/manifests/{digest}``.  The OCI
    Distribution Spec requires deletion by digest, not by tag.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param digest: Complete lowercase SHA-256 or SHA-512 manifest digest.
    :param strict: Validate the digest before HTTP by default. ``False`` sends
        the supplied value verbatim for deliberate registry-conformance tests.
    :raises ReferenceValidationError: On an invalid digest when strict is enabled.
    :raises AuthError: On authentication failure.
    :raises ManifestError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    if strict:
        validate_manifest_digest(digest)
    path = f"/v2/{repo}/manifests/{digest}"
    response = client.delete(path)
    _raise_for_manifest_error(response, client.config.registry, repo, digest)


def validate_manifest_digest(digest: str) -> str:
    """Validate a manifest-delete target without transport or credential access.

    :param digest: Parsed manifest digest; tags and invalid digests are rejected.
    :returns: The complete, supported digest unchanged.
    :raises ReferenceValidationError: If shared digest validation fails, with
        the manifest-delete usage message retained for CLI compatibility.
    """
    try:
        return validate_digest(digest)
    except ReferenceValidationError as exc:
        raise ReferenceValidationError(
            "manifest delete requires a digest reference (@sha256:...); "
            "tag references are not supported by the OCI spec for delete operations"
        ) from exc


# ===========================================================================
# Private error helper
# ===========================================================================


def _raise_for_manifest_error(
    response: requests.Response,
    registry: str,
    repo: str,
    reference: str,
) -> None:
    """Raise on non-2xx responses.

    Maps HTTP status codes to :class:`~regshape.libs.errors.ManifestError`
    or :class:`~regshape.libs.errors.AuthError` with a descriptive message
    parsed from the OCI error JSON body.

    Read error payloads before tolerant parsing so framing/read failures retain
    their original exception instead of being mistaken for malformed error JSON.

    :raises AuthError: For 401.
    :raises ManifestError: For all other non-2xx status codes.
    :raises requests.exceptions.RequestException: If the error body cannot be read.
    """
    if 200 <= response.status_code < 300:
        return

    _ = response.content
    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )
    ref_str = format_ref(registry, repo, reference)

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise ManifestError(
            f"Manifest not found: {ref_str}",
            detail or "HTTP 404",
        )
    raise ManifestError(
        f"Registry error for {ref_str}",
        detail or f"HTTP {response.status_code}",
    )

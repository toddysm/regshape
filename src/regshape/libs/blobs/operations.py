#!/usr/bin/env python3

"""
:mod:`regshape.libs.blobs.operations` - OCI blob operations
============================================================

.. module:: regshape.libs.blobs.operations
   :platform: Unix, Windows
   :synopsis: Library-level functions for HEAD, GET, DELETE, upload (monolithic
              and chunked), and cross-repo mount of OCI blobs against
              OCI Distribution-compliant registries.

.. moduleauthor:: ToddySM <toddysm@gmail.com>

Each function accepts a :class:`~regshape.libs.transport.RegistryClient`
instance that is already initialised with the target registry, credentials,
and transport settings.  The functions are intentionally free of Click/CLI
concerns — error reporting is the caller's responsibility.
"""

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Optional
from urllib.parse import parse_qsl, urlparse

import requests

from regshape.libs._http import ClosingResponse
from regshape.libs._http import download_total as _download_total
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import AuthError, BlobError
from regshape.libs.manifests._stream import entity_chunks
from regshape.libs.manifests.download import _DownloadFile
from regshape.libs.manifests.operations import _BufferedManifestText
from regshape.libs.models.blob import BlobInfo, BlobUploadSession
from regshape.libs.models.error import OciErrorResponse
from regshape.libs.progress import (
    ProgressCallback,
    TransferDirection,
    TransferPhase,
    _TransferProgress,
)
from regshape.libs.transport import RegistryClient
from regshape.libs.transport.body import _UploadBody

_DEFAULT_CHUNK_SIZE = 65_536
_DEFAULT_CONTENT_TYPE = "application/octet-stream"
_SUPPORTED_ALGORITHMS = {"sha256", "sha512"}
logger = logging.getLogger(__name__)


# ===========================================================================
# Public domain operations
# ===========================================================================


@track_time
def head_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
) -> BlobInfo:
    """Check existence and retrieve metadata for a blob without downloading
    its content.

    Issues a ``HEAD /v2/{repo}/blobs/{digest}`` request.  The 401→auth→retry
    cycle is handled transparently by *client*.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name (e.g. ``"myrepo/myimage"``).
    :param digest: Blob digest (e.g. ``"sha256:abc..."``).
    :returns: :class:`~regshape.libs.models.blob.BlobInfo` constructed from
        the response headers.
    :raises AuthError: On authentication failure.
    :raises BlobError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.

    The response is owned and closed on success/failure; cleanup preserves an
    active primary error. Only HTTP 200 confirms presence; status_code 404
    identifies a missing blob, and other statuses remain explicit failures.
    """
    path = f"/v2/{repo}/blobs/{digest}"
    with ClosingResponse(client.head(path)) as response:
        _raise_for_blob_error(response, client.config.registry, repo, digest)
        if response.status_code != 200:
            raise BlobError("Blob existence requires HTTP 200 OK", status_code=response.status_code)
        return _blob_info_from_response(response, digest)


@track_time
def get_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
    output_path: Optional[str] = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    *,
    progress_callback: ProgressCallback | None = None,
    total_size: int | None = None,
    max_bytes: int | None = None,
    output_dir_fd: int | None = None,
) -> BlobInfo:
    """Download a blob and verify its digest.

    Issues a ``GET /v2/{repo}/blobs/{digest}`` request with streaming enabled.
    When *output_path* is supplied the response body is streamed to that file
    in *chunk_size*-byte increments.  When *output_path* is ``None`` the body
    is still consumed incrementally in chunks but only to compute the digest;
    the content itself is not retained after verification.  In both cases the
    digest of the received bytes is verified against *digest* before returning.

    The hash algorithm is derived from the *digest* prefix (e.g. ``sha256``
    or ``sha512``).  Unsupported algorithms cause an immediate
    :class:`~regshape.libs.errors.BlobError` before any network I/O.  The
    set of supported algorithms is ``sha256`` and ``sha512``.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param digest: Expected blob digest (e.g. ``"sha256:..."`` or
        ``"sha512:..."``).
    :param output_path: File path to write the blob to. When ``None`` the
        content is streamed only for digest verification and then discarded.
    :param chunk_size: Streaming chunk size in bytes (default ``65536``).
        Must be a positive integer.
    :param progress_callback: Optional synchronous observer of immutable transfer
        events. Callback errors propagate unchanged, without recursive notification.
    :param total_size: Optional trusted size of the consumed payload. With progress
        enabled, a valid Content-Length is used when encoding permits; otherwise
        the total is unknown. A size mismatch raises :class:`BlobError`.
    :param max_bytes: Optional positive hard entity-byte cap. Enables bounded
        decoding/error bodies and exclusive output, checking counts before writes.
    :param output_dir_fd: Borrowed safe parent handle for a single output filename.
        Requires max_bytes; the caller checks platform support and parent identity.
    :returns: :class:`~regshape.libs.models.blob.BlobInfo` built from
        response headers after successful digest verification.
    :raises AuthError: On authentication failure.
    :raises BlobError: On a non-2xx response, an unsupported digest
        algorithm, a digest mismatch, or an I/O error when *output_path*
        is supplied.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    _validate_chunk_size(chunk_size)
    if max_bytes is not None:
        return _get_blob_bounded(
            client, repo, digest, output_path, chunk_size, progress_callback,
            total_size, max_bytes, output_dir_fd,
        )
    if output_dir_fd is not None:
        raise ValueError("output_dir_fd requires max_bytes")
    algorithm, sep, _ = digest.partition(":")
    if not sep or algorithm not in _SUPPORTED_ALGORITHMS:
        raise BlobError(
            f"Unsupported digest algorithm: {algorithm!r}",
            f"supported algorithms: {', '.join(sorted(_SUPPORTED_ALGORITHMS))}",
        )

    with _TransferProgress(
        digest, TransferDirection.DOWNLOAD, progress_callback, total_size,
    ) as progress:
        path = f"/v2/{repo}/blobs/{digest}"
        response = client.get(path, stream=True)
        processed = False
        try:
            _raise_for_blob_error(response, client.config.registry, repo, digest)
            if total_size is None and progress_callback is not None:
                progress.set_total(_download_total(response.headers))
            progress.emit(TransferPhase.TRANSFERRING)
            hasher = hashlib.new(algorithm)
            if output_path is not None:
                try:
                    _stream_to_file(response, output_path, chunk_size, hasher.update, progress)
                except OSError as exc:
                    if progress.callback_failed or isinstance(exc, requests.exceptions.RequestException):
                        raise
                    raise BlobError(
                        f"Cannot write to output path: {output_path}", str(exc),
                    ) from exc
            else:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        hasher.update(chunk)
                        progress.advance(len(chunk))

            progress.check_complete()
            progress.emit(TransferPhase.VERIFYING)
            computed = f"{algorithm}:{hasher.hexdigest()}"
            if computed != digest:
                if output_path is not None:
                    import os
                    try:
                        os.unlink(output_path)
                    except OSError as exc:
                        logger.error("Could not remove invalid blob output (%s)", type(exc).__name__)
                raise BlobError(
                    f"Digest mismatch: expected {digest}, got {computed}",
                    f"registry={client.config.registry} repo={repo}",
                )
            info = _blob_info_from_response(response, digest)
            processed = True
        finally:
            try:
                _close_download_resource(response, failing=not processed)
            except OSError as exc:
                raise requests.exceptions.RequestException(
                    "Could not close the blob download response",
                ) from exc
        progress.complete()
        return info


def _get_blob_bounded(
    client: RegistryClient,
    repo: str,
    digest: str,
    output_path: str | None,
    chunk_size: int,
    callback: ProgressCallback | None,
    total_size: int | None,
    max_bytes: int,
    dir_fd: int | None,
) -> BlobInfo:
    """Receive into an exclusive file under strict pre-write budgets.

    This opt-in path borrows a safe directory handle, never buffers an unbounded
    error body, and preserves immutable observer exceptions through close/cleanup.
    Blob redirects retain the transport's existing credential-stripping policy.
    """
    from regshape.libs.manifests.download import _digest_algorithm

    algorithm = _digest_algorithm(digest)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if output_path is None:
        raise ValueError("Bounded blob download requires output_path")
    progress = _TransferProgress(digest, TransferDirection.DOWNLOAD, callback, total_size)
    if total_size is not None and total_size > max_bytes:
        raise BlobError("Blob exceeds max_bytes")
    size = 0
    with progress, _DownloadFile(Path(output_path), dir_fd) as output:
        with ClosingResponse(client.get(
            f"/v2/{repo}/blobs/{digest}", stream=True,
            headers={"Accept-Encoding": "identity"},
            response_hook=_bounded_blob_redirect,
        )) as response:
            if response.status_code == 206 or "Content-Range" in response.headers:
                raise BlobError("Partial blob responses are not supported")
            if not 200 <= response.status_code < 300:
                payload = bytearray()
                for chunk in entity_chunks(response, min(chunk_size, 65536)):
                    if len(payload) + len(chunk) > min(max_bytes, 65536):
                        raise BlobError("Registry error body exceeds max_bytes")
                    payload.extend(chunk)
                buffered = _BufferedManifestText(bytes(payload), response.encoding)
                buffered.status_code = response.status_code
                _raise_for_blob_error(buffered, client.config.registry, repo, digest)
            if total_size is None and callback is not None:
                progress.set_total(_download_total(response.headers))
            progress.emit(TransferPhase.TRANSFERRING)
            hasher = hashlib.new(algorithm)
            for chunk in entity_chunks(response, min(chunk_size, 65536, max_bytes + 1)):
                if size + len(chunk) > max_bytes:
                    raise BlobError("Blob exceeds max_bytes")
                progress.check_increment(len(chunk))
                output.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
                progress.advance(len(chunk))
            progress.check_complete()
            progress.emit(TransferPhase.VERIFYING)
            if f"{algorithm}:{hasher.hexdigest()}" != digest:
                raise BlobError("Blob digest verification failed")
            content_type = response.headers.get("Content-Type", _DEFAULT_CONTENT_TYPE)
            info = BlobInfo(digest=digest, content_type=content_type, size=size)
        output.verify_identity()
        output.close()
        progress.complete()
        output.verify_identity()
    return info


def _bounded_blob_redirect(response: requests.Response, **kwargs: object) -> requests.Response:
    """Bound intermediate redirect bodies before Requests eagerly drains them.

    Signed download redirects still use Requests' authorization-stripping policy.
    A bounded discarded body is cached as empty solely to prevent an unbounded
    second read by redirect handling; final entity bytes are never changed.
    """
    if 300 <= response.status_code < 400:
        with ClosingResponse(response):
            count = 0
            for chunk in entity_chunks(response, 65536):
                count += len(chunk)
                if count > 65536:
                    raise BlobError("Blob redirect body exceeds metadata limit")
        response._content = b""
        response._content_consumed = True  # type: ignore[attr-defined]  # Requests' redirect cache flag
    return response


@track_time
def delete_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
) -> None:
    """Delete a blob from the registry.

    Issues a ``DELETE /v2/{repo}/blobs/{digest}`` request.  Expects
    ``202 Accepted``; all other responses are mapped to errors by
    :func:`_raise_for_blob_error`.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param digest: Blob digest to delete (``"sha256:..."``).
    :raises AuthError: On authentication failure.
    :raises BlobError: On a non-2xx registry response.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    path = f"/v2/{repo}/blobs/{digest}"
    response = client.delete(path)
    _raise_for_blob_error(response, client.config.registry, repo, digest)


@track_scenario("blob upload")
def upload_blob(
    client: RegistryClient,
    repo: str,
    data: bytes,
    digest: str,
    content_type: str = _DEFAULT_CONTENT_TYPE,
) -> str:
    """Upload a blob using the monolithic (POST + PUT) protocol.

    Two distinct HTTP calls are made:

    1. ``POST /v2/{repo}/blobs/uploads/`` — initiates the upload session.
    2. ``PUT <upload-url>?digest={digest}`` — commits the content in one shot.

    The digest returned by the registry in the ``Docker-Content-Digest``
    response header is verified against *digest* before returning.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param data: Raw blob bytes to upload.
    :param digest: Expected content digest (``"sha256:..."``). Sent as a
        query parameter on the completing PUT.
    :param content_type: MIME type for the ``Content-Type`` header on the PUT
        (default ``"application/octet-stream"``).
    :returns: The confirmed digest string from the ``Docker-Content-Digest``
        response header.
    :raises AuthError: On authentication failure at either step.
    :raises BlobError: On a non-2xx response at any step, or a confirmed
        digest mismatch.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    registry = client.config.registry

    # --- Step 1: initiate upload session ---
    init_path = f"/v2/{repo}/blobs/uploads/"
    init_response = client.post(init_path)
    _raise_for_upload_error(init_response, registry, session_id=None)

    location = init_response.headers.get("Location", "")
    session = BlobUploadSession.from_location(location)

    # --- Step 2: PUT the full content ---
    _put_base, _put_params = _split_upload_path(session.upload_path)
    _put_params.append(("digest", digest))
    put_response = client.put(
        _put_base,
        data=data,
        params=_put_params,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        },
    )
    _raise_for_upload_error(put_response, registry, session_id=session.session_id)

    confirmed = put_response.headers.get("Docker-Content-Digest", "")
    if confirmed and confirmed != digest:
        raise BlobError(
            f"Digest mismatch: expected {digest}, registry confirmed {confirmed}",
            f"registry={registry} repo={repo}",
        )
    return confirmed or digest


@track_scenario("blob upload streamed")
def upload_blob_stream(
    client: RegistryClient,
    repo: str,
    source: BinaryIO,
    digest: str,
    content_type: str = _DEFAULT_CONTENT_TYPE,
    *,
    progress_callback: ProgressCallback | None = None,
    total_size: int | None = None,
) -> str:
    """Upload a monolithic body without buffering a file solely to report progress.

    The source is borrowed and never closed. Seekable sources are replayed from
    their initial offset for authentication, redirects, and configured retries.
    Nonseekable sources require an explicit size and cannot be replayed after
    consumption. Bytes observed while the HTTP client reads are unconfirmed
    until the final response/digest check succeeds.

    :param client: Authenticated registry transport.
    :param repo: Destination repository.
    :param source: Binary payload positioned at its intended start.
    :param digest: Expected digest, passed to the completing PUT.
    :param content_type: Payload media type.
    :param progress_callback: Optional synchronous immutable-event observer.
    :param total_size: Trusted payload size, inferred by seeking when omitted.
        For a nonseekable source this frames the payload without waiting for EOF.
    :returns: The registry-confirmed digest, or the supplied digest if absent.
    :raises BlobError: On size/replay/integrity or registry failures.
    :raises AuthError: On authentication failure.
    :raises requests.exceptions.RequestException: On transport failures.

    Owns each POST/PUT response, closing before success is reported and preserving
    primary transfer/observer failures if response cleanup also fails.
    """
    with _TransferProgress(
        digest, TransferDirection.UPLOAD, progress_callback, total_size,
    ) as progress:
        with _UploadBody(source, progress) as body:
            with ClosingResponse(client.post(f"/v2/{repo}/blobs/uploads/")) as response:
                _raise_for_upload_error(response, client.config.registry, session_id=None)
                session = BlobUploadSession.from_location(response.headers.get("Location", ""))
            path, params = _split_upload_path(session.upload_path)
            params.append(("digest", digest))
            with ClosingResponse(client.put(
                path, data=body, params=params,
                headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            )) as response:
                body.raise_if_failed()
                _raise_for_upload_error(response, client.config.registry, session_id=session.session_id)
                _require_upload_commit(response, "Streamed")
                if response.request is not None and len(body) and response.request.body is not body:
                    raise BlobError("Streamed upload redirect removed the counted request body")
                progress.check_complete()
                progress.emit(TransferPhase.VERIFYING)
                confirmed = response.headers.get("Docker-Content-Digest", "")
                if confirmed and confirmed != digest:
                    raise BlobError("Registry confirmed a different digest for the streamed upload")
        progress.complete()
        return confirmed or digest


@track_scenario("blob upload chunked")
def upload_blob_chunked(
    client: RegistryClient,
    repo: str,
    source: BinaryIO,
    digest: str,
    content_type: str = _DEFAULT_CONTENT_TYPE,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    *,
    progress_callback: ProgressCallback | None = None,
    total_size: int | None = None,
) -> str:
    """Upload a blob using the chunked (POST + N×PATCH + PUT) protocol.

    Three or more HTTP calls are made:

    1. ``POST /v2/{repo}/blobs/uploads/`` — initiates the upload session.
    2. N × ``PATCH <upload-url>`` — streams content in *chunk_size*-byte
       increments; each PATCH carries a ``Content-Range`` header. On
       each ``202`` response the session offset is advanced and the
       ``Location`` header (if present) is used for the next PATCH.
    3. ``PUT <upload-url>?digest={digest}`` — commits the upload with an
       empty body. Completion requires a final PUT with ``201 Created``.

    If *source* is exhausted immediately (zero-byte blob), the PATCH loop
    is skipped and only the completing PUT is issued.

    :param client: Authenticated transport client for the target registry.
    :param repo: Repository name.
    :param source: Open binary file-like object to read chunks from.
    :param digest: Expected content digest. Sent as a query parameter on the
        completing PUT and verified against the registry's confirmed digest.
    :param content_type: MIME type sent on the completing PUT.
    :param chunk_size: Chunk size in bytes (default ``65536``).
        Must be a positive integer.
    :param progress_callback: Optional observer of accepted-byte/lifecycle events.
        Buffered-chunk transport retries are counted only once after success.
        Callback errors propagate unchanged; the caller retains ownership of source.
    :param total_size: Optional trusted count of bytes remaining in source. No
        seeking or pre-reading is performed to discover it; ``None`` is unknown.
    :returns: The confirmed digest from ``Docker-Content-Digest``.
    :raises AuthError: On authentication failure at any step.
    :raises BlobError: On a non-2xx response, an offset mismatch (416), or
        a confirmed digest mismatch.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    _validate_chunk_size(chunk_size)
    with _TransferProgress(
        digest, TransferDirection.UPLOAD, progress_callback, total_size,
    ) as progress:
        result = _upload_blob_chunks(
            client, repo, source, digest, content_type, chunk_size, progress,
        )
        progress.complete()
        return result


def _upload_blob_chunks(
    client: RegistryClient,
    repo: str,
    source: BinaryIO,
    digest: str,
    content_type: str,
    chunk_size: int,
    progress: _TransferProgress,
) -> str:
    """Upload buffered chunks, advancing progress only after accepted responses.

    The caller owns source and the observer lifecycle. This helper preserves
    upload-session query values and performs final digest confirmation. Each
    response is closed before advancing success, without masking primary errors.
    """
    registry = client.config.registry

    # --- Step 1: initiate upload session ---
    init_path = f"/v2/{repo}/blobs/uploads/"
    with ClosingResponse(client.post(init_path)) as init_response:
        _raise_for_upload_error(init_response, registry, session_id=None)
        location = init_response.headers.get("Location", "")
        session = BlobUploadSession.from_location(location)
    progress.emit(TransferPhase.TRANSFERRING)

    # --- Step 2: PATCH loop ---
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        progress.check_increment(len(chunk))
        start = session.offset
        end = start + len(chunk) - 1
        with ClosingResponse(client.patch(
            session.upload_path,
            data=chunk,
            headers={
                "Content-Range": f"{start}-{end}/*",
                "Content-Length": str(len(chunk)),
                "Content-Type": "application/octet-stream",
            },
        )) as patch_response:
            _raise_for_upload_error(
                patch_response, registry, session_id=session.session_id
            )
            # A present rotated URL must be usable; do not silently reuse a stale one.
            new_location = patch_response.headers.get("Location", "")
            if new_location:
                updated = BlobUploadSession.from_location(new_location)
                session.upload_path = updated.upload_path
                session.session_id = updated.session_id
        session.offset += len(chunk)
        progress.advance(len(chunk))

    progress.check_complete()
    progress.emit(TransferPhase.COMMITTING)
    # --- Step 3: completing PUT ---
    _put_base, _put_params = _split_upload_path(session.upload_path)
    _put_params.append(("digest", digest))
    with ClosingResponse(client.put(
        _put_base,
        data=b"",
        params=_put_params,
        headers={
            "Content-Type": content_type,
            "Content-Length": "0",
        },
    )) as put_response:
        _raise_for_upload_error(put_response, registry, session_id=session.session_id)
        _require_upload_commit(put_response, "Chunked")
        progress.emit(TransferPhase.VERIFYING)
        confirmed = put_response.headers.get("Docker-Content-Digest", "")
        if confirmed and confirmed != digest:
            raise BlobError(
                f"Digest mismatch: expected {digest}, registry confirmed {confirmed}",
                f"registry={registry} repo={repo}",
            )
    return confirmed or digest


@track_time
def mount_blob(
    client: RegistryClient,
    repo: str,
    digest: str,
    from_repo: str,
) -> str:
    """Attempt to cross-repository mount a blob without a data transfer.

    Issues ``POST /v2/{repo}/blobs/uploads/?from={from_repo}&mount={digest}``.

    * ``201 Created`` — mount succeeded; returns the confirmed digest from
      ``Docker-Content-Digest``.
    * ``202 Accepted`` — the registry cannot perform the mount (unsupported
      feature or the source blob is inaccessible); raises :class:`BlobError`
      directing the caller to fall back to :func:`upload_blob` or
      :func:`upload_blob_chunked`.

    :param client: Authenticated transport client for the target registry.
    :param repo: Destination repository name.
    :param digest: Blob digest to mount (``"sha256:..."``).
    :param from_repo: Source repository name (without registry prefix).
    :returns: Confirmed digest string from the ``Docker-Content-Digest``
        response header.
    :raises AuthError: On authentication failure.
    :raises BlobError: If the registry returns ``202`` (mount not accepted)
        or any other non-2xx status.
    :raises requests.exceptions.RequestException: On transport errors.
    """
    registry = client.config.registry
    path = f"/v2/{repo}/blobs/uploads/"
    response = client.post(path, params={"from": from_repo, "mount": digest})

    if response.status_code == 202:
        raise BlobError(
            f"Blob mount not accepted for {registry}/{repo}@{digest}"
            f": registry returned 202 — retry with upload_blob or upload_blob_chunked",
            f"from_repo={from_repo}",
        )

    _raise_for_blob_error(response, registry, repo, digest)
    return response.headers.get("Docker-Content-Digest", digest)


# ===========================================================================
# Private helpers
# ===========================================================================


def _require_upload_commit(response: requests.Response, protocol: str) -> None:
    """Raise :class:`BlobError` unless the response confirms a final PUT with 201 Created."""
    if response.request is not None and response.request.method != "PUT":
        raise BlobError(f"{protocol} upload redirect changed the completing PUT method")
    if response.status_code != 201:
        raise BlobError(f"{protocol} upload completion requires HTTP 201 Created")


def _split_upload_path(upload_path: str) -> tuple[str, list[tuple[str, str]]]:
    """Split *upload_path* into a bare path and a list of (key, value) query params.

    Registries may embed required session tokens in the query string of the
    upload URL (e.g. ``?_state=...``).  Callers append ``digest`` to the
    returned list and pass both the bare path and params list to
    ``client.put(..., params=params)``, letting ``requests`` handle
    percent-encoding rather than building the URL manually.

    :param upload_path: Upload path from :attr:`BlobUploadSession.upload_path`,
        optionally containing a query string.
    :returns: ``(bare_path, [(key, value), ...])`` pair; *params* preserves
        the original ordering and allows duplicate keys.
    """
    parsed = urlparse(upload_path)
    return parsed.path, parse_qsl(parsed.query, keep_blank_values=True)


def _blob_info_from_response(
    response: requests.Response,
    fallback_digest: str,
) -> BlobInfo:
    """Construct a :class:`BlobInfo`, normalizing unusable length metadata to zero.

    :param response: A successful HEAD or GET response.
    :param fallback_digest: Digest to use when ``Docker-Content-Digest`` is
        absent from the response headers (should be the requested digest).
    :returns: A :class:`BlobInfo` instance.
    """
    digest = response.headers.get("Docker-Content-Digest", fallback_digest)
    content_type = response.headers.get("Content-Type", "application/octet-stream")
    try:
        size = max(0, int(response.headers.get("Content-Length", "0")))
    except ValueError:
        size = 0
    return BlobInfo(digest=digest, content_type=content_type, size=size)


def _stream_to_file(
    response: requests.Response,
    output_path: str,
    chunk_size: int,
    update_hash: Callable[[bytes], None],
    progress: _TransferProgress,
) -> None:
    """Stream a response body to a file, updating *hasher* as bytes arrive.

    :param response: A streaming GET response.
    :param output_path: Destination file path.
    :param chunk_size: Read/write chunk size in bytes.
    :param update_hash: Hash update function called for each nonempty chunk.
    :param progress: Invocation-local observer, advanced after writing each chunk.
    """
    fh = open(output_path, "wb")  # noqa: SIM115 - close must preserve the primary failure
    consumed = False
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                update_hash(chunk)
                fh.write(chunk)
                progress.advance(len(chunk))
        consumed = True
    finally:
        _close_download_resource(fh, failing=not consumed)


def _validate_chunk_size(chunk_size: int) -> None:
    """Reject zero/negative/noninteger reads before network access or file creation."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise BlobError("Blob chunk_size must be a positive integer")


def _close_download_resource(
    resource: BinaryIO | requests.Response, *, failing: bool,
) -> None:
    """Preserve this transfer's failure, not an unrelated caller exception context."""
    try:
        resource.close()
    except (OSError, requests.exceptions.RequestException) as exc:
        if not failing:
            raise
        logger.error(
            "Download resource cleanup failed (%s); preserving the original error",
            type(exc).__name__,
        )


def _raise_for_blob_error(
    response: requests.Response,
    registry: str,
    repo: str,
    digest: str,
) -> None:
    """Raise on non-2xx responses for blob content-read and delete operations.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 404 → :class:`~regshape.libs.errors.BlobError` "Blob not found"
    * 405 → :class:`~regshape.libs.errors.BlobError` "not supported"
    * other → :class:`~regshape.libs.errors.BlobError` generic

    :raises AuthError: On 401.
    :raises BlobError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 404:
        raise BlobError(
            f"Blob not found: {registry}/{repo}@{digest}",
            detail or "HTTP 404",
            status_code=404,
        )
    if response.status_code == 405:
        raise BlobError(
            "Operation not supported by this registry",
            detail or "HTTP 405",
            status_code=405,
        )
    raise BlobError(
        f"Registry error for {registry}/{repo}",
        detail or f"HTTP {response.status_code}",
        status_code=response.status_code,
    )


def _raise_for_upload_error(
    response: requests.Response,
    registry: str,
    session_id: Optional[str],
) -> None:
    """Raise on non-2xx responses during a blob upload workflow.

    Used for POST (initiate), PATCH (chunks), and PUT (completing) stages.

    * 401 → :class:`~regshape.libs.errors.AuthError`
    * 400 → :class:`~regshape.libs.errors.BlobError` "Invalid upload"
    * 404 → :class:`~regshape.libs.errors.BlobError` "Upload session not found"
    * 416 → :class:`~regshape.libs.errors.BlobError` "Offset mismatch"
    * other → :class:`~regshape.libs.errors.BlobError` generic

    :raises AuthError: On 401.
    :raises BlobError: On all other non-2xx status codes.
    """
    if 200 <= response.status_code < 300:
        return

    detail = (
        OciErrorResponse.from_response(response).first_detail() or response.text[:200]
    )

    if response.status_code == 401:
        raise AuthError(
            f"Authentication failed for {registry}",
            detail or "HTTP 401",
        )
    if response.status_code == 400:
        raise BlobError(
            f"Invalid upload: {detail or 'HTTP 400'}",
            f"registry={registry}",
        )
    if response.status_code == 404:
        sid = session_id or "unknown"
        raise BlobError(
            f"Upload session not found: {sid}",
            detail or "HTTP 404",
        )
    if response.status_code == 416:
        raise BlobError(
            "Offset mismatch during chunked upload",
            detail or "HTTP 416",
        )
    raise BlobError(
        "Registry error during blob upload",
        detail or f"HTTP {response.status_code}",
    )

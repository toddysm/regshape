"""Receive manifest entity bytes into an exclusive file without decoding or model serialization.

This primitive verifies byte identity and size, not manifest schemas or graph
completeness. The caller supplies a trusted, existing parent directory and owns
the returned file. Clients are borrowed; responses and output handles are owned.
"""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, final

import requests

from regshape.libs._http import (
    ClosingResponse,
    download_total,
    report_secondary_failure,
)
from regshape.libs.decorators.timing import track_time
from regshape.libs.errors import ManifestError
from regshape.libs.manifests.operations import (
    _BufferedManifestText,
    _manifest_kind,
    _raise_for_manifest_error,
)
from regshape.libs.progress import (
    ManifestPhase,
    ManifestProgressCallback,
    _ManifestProgress,
)
from regshape.libs.transport import RegistryClient

_CHUNK_SIZE = 64 * 1024
_DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r"(sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128})")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_REPO = re.compile(
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
)


@dataclass(frozen=True)
class ManifestDownload:
    """Verified raw receive result, without a model-valid or graph-complete claim.

    :param path: Caller-selected output filename; the caller owns it after return.
    :param digest: Verified requested digest, then expected digest, reported digest,
        or computed SHA-256, in that priority order.
    :param size: Number of entity bytes written, after HTTP content-coding removal.
    :param media_type: Content-Type header as received, empty when absent.
    :param reported_digest: Verified Docker-Content-Digest header, empty when absent.
    """

    path: str
    digest: str
    size: int
    media_type: str
    reported_digest: str


def _digest_algorithm(value: str) -> str:
    """Validate a strict supported content address before using its algorithm."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("Digest must be lowercase sha256:64-hex or sha512:128-hex")
    return value.partition(":")[0]


def _validate_inputs(
    repo: str,
    reference: str,
    accept: str,
    expected_digest: str | None,
    expected_size: int | None,
    max_bytes: int,
) -> str | None:
    """Reject invalid or contradictory inputs without file or network side effects."""
    if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
        raise ValueError(
            "Repository must contain valid relative OCI repository components"
        )
    if not isinstance(reference, str):
        raise ValueError("Reference must be a tag or supported digest")  # noqa: TRY004 - input contract
    requested = None
    if ":" in reference:
        _digest_algorithm(reference)
        requested = reference
    elif _TAG.fullmatch(reference) is None:
        raise ValueError("Reference must be a valid registry tag or supported digest")
    if (
        not isinstance(accept, str)
        or not accept
        or not accept.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in accept)
    ):
        raise ValueError(
            "Accept must be a nonempty ASCII header value without controls"
        )
    if expected_digest is not None:
        _digest_algorithm(expected_digest)
        if (
            requested is not None
            and requested.partition(":")[0] == expected_digest.partition(":")[0]
            and requested != expected_digest
        ):
            raise ValueError("Requested and expected manifest digests disagree")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if expected_size is not None:
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ValueError("expected_size must be a nonnegative integer or None")
        if expected_size > max_bytes:
            raise ManifestError("Expected manifest size exceeds max_bytes")
    return requested


@final
class _DownloadFile:
    """Own one exclusively created file in a caller-controlled parent directory.

    Remove failed output only if its directory entry still identifies our file.
    This is not a sandbox for an adversary controlling parent directories; graph
    orchestration must provide its own safe staging/publication boundary.
    """

    def __init__(self, path: Path) -> None:
        """Retain the filename without opening or creating any parent directory."""
        self.path = path
        self.file: BinaryIO | None = None
        self.identity: os.stat_result | None = None
        self.close_attempted = False

    def __enter__(self) -> "_DownloadFile":
        """Create without replacing any existing file, directory or symlink."""
        self.file = self.path.open("xb")
        try:
            self.identity = os.fstat(self.file.fileno())
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:  # noqa: BLE001 - preserve the active acquisition error
                report_secondary_failure(primary, cleanup, "Manifest output close")
            report_secondary_failure(
                primary,
                primary,
                "Manifest output identity lookup; newly created file retained",
            )
            raise
        return self

    def write(self, chunk: bytes) -> None:
        """Require complete writes; a short write is a failed receive, not success."""
        if self.file is None:
            raise RuntimeError("Manifest output is not open")
        if self.file.write(chunk) != len(chunk):
            raise OSError("Short write while saving manifest")

    def close(self) -> None:
        """Flush/close at most once so receipt cannot precede file finalization."""
        if self.file is not None and not self.close_attempted:
            self.close_attempted = True
            self.file.close()

    def verify_identity(self) -> None:
        """Reject a missing/replaced output entry without touching its replacement."""
        current = self.path.lstat()
        if self.identity is None or not os.path.samestat(current, self.identity):
            raise OSError("Manifest output was replaced; refusing to use or remove it")

    def _remove_failed(self) -> None:
        """Remove only our failed entry; report missing/replaced output explicitly."""
        self.verify_identity()
        self.path.unlink()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Close and remove unsuccessful output while preserving a primary failure."""
        close_error: BaseException | None = None
        try:
            self.close()
        except BaseException as error:  # noqa: BLE001 - clean owned output even on interruption
            close_error = error
        primary = exc if exc is not None else close_error
        if primary is not None:
            try:
                self._remove_failed()
            except BaseException as cleanup:  # noqa: BLE001 - secondary failure must not mask primary
                report_secondary_failure(primary, cleanup, "Manifest output cleanup")
            finally:
                if close_error is not None and exc is not None:
                    report_secondary_failure(exc, close_error, "Manifest output close")
            if exc is None:
                raise primary
        return False


def _raise_for_download_error(
    response: requests.Response,
    client: RegistryClient,
    repo: str,
    reference: str,
    max_bytes: int,
) -> None:
    """Bound error-body parsing without swallowing the first transport read failure."""
    if 200 <= response.status_code < 300:
        return
    cap = min(max_bytes, _CHUNK_SIZE)
    payload = bytearray()
    for chunk in response.iter_content(chunk_size=min(_CHUNK_SIZE, cap + 1)):
        if len(payload) + len(chunk) > cap:
            raise ManifestError(
                "Registry error body exceeds the manifest error-body limit"
            )
        payload.extend(chunk)
    buffered = _BufferedManifestText(bytes(payload), response.encoding)
    buffered.status_code = response.status_code
    _raise_for_manifest_error(buffered, client.config.registry, repo, reference)


@track_time
def download_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
    output_path: str | Path,
    *,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    progress_callback: ManifestProgressCallback | None = None,
) -> ManifestDownload:
    """Stream exact manifest entity bytes to a new file and verify identity/size.

    :param client: Borrowed registry client; existing auth/retry policies apply.
    :param repo: Relative repository name, without URL syntax or traversal.
    :param reference: Registry tag or strict SHA-256/SHA-512 digest.
    :param accept: Nonempty ASCII Accept header without control characters.
    :param output_path: New filename in a trusted, existing caller-controlled
        parent directory. Existing entries are never overwritten. The caller must
        exclude concurrent writes or renames of the parent/output during receipt.
    :param expected_digest: Additional descriptor digest to verify, when supplied.
    :param expected_size: Strict expected entity byte count, not an HTTP size hint.
    :param max_bytes: Positive payload cap; default 16 MiB. Error bodies also have
        a 64 KiB cap. Reads request at most 64 KiB and never buffer a whole success body.
    :param progress_callback: Optional synchronous manifest observer. RECEIVED
        follows verification and response/file close, not JSON/schema validation.
    :returns: Frozen path/identity/size/header metadata; caller owns the saved file.
    :raises ValueError: On invalid inputs, before file/network activity.
    :raises TypeError: If the observer is not callable.
    :raises ManifestError: On digest/size/cap/registry failures.
    :raises OSError: On exclusive creation or output failure.

    Auth/transport and observer errors propagate unchanged. Failed output is
    removed where safe; cleanup failures are logged without replacing the primary
    error. A failing RECEIVED observer also removes the owned file. No extra
    HEAD, root refetch, charset conversion, model parsing or body resume occurs.
    """
    progress = _ManifestProgress(progress_callback)
    requested = _validate_inputs(
        repo, reference, accept, expected_digest, expected_size, max_bytes
    )
    path = Path(output_path)
    with progress, _DownloadFile(path) as output:
        with ClosingResponse(
            client.get(
                f"/v2/{repo}/manifests/{reference}",
                headers={"Accept": accept},
                stream=True,
            )
        ) as response:
            _raise_for_download_error(response, client, repo, reference, max_bytes)
            reported = response.headers.get("Docker-Content-Digest", "")
            if "Docker-Content-Digest" in response.headers:
                try:
                    _digest_algorithm(reported)
                except ValueError as error:
                    raise ManifestError(
                        "Invalid Docker-Content-Digest response header"
                    ) from error
            expected = [
                digest for digest in (requested, expected_digest, reported) if digest
            ]
            algorithms = {_digest_algorithm(digest) for digest in expected} or {
                "sha256"
            }
            hashers = {algorithm: hashlib.new(algorithm) for algorithm in algorithms}
            media_type = response.headers.get("Content-Type", "")
            progress.kind = _manifest_kind(media_type)
            progress.total_bytes = (
                expected_size
                if expected_size is not None
                else download_total(response.headers)
            )
            progress.emit(ManifestPhase.TRANSFERRING)
            for chunk in response.iter_content(
                chunk_size=min(_CHUNK_SIZE, max_bytes + 1)
            ):
                if not chunk:
                    continue
                progress.advance(len(chunk))
                if progress.bytes_completed > max_bytes:
                    raise ManifestError("Manifest exceeds max_bytes")
                if (
                    expected_size is not None
                    and progress.bytes_completed > expected_size
                ):
                    raise ManifestError("Manifest exceeds expected_size")
                output.write(chunk)
                for hasher in hashers.values():
                    hasher.update(chunk)
            size = progress.bytes_completed
            if expected_size is not None and size != expected_size:
                raise ManifestError("Manifest size does not match expected_size")
            progress.finish_body()
            computed = {
                algorithm: f"{algorithm}:{hasher.hexdigest()}"
                for algorithm, hasher in hashers.items()
            }
            for digest in expected:
                if computed[_digest_algorithm(digest)] != digest:
                    raise ManifestError("Manifest digest verification failed")
            digest = requested or expected_digest or reported or computed["sha256"]
        output.verify_identity()
        output.close()
        progress.emit(ManifestPhase.RECEIVED)
        output.verify_identity()
        return ManifestDownload(str(path), digest, size, media_type, reported)

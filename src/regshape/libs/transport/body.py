"""Length-aware upload bodies with explicit replay and observer-error boundaries."""

import io
import sys
from typing import BinaryIO

from regshape.libs.errors import BlobError
from regshape.libs.progress import TransferPhase, _TransferProgress


class _UploadBody(io.RawIOBase):
    """Borrow a binary source without closing it; replay from its initial offset.

    Known-length nonseekable sources may be sent once. A replay after consumption
    fails before another request is dispatched. Read/observer errors are retained
    so Requests wrapping them cannot accidentally make them eligible for retry.
    """

    def __init__(self, source: BinaryIO, progress: _TransferProgress) -> None:
        """Discover remaining length by seeking, or require an explicit trusted total."""
        super().__init__()
        self.source = source
        self.progress = progress
        self.position = 0
        self.started = False
        self._failure: BaseException | None = None
        self._eof_checked = False
        self.origin: int | None = None
        size = progress.total_bytes
        if source.seekable():
            self.origin = source.tell()
            try:
                end = source.seek(0, io.SEEK_END)
            finally:
                source.seek(self.origin)
            remaining = end - self.origin
            if remaining < 0 or (size is not None and size != remaining):
                raise BlobError("Upload total_size does not match the remaining source")
            size = remaining
        if size is None:
            raise BlobError("Nonseekable uploads require an explicit total_size")
        self.size = size
        progress.set_total(size)

    def __len__(self) -> int:
        """Provide Content-Length without reading the payload."""
        return self.size

    def readable(self) -> bool:
        """Advertise a read-only request body."""
        return True

    def seekable(self) -> bool:
        """Expose whether Requests can safely rewind a consumed redirect body."""
        return self.origin is not None

    def tell(self) -> int:
        """Return the attempt-relative position used by Requests redirect replay."""
        return self.position

    def raise_if_failed(self) -> None:
        """Restore an original source/observer failure instead of retrying its HTTP wrapper."""
        if self._failure is not None:
            raise self._failure

    def prepare_attempt(self) -> None:
        """Begin a dispatch or rewind an existing attempt before any new request."""
        self.raise_if_failed()
        succeeded = False
        try:
            if self.closed:
                raise BlobError("Upload body is closed")
            if self.started:
                if self.origin is None and self.position:
                    raise BlobError("Cannot replay a consumed nonseekable upload source")
                if self.origin is not None:
                    self.source.seek(self.origin)
                self.position = 0
                self._eof_checked = False
                self.progress.retry()
            else:
                self.started = True
            self.progress.emit(TransferPhase.TRANSFERRING)
            if self.size == 0:
                self._check_eof()
                self.progress.emit(TransferPhase.COMMITTING)
            succeeded = True
        finally:
            if not succeeded:
                self._failure = sys.exc_info()[1]

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Support only a complete rewind, preserving the original source offset."""
        if offset != 0 or whence != io.SEEK_SET:
            raise io.UnsupportedOperation("Upload bodies support only rewind to zero")
        self.prepare_attempt()
        return 0

    def _check_eof(self) -> None:
        """Check file growth without reading beyond a nonseekable framing boundary."""
        if self.origin is None:
            return
        if not self._eof_checked:
            extra = self.source.read(1)
            if not isinstance(extra, bytes):
                raise BlobError("Upload sources must return bytes, including EOF")
            if extra:
                raise BlobError("Upload source exceeds its trusted total_size")
            self._eof_checked = True

    def read(self, size: int = -1) -> bytes:
        """Read only the requested remaining payload and observe bytes handed to the client."""
        self.raise_if_failed()
        succeeded = False
        try:
            if self.closed:
                raise BlobError("Upload body is closed")
            if not self.started:
                self.prepare_attempt()
            remaining = self.size - self.position
            if not remaining or size == 0:
                result = b""
            else:
                amount = remaining if size < 0 else min(size, remaining)
                result = self.source.read(amount)
                if not isinstance(result, bytes):
                    raise BlobError("Upload sources must return bytes")
                if not result:
                    raise BlobError("Upload source ended before its trusted total_size")
                if len(result) > amount:
                    raise BlobError("Upload source returned more bytes than requested")
                self.position += len(result)
                if self.position == self.size:
                    self._check_eof()
                self.progress.advance(len(result))
                if self.position == self.size:
                    self.progress.emit(TransferPhase.COMMITTING)
            succeeded = True
            return result
        finally:
            if not succeeded:
                self._failure = sys.exc_info()[1]

"""Bound raw manifest entity decoding independently of urllib3 decoder buffering.

Redirect bodies are rejected in a Requests response hook before Session can
consume them eagerly. Ordinary registry operations retain their existing policy.
"""

import zlib
from collections.abc import Iterator

import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError
from urllib3.exceptions import SSLError as Urllib3SSLError

from regshape.libs._http import close_response
from regshape.libs.errors import ManifestError


def reject_redirect(response: requests.Response, **kwargs: object) -> requests.Response:
    """Close/reject redirects before Requests' automatic follow or next-request preparation."""
    if 300 <= response.status_code < 400:
        error = ManifestError("Raw manifest downloads do not follow redirects")
        close_response(response, primary_error=error)
        raise error
    return response


def _wire_chunks(response: requests.Response, chunk_size: int) -> Iterator[bytes]:
    """Read encoded bytes without urllib3 decoding, retaining Requests' exception taxonomy."""
    try:
        yield from response.raw.stream(chunk_size, decode_content=False)
    except ProtocolError as error:
        raise requests.exceptions.ChunkedEncodingError(error) from error
    except ReadTimeoutError as error:
        raise requests.exceptions.ConnectionError(error) from error
    except Urllib3SSLError as error:
        raise requests.exceptions.SSLError(error) from error


def entity_chunks(response: requests.Response, chunk_size: int) -> Iterator[bytes]:
    """Yield identity/gzip/deflate bytes in bounded pieces, validating encoded EOF.

    zlib's max_length bounds each allocation even with urllib3 1.26. Gzip members
    may be concatenated; invalid trailing data or incomplete streams fail.
    Unsupported/stacked encodings fail explicitly rather than delegating to an
    installed optional decoder with unknown allocation behavior.
    """
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding in ("", "identity"):
        yield from _wire_chunks(response, chunk_size)
        return
    if encoding not in ("gzip", "x-gzip", "deflate"):
        raise requests.exceptions.ContentDecodingError(
            "Raw manifest downloads support only identity, gzip or deflate content encoding"
        )
    is_gzip = encoding != "deflate"
    decoder = None
    prefix = b""
    finished = False
    for data in _wire_chunks(response, chunk_size):
        if not data:
            continue
        if decoder is None and not is_gzip:
            prefix += data
            if len(prefix) < 2:
                continue
            # HTTP deflate is zlib-wrapped; tolerate the established raw-deflate variant.
            wrapped = (
                prefix[0] & 15 == 8 and int.from_bytes(prefix[:2], "big") % 31 == 0
            )
            decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            data, prefix = prefix, b""
        while True:
            if decoder is None:
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                finished = False
            try:
                decoded = decoder.decompress(data, chunk_size)
            except zlib.error as error:
                raise requests.exceptions.ContentDecodingError(
                    "Invalid manifest content encoding"
                ) from error
            data = decoder.unconsumed_tail
            if decoded:
                yield decoded
            if decoder.eof:
                finished = True
                data = decoder.unused_data
                if not is_gzip:
                    if data:
                        raise requests.exceptions.ContentDecodingError(
                            "Trailing bytes after deflate stream"
                        )
                    break
                decoder = None
                if not data:
                    break
            elif not data and len(decoded) < chunk_size:
                break
    if not finished:
        raise requests.exceptions.ContentDecodingError(
            "Incomplete manifest content encoding"
        )

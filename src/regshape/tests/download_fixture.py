"""Serve byte-exact loopback downloads for stress tests and maintainer benchmarks.

Only ephemeral loopback listeners and synthetic authentication are used. Shaping
is an application-level per-connection approximation, not a network emulator.
The caller owns payload memory and must exit the fixture after downloads stop.
"""

import json
import socket
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Literal
from urllib.parse import urlsplit


class DownloadFixture:
    """Own framed HTTP/1.1 registry/storage listeners and synchronized observations.

    :param objects: Immutable digest-to-stored-bytes fixture contents.
    :param capability: Strong-validator ranges, ignored ranges, or missing ETags.
    :param authenticated: Exercise discovery, a separate /v2/ probe and one token.
    :param redirected: Redirect every authenticated blob read to the storage origin.
    :param latency: Seconds before each data response; zero disables RTT shaping.
    :param bandwidth: Bytes/second per data connection; zero disables pacing.
    :param overlap: Barrier size proving initial data-request overlap without clocks.
    :param gate_later_ranges: Exclude negotiation range zero from that barrier.

    Observations contain no authorization values or authenticated URLs. Instances
    are single-use context managers; reset between completed benchmark samples.
    """

    def __init__(
        self,
        objects: dict[str, bytes],
        *,
        capability: Literal["ranges", "ignored", "missing-etag"] = "ranges",
        authenticated: bool = False,
        redirected: bool = False,
        latency: float = 0,
        bandwidth: int = 0,
        overlap: int = 1,
        gate_later_ranges: bool = False,
    ) -> None:
        """Retain payloads and policies without binding sockets or starting threads."""
        self.objects = objects
        self.capability = capability
        self.authenticated, self.redirected = authenticated, redirected
        self.latency, self.bandwidth = latency, bandwidth
        self.gate_later_ranges = gate_later_ranges
        self.condition = threading.Condition()
        self.barrier = threading.Barrier(overlap)
        self.stack = ExitStack()
        self.calls: Counter[str] = Counter()
        self.intervals: list[tuple[str, str | None]] = []
        self.ports: set[tuple[str, int]] = set()
        self.data_ports: set[int] = set()
        self.foreign_authorization = False
        self.active = self.peak = self.started = self.sent = 0
        self.errors: list[BaseException] = []
        self.authority = self.storage_authority = ""

    def __enter__(self) -> "DownloadFixture":  # noqa: PYI034 - Python 3.10 has no typing.Self.
        """Start both origins; unwind already-started listeners if setup fails."""
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            """Serve deterministic discovery/redirects and bounded slices of stored bytes."""

            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                """Disable delayed small writes so localhost TCP policy does not shape results."""
                super().setup()
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def log_message(self, *args: object) -> None:
                """Keep synthetic access logs and token-like values out of persisted evidence."""

            def handle(self) -> None:
                """Tolerate client-discarded negotiation bodies, but retain other handler errors."""
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass

            def reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
                """Send one framed non-data response without retaining a streaming body."""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self) -> None:
                """Dispatch synthetic auth/probes/redirects, then serve exact data intervals."""
                path = urlsplit(self.path).path
                storage = path.startswith("/storage/")
                kind = (
                    "storage"
                    if storage
                    else (
                        "token"
                        if path == "/token"
                        else "probe"
                        if path == "/v2/"
                        else "blob"
                    )
                )
                with fixture.condition:
                    fixture.calls[kind] += 1
                    fixture.ports.add((kind, self.client_address[1]))
                    if storage and self.headers.get("Authorization") is not None:
                        fixture.foreign_authorization = True
                if path == "/token":
                    self.reply(
                        200,
                        json.dumps(
                            {
                                "token": "synthetic-validation-token",
                                "expires_in": 3600,
                            }
                        ).encode(),
                        {"Content-Type": "application/json"},
                    )
                    return
                if (
                    not storage
                    and fixture.authenticated
                    and (
                        path == "/v2/"
                        or self.headers.get("Authorization")
                        != "Bearer synthetic-validation-token"
                    )
                ):
                    with fixture.condition:
                        fixture.calls["unauthorized"] += 1
                    # The first blob rejection intentionally requires a /v2/ probe.
                    challenge = (
                        {
                            "WWW-Authenticate": (
                                f'Bearer realm="http://{fixture.authority}/token",'
                                'service="validation",scope="repository:repo:pull"'
                            ),
                        }
                        if path == "/v2/"
                        else {}
                    )
                    self.reply(401, b"", challenge)
                    return
                digest = path.rsplit("/", 1)[-1]
                if digest not in fixture.objects:
                    self.reply(404, b"", {})
                    return
                if not storage and fixture.redirected:
                    self.reply(
                        307,
                        b"",
                        {
                            "Location": f"http://{fixture.storage_authority}/storage/{digest}",
                        },
                    )
                    return
                self.data(digest)

            def data(self, digest: str) -> None:
                """Serve a bounded memoryview, optionally barrier-gated or bandwidth-paced."""
                payload = fixture.objects[digest]
                interval = self.headers.get("Range")
                ranged = interval is not None and fixture.capability != "ignored"
                start, end = 0, len(payload) - 1
                if ranged and interval is not None:
                    start, end = map(int, interval.removeprefix("bytes=").split("-"))
                    end = min(end, len(payload) - 1)
                body = memoryview(payload)[start : end + 1]
                qualifies = not fixture.gate_later_ranges or ranged and start > 0
                with fixture.condition:
                    fixture.intervals.append((digest, interval))
                    fixture.data_ports.add(self.client_address[1])
                    fixture.active += 1
                    fixture.peak = max(fixture.peak, fixture.active)
                    if qualifies:
                        fixture.started += 1
                    gate = qualifies and fixture.started <= fixture.barrier.parties
                try:
                    if fixture.latency:
                        time.sleep(fixture.latency)
                    self.send_response(206 if ranged else 200)
                    self.send_header("Content-Length", str(len(body)))
                    if ranged:
                        self.send_header(
                            "Content-Range", f"bytes {start}-{end}/{len(payload)}"
                        )
                    if fixture.capability != "missing-etag":
                        self.send_header("ETag", '"stable"')
                    self.end_headers()
                    if gate:
                        fixture.barrier.wait(timeout=10)
                    began = time.perf_counter()
                    for offset in range(0, len(body), 65_536):
                        chunk = body[offset : offset + 65_536]
                        if fixture.bandwidth:
                            delay = min(
                                offset + len(chunk), len(body)
                            ) / fixture.bandwidth - (time.perf_counter() - began)
                            if delay > 0:
                                time.sleep(delay)
                        self.wfile.write(chunk)
                        with fixture.condition:
                            fixture.sent += len(chunk)
                    self.wfile.flush()
                finally:
                    with fixture.condition:
                        fixture.active -= 1
                        fixture.condition.notify_all()

        class Server(ThreadingHTTPServer):
            """Retain unexpected handler failures instead of losing errors on daemon threads."""

            def handle_error(
                self,
                request: socket.socket | tuple[bytes, socket.socket],
                client_address: tuple,
            ) -> None:
                """Make a handler exception fail fixture teardown on the invoking thread."""
                error = sys.exc_info()[1]
                if error is not None:
                    with fixture.condition:
                        fixture.errors.append(error)

        try:
            for name in ("storage_authority", "authority"):
                server = Server(("127.0.0.1", 0), Handler)
                self.stack.callback(server.server_close)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                self.stack.callback(thread.join, 5)
                self.stack.callback(server.shutdown)
                setattr(self, name, f"127.0.0.1:{server.server_port}")
        except BaseException:
            self.stack.close()
            raise
        return self

    def reset(self) -> None:
        """Clear observations only after handlers stop; never reset an active barrier."""
        with self.condition:
            if not self.condition.wait_for(lambda: self.active == 0, timeout=10):
                raise RuntimeError("Download fixture still has active data handlers")
            if self.errors:
                raise RuntimeError("Download fixture handler failed") from self.errors[
                    0
                ]
            self.calls.clear()
            self.intervals.clear()
            self.ports.clear()
            self.data_ports.clear()
            self.foreign_authorization = False
            self.peak = self.started = self.sent = 0
            self.barrier.reset()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release barrier waiters and join listeners; surface unexpected server failures."""
        self.barrier.abort()
        self.stack.close()
        if self.errors and exc is None:
            raise RuntimeError("Download fixture handler failed") from self.errors[0]

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
from urllib.parse import quote, urlsplit


def _header_value(value: str) -> str:
    """Reject line breaks instead of silently repairing an invalid fixture header."""
    single_line = value.replace("\r", "").replace("\n", "")
    if single_line != value:
        raise ValueError("Invalid fixture response header")
    return single_line


class DownloadFixture:
    """Own framed HTTP/1.1 registry/storage listeners and synchronized observations.

    :param objects: Immutable digest-to-stored-bytes fixture contents.
    :param capability: Strong-validator ranges, ignored ranges, or missing ETags.
    :param authenticated: Exercise discovery, a /v2/ probe and one token per scope.
    :param redirected: Redirect every authenticated blob read to the storage origin.
    :param latency: Seconds before each data response; zero disables RTT shaping.
    :param bandwidth: Bytes/second per data connection; zero disables pacing.
    :param overlap: Barrier size proving initial data-request overlap without clocks.
    :param gate_later_ranges: Exclude negotiation range zero from that barrier.

    Observations contain no authorization values or authenticated URLs. Instances
    are single-use context managers; reset between completed benchmark samples.
    ``requests_peak`` independently includes control and data handlers until
    their final write starts. Tests may disable ``gating`` while priming authentication,
    then select ``control_kind`` to rendezvous one control hop with data bodies.
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
        self.requests_active = self.requests_peak = 0
        self.active_kinds: Counter[str] = Counter()
        self.control_kind: str | None = None
        self.control_overlaps: set[str] = set()
        self.control_release: threading.Event | None = None
        self.gating = True
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
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass

            def reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
                """Send one framed non-data response without retaining a streaming body."""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, _header_value(value))
                if not body:
                    self.release_observed()
                self.end_headers()
                if body:
                    self.wfile.write(body[:-1])
                    self.release_observed()
                    self.wfile.write(body[-1:])
                self.wfile.flush()

            def do_GET(self) -> None:
                """Independently count every request until its final framing/body write.

                Release immediately before sending the last bytes so a client
                cannot start its successor before the old handler decrements.
                This measures server work, not the SDK's own lease counter.
                """
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
                self.observed_kind = kind
                self.observed_open = True
                with fixture.condition:
                    fixture.requests_active += 1
                    fixture.active_kinds[kind] += 1
                    fixture.requests_peak = max(
                        fixture.requests_peak, fixture.requests_active
                    )
                    fixture.calls[kind] += 1
                    fixture.ports.add((kind, self.client_address[1]))
                    if storage and self.headers.get("Authorization") is not None:
                        fixture.foreign_authorization = True
                try:
                    self.dispatch(path, storage)
                finally:
                    self.release_observed()

            def release_observed(self) -> None:
                """Release a server-side observation once, including failed writes."""
                if self.observed_open:
                    self.observed_open = False
                    with fixture.condition:
                        fixture.requests_active -= 1
                        fixture.active_kinds[self.observed_kind] -= 1
                        fixture.condition.notify_all()

            def gate_control(self, kind: str) -> None:
                """Hold a selected control hop until three other data bodies are active."""
                if not fixture.gating or fixture.control_kind != kind:
                    return
                with fixture.condition:
                    ready = fixture.condition.wait_for(
                        lambda: fixture.started >= fixture.barrier.parties - 1,
                        timeout=10,
                    )
                    if not ready:
                        raise RuntimeError(
                            "Data requests did not overlap the control hop"
                        )
                    fixture.control_overlaps.update(
                        key for key, value in fixture.active_kinds.items() if value
                    )
                    fixture.condition.notify_all()
                if (
                    fixture.control_release is not None
                    and not fixture.control_release.wait(10)
                ):
                    raise RuntimeError("Control hop was not released")
                fixture.barrier.wait(timeout=10)

            def dispatch(self, path: str, storage: bool) -> None:
                """Serve a synthetic auth/redirect hop or delegate the actual data response."""
                if path == "/token":
                    self.gate_control("token")
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
                    if path == "/v2/":
                        self.gate_control("probe")
                    with fixture.condition:
                        fixture.calls["unauthorized"] += 1
                    # The first blob rejection intentionally requires a /v2/ probe.
                    challenge = (
                        {
                            "WWW-Authenticate": (
                                f'Bearer realm="http://{fixture.authority}/token",'
                                'service="validation"'
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
                    if path.startswith("/v2/other/blobs/"):
                        self.gate_control("redirect")
                    self.reply(
                        307,
                        b"",
                        {
                            "Location": f"http://{fixture.storage_authority}/storage/{quote(digest, safe=':')}",
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
                        fixture.condition.notify_all()
                    parties = fixture.barrier.parties - int(
                        fixture.control_kind is not None
                    )
                    gate = fixture.gating and qualifies and fixture.started <= parties
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
                    if not body:
                        self.release_observed()
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
                        if offset + len(chunk) == len(body):
                            self.release_observed()
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

    def wait_idle(self) -> None:
        """Wait for data writes/counters and observed requests; surface delayed handler errors."""
        with self.condition:
            if not self.condition.wait_for(
                lambda: self.active == 0 and self.requests_active == 0,
                timeout=10,
            ):
                raise RuntimeError("Download fixture still has active request handlers")
            if self.errors:
                raise RuntimeError("Download fixture handler failed") from self.errors[
                    0
                ]

    def reset(self) -> None:
        """Clear observations only after handlers stop; never reset an active barrier."""
        with self.condition:
            self.wait_idle()
            self.calls.clear()
            self.intervals.clear()
            self.ports.clear()
            self.data_ports.clear()
            self.foreign_authorization = False
            self.requests_peak = 0
            self.control_overlaps.clear()
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

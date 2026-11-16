#!/usr/bin/env python3

"""
:mod: `metrics` - Aggregate performance metrics for telemetry
==============================================================

    module:: metrics
    :platform: Unix, Windows
    :synopsis: Provides :class:`PerformanceMetrics` for collecting aggregate
               HTTP performance data during a command invocation.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


metrics_collector: ContextVar["PerformanceMetrics | None"] = ContextVar(
    "regshape_metrics_collector", default=None,
)


@dataclass
class PerformanceMetrics:
    """Aggregate metrics collected during a command invocation.

    :param total_requests: Total number of HTTP requests made.
    :param total_bytes_sent: Total request body bytes sent.
    :param total_bytes_received: Total response body bytes received.
    :param retries: Number of retried requests (e.g. 401 re-auth).
    :param errors: Number of requests that resulted in 4xx/5xx status codes.
    :param status_code_counts: Counter of status codes seen.
    :param total_elapsed: Wall-clock time for all HTTP calls combined.
    """
    total_requests: int = 0
    total_bytes_sent: int = 0
    total_bytes_received: int = 0
    retries: int = 0
    errors: int = 0
    status_code_counts: dict[int, int] = field(default_factory=dict)
    total_elapsed: float = 0.0

    def record_request(
        self,
        status_code: int,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        elapsed: float = 0.0,
        is_retry: bool = False,
    ) -> None:
        """Record a single HTTP request into the aggregated metrics.

        :param status_code: HTTP response status code.
        :param bytes_sent: Request body size in bytes.
        :param bytes_received: Response body size in bytes.
        :param elapsed: Elapsed time in seconds for this request.
        :param is_retry: Whether this request was a retry.
        :raises ValueError: In worker collection, for a status outside 100..599;
            this bounds immutable status-count records without changing ordinary callers.
        """
        collector = metrics_collector.get()
        if collector is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
            raise ValueError("Worker metrics require a valid HTTP status")
        self.total_requests += 1
        self.total_bytes_sent += bytes_sent
        self.total_bytes_received += bytes_received
        self.total_elapsed += elapsed
        self.status_code_counts[status_code] = (
            self.status_code_counts.get(status_code, 0) + 1
        )
        if is_retry:
            self.retries += 1
        if status_code >= 400:
            self.errors += 1
        if collector is not None and collector is not self:
            collector.record_request(status_code, bytes_sent, bytes_received, elapsed, is_retry)

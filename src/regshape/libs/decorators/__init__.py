#!/usr/bin/env python3

"""
:mod: `decorators` - Telemetry decorators for RegShape
=======================================================

    module:: decorators
    :platform: Unix, Windows
    :synopsis: Provides framework-neutral telemetry decorators and scoped
                configuration through a shared TelemetryConfig context variable.

                ``@track_time``     -- per-method execution timing
                ``@track_scenario`` -- named multi-step workflow timing
                ``@debug_call``     -- HTTP request/response header logging

    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import sys
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import IO, Literal, Optional

from regshape.libs._http import report_secondary_failure
from regshape.libs.decorators.metrics import PerformanceMetrics
from regshape.libs.decorators.streams import TextOutput


@dataclass
class TelemetryConfig:
    """
    Runtime configuration for telemetry decorators.

    :param time_methods_enabled: When True, ``@track_time`` accumulates
        per-method timing entries into :attr:`method_timings`.
    :param time_scenarios_enabled: When True, ``@track_scenario`` renders a
        telemetry summary block at the end of the decorated workflow.
    :param debug_calls_enabled: When True, ``@debug_call`` prints each HTTP
        round-trip in ``curl -v`` style.
    :param metrics_enabled: When True, aggregate performance metrics are
        collected and displayed.
    :param verbosity: Detail level for telemetry output (0=off, 1=normal,
        2=verbose).
    :param output_format: Output format — ``"text"`` (default) or ``"json"``.
    :param output: Writable stream for telemetry output (defaults to stderr so
        it does not interfere with structured stdout output).
    :param log_file_path: Optional file path for telemetry log output.
    :param log_file: Optional caller-owned log stream. A telemetry session
        borrows this stream, or opens log_file_path when no stream is supplied.
    :param method_timings: Ordered list of ``(qualname, elapsed)`` pairs
        accumulated by ``@track_time`` during the current invocation. Consumed
        and cleared by ``@track_scenario`` (or by :func:`flush_telemetry` for
        commands that have no scenario wrapper).
    :param metrics: Aggregate performance metrics.
    """
    time_methods_enabled: bool = False
    time_scenarios_enabled: bool = False
    debug_calls_enabled: bool = False
    metrics_enabled: bool = False
    verbosity: int = 1
    output_format: str = "text"
    output: TextOutput = field(default_factory=lambda: sys.stderr)
    log_file_path: Optional[str] = None
    log_file: Optional[IO] = field(default=None, repr=False)
    method_timings: list[tuple[str, float]] = field(default_factory=list)
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)


_telemetry_config: ContextVar[TelemetryConfig] = ContextVar(
    'telemetry_config', default=TelemetryConfig()
)


def configure_telemetry(config: TelemetryConfig) -> None:
    """
    Set the active telemetry configuration.

    This only activates the configuration; it does not open or close files or
    flush telemetry. Use :func:`telemetry_session` for a managed scope.

    :param config: The telemetry configuration to activate.
    :type config: TelemetryConfig
    """
    _telemetry_config.set(config)


def get_telemetry_config() -> TelemetryConfig:
    """
    Return the active telemetry configuration.

    :return: The current TelemetryConfig (defaults to all-disabled if
        ``configure_telemetry`` has not been called).
    :rtype: TelemetryConfig
    """
    return _telemetry_config.get()


class _TelemetrySession(AbstractContextManager[TelemetryConfig]):
    """Own one telemetry scope without modifying active exception objects."""

    def __init__(self, config: TelemetryConfig, restore_config: bool) -> None:
        """Borrow a configuration; defer file acquisition until entering."""
        self.config = config
        self.restore_config = restore_config
        self._previous_config: TelemetryConfig
        self._owned_log_file: IO | None = None

    def __enter__(self) -> TelemetryConfig:
        """Open an optional append-only log, then activate the configuration."""
        self._previous_config = get_telemetry_config()
        if self.config.log_file_path and self.config.log_file is None:
            self._owned_log_file = open(self.config.log_file_path, "a")
            self.config.log_file = self._owned_log_file
        configure_telemetry(self.config)
        return self.config

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Flush this scope's data and close its log, preserving errors and retained config."""
        primary_error = exc
        token = _telemetry_config.set(self.config)
        try:
            from regshape.libs.decorators.output import flush_telemetry
            try:
                flush_telemetry()
            except BaseException as diagnostic_error:
                if primary_error is None:
                    primary_error = diagnostic_error
                    raise
                report_secondary_failure(primary_error, diagnostic_error, "Telemetry flush")
            finally:
                if self._owned_log_file is not None:
                    try:
                        self._owned_log_file.close()
                    except BaseException as close_error:
                        if primary_error is None:
                            raise
                        report_secondary_failure(primary_error, close_error, "Telemetry log cleanup")
                    finally:
                        self._owned_log_file = None
        finally:
            _telemetry_config.reset(token)
            if self.restore_config:
                _telemetry_config.set(self._previous_config)
        return False


def telemetry_session(
    config: TelemetryConfig, *, restore_config: bool = True,
) -> AbstractContextManager[TelemetryConfig]:
    """Activate telemetry for a synchronous scope and flush it on every exit.

    Open ``log_file_path`` in append mode only if ``log_file`` is not supplied.
    Close only the stream opened by this session; retain its closed handle on
    the configuration for outer cleanup inspection. Caller-supplied streams
    are not closed. Restore the previous active configuration
    after cleanup by default, including for nested sessions and failing operations.
    Final flushing always uses this session's configuration, even if a nested
    scope retained another active configuration.

    :param config: Configuration to activate; yielded unchanged on entry.
    :param restore_config: Restore the previous active configuration on exit.
        Set False when an outer result/diagnostic scope still needs this config;
        file cleanup and flushing are unchanged.
    :returns: A context manager for one telemetry scope.
    :raises OSError: If opening or closing the owned log fails.
    :raises BaseException: Operation and telemetry sink errors propagate.
        Secondary flush/close failures are logged without replacing an active
        error; when cleanup alone fails, its first error propagates.
    """
    return _TelemetrySession(config, restore_config)


# Sub-module imports come after the definitions above.
# The three decorator modules import `get_telemetry_config` from this package;
# because that name is already bound by the time Python executes those imports,
# the circular reference resolves cleanly.
from regshape.libs.decorators.call_details import (  # noqa: E402
    debug_call,
    format_curl_debug,
    format_curl_debug_json,
    http_request,
)
from regshape.libs.decorators.metrics import PerformanceMetrics  # noqa: E402
from regshape.libs.decorators.output import (  # noqa: E402
    flush_telemetry,
    print_telemetry_block,
    telemetry_write,
)
from regshape.libs.decorators.sanitization import (  # noqa: E402
    SENSITIVE_HEADERS,
    redact_header_value,
    redact_headers,
)
from regshape.libs.decorators.scenario import track_scenario  # noqa: E402
from regshape.libs.decorators.timing import track_time  # noqa: E402

__all__ = [
    'TelemetryConfig',
    'PerformanceMetrics',
    'TextOutput',
    'configure_telemetry',
    'get_telemetry_config',
    'telemetry_session',
    'SENSITIVE_HEADERS',
    'redact_header_value',
    'redact_headers',
    'track_time',
    'track_scenario',
    'debug_call',
    'format_curl_debug',
    'format_curl_debug_json',
    'http_request',
    'print_telemetry_block',
    'flush_telemetry',
    'telemetry_write',
]

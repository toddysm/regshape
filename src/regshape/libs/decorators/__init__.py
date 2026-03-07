#!/usr/bin/env python3

"""
:mod: `decorators` - Telemetry decorators for RegShape
=======================================================

    module:: decorators
    :platform: Unix, Windows
    :synopsis: Provides three opt-in telemetry decorators controlled by CLI
                flags and a shared TelemetryConfig context variable.

                ``@track_time``     -- per-method execution timing
                ``@track_scenario`` -- named multi-step workflow timing
                ``@debug_call``     -- HTTP request/response header logging

    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import functools
import sys

import click

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, IO, Optional

from regshape.libs.decorators.metrics import PerformanceMetrics


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
    :param log_file: File handle for log_file_path (managed by
        telemetry_options).
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
    output: IO = field(default_factory=lambda: sys.stderr)
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

    Call this once from ``cli/main.py`` after parsing CLI flags, before any
    domain operations run.

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


def telemetry_options(func: Callable) -> Callable:
    """
    Click decorator that attaches ``--time-methods``, ``--time-scenarios``,
    and ``--debug-calls`` options to a leaf command and automatically calls
    :func:`configure_telemetry` before the command body executes.

    Apply this decorator on leaf commands so that the three telemetry flags
    appear after the subcommand name on the command line, e.g.::

        regshape auth login --time-methods --time-scenarios -r registry.example.com

    The three parameters are consumed by the wrapper and never forwarded to
    the original function, so the command callback does not need to declare
    them.

    :param func: The Click command callback to decorate.
    :return: Wrapped callback with telemetry options registered.
    """
    @click.option(
        "--telemetry-verbosity", "-tv",
        type=click.IntRange(0, 2),
        default=1,
        help="Verbosity level for telemetry output (0=off, 1=normal, 2=verbose).",
    )
    @click.option(
        "--telemetry-format",
        type=click.Choice(["text", "json"], case_sensitive=False),
        default="text",
        help="Telemetry output format: text or json.",
    )
    @click.option(
        "--metrics",
        is_flag=True,
        default=False,
        help="Display aggregate performance metrics.",
    )
    @click.option(
        "--debug-calls",
        is_flag=True,
        default=False,
        help="Print request/response headers for each HTTP call.",
    )
    @click.option(
        "--time-scenarios",
        is_flag=True,
        default=False,
        help="Print execution time for multi-step workflows.",
    )
    @click.option(
        "--time-methods",
        is_flag=True,
        default=False,
        help="Print execution time for individual method calls.",
    )
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        ctx = click.get_current_context()
        log_file_path = ctx.obj.get("log_file") if ctx.obj else None

        verbosity = kwargs.pop("telemetry_verbosity", 1)

        # Verbosity 0 means "off" — disable all telemetry feature flags
        # so downstream decorators and renderers are no-ops.
        if verbosity <= 0:
            enabled_time_methods = False
            enabled_time_scenarios = False
            enabled_debug_calls = False
            enabled_metrics = False
        else:
            enabled_time_methods = kwargs.pop("time_methods", False)
            enabled_time_scenarios = kwargs.pop("time_scenarios", False)
            enabled_debug_calls = kwargs.pop("debug_calls", False)
            enabled_metrics = kwargs.pop("metrics", False)

        # Still pop the flags from kwargs when verbosity=0 so they
        # are not forwarded to the command callback.
        if verbosity <= 0:
            kwargs.pop("time_methods", None)
            kwargs.pop("time_scenarios", None)
            kwargs.pop("debug_calls", None)
            kwargs.pop("metrics", None)

        config = TelemetryConfig(
            time_methods_enabled=enabled_time_methods,
            time_scenarios_enabled=enabled_time_scenarios,
            debug_calls_enabled=enabled_debug_calls,
            metrics_enabled=enabled_metrics,
            verbosity=verbosity,
            output_format=kwargs.pop("telemetry_format", "text"),
            log_file_path=log_file_path,
        )

        # Open log file in append mode if specified
        if config.log_file_path:
            config.log_file = open(config.log_file_path, "a")  # noqa: SIM115

        configure_telemetry(config)
        try:
            result = func(*args, **kwargs)
        finally:
            # flush any method timings not already consumed by a @track_scenario block;
            # runs even when the command calls sys.exit() (raises SystemExit)
            from regshape.libs.decorators.output import flush_telemetry
            flush_telemetry()
            if config.log_file is not None:
                config.log_file.close()
        return result
    return wrapper


# Sub-module imports come after the definitions above.
# The three decorator modules import `get_telemetry_config` from this package;
# because that name is already bound by the time Python executes those imports,
# the circular reference resolves cleanly.
from regshape.libs.decorators.sanitization import SENSITIVE_HEADERS, redact_header_value, redact_headers  # noqa: E402
from regshape.libs.decorators.timing import track_time                                          # noqa: E402
from regshape.libs.decorators.scenario import track_scenario                                    # noqa: E402
from regshape.libs.decorators.call_details import debug_call, format_curl_debug, format_curl_debug_json, http_request  # noqa: E402
from regshape.libs.decorators.output import flush_telemetry, print_telemetry_block, telemetry_write  # noqa: E402
from regshape.libs.decorators.metrics import PerformanceMetrics                                # noqa: E402

__all__ = [
    'TelemetryConfig',
    'PerformanceMetrics',
    'configure_telemetry',
    'get_telemetry_config',
    'telemetry_options',
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

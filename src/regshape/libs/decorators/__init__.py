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

import sys

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import IO


@dataclass
class TelemetryConfig:
    """
    Runtime configuration for telemetry decorators.

    :param time_methods_enabled: When True, ``@track_time`` prints per-method
        timing info.
    :param time_scenarios_enabled: When True, ``@track_scenario`` prints
        per-scenario timing info.
    :param debug_calls_enabled: When True, ``@debug_call`` prints
        request/response headers.
    :param output: Writable stream for telemetry output (defaults to stderr so
        it does not interfere with structured stdout output).
    """
    time_methods_enabled: bool = False
    time_scenarios_enabled: bool = False
    debug_calls_enabled: bool = False
    output: IO = field(default_factory=lambda: sys.stderr)


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


# Sub-module imports come after the definitions above.
# The three decorator modules import `get_telemetry_config` from this package;
# because that name is already bound by the time Python executes those imports,
# the circular reference resolves cleanly.
from regshape.libs.decorators.sanitization import SENSITIVE_HEADERS, redact_header_value, redact_headers  # noqa: E402
from regshape.libs.decorators.timing import track_time          # noqa: E402
from regshape.libs.decorators.scenario import track_scenario    # noqa: E402
from regshape.libs.decorators.call_details import debug_call    # noqa: E402

__all__ = [
    'TelemetryConfig',
    'configure_telemetry',
    'get_telemetry_config',
    'SENSITIVE_HEADERS',
    'redact_header_value',
    'redact_headers',
    'track_time',
    'track_scenario',
    'debug_call',
]

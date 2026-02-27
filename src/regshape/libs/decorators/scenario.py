#!/usr/bin/env python3

"""
:mod: `scenario` - Decorator for tracking multi-step workflow execution time
=============================================================================

    module:: scenario
    :platform: Unix, Windows
    :synopsis: Provides the @track_scenario decorator. When --time-scenarios is
                enabled, prints the total execution time of the decorated
                multi-step workflow to stderr after each call.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import functools
import time


def track_scenario(name: str):
    """
    Decorator that prints execution time for a named multi-step workflow
    when ``--time-scenarios`` is enabled.

    A scenario is a logical operation that may involve multiple HTTP calls
    (e.g., a chunked blob upload is POST + N×PATCH + PUT). Use this decorator
    to measure end-to-end workflow time. Use :func:`track_time` for atomic
    single-call operations.

    When disabled, the decorator is a lightweight passthrough.

    Renders a telemetry summary block on completion, including any method
    timings accumulated by ``@track_time`` during the scenario::

        ── telemetry ──────────────────────────────────────────────────────
          scenario  auth login                                    0.523s
            method  _verify_credentials                           0.231s
            method  store_credentials                             0.045s
        ───────────────────────────────────────────────────────────────────

    After rendering, :attr:`~TelemetryConfig.method_timings` is cleared so
    that each scenario produces exactly one block.

    :param name: Human-readable scenario name (e.g., ``"chunked blob upload"``)
    :type name: str
    :return: Decorator
    :rtype: callable
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Import here to avoid circular import at module load time
            from regshape.libs.decorators import get_telemetry_config
            from regshape.libs.decorators.output import print_telemetry_block
            config = get_telemetry_config()
            if not config.time_scenarios_enabled:
                return func(*args, **kwargs)
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                print_telemetry_block(
                    name, elapsed, list(config.method_timings), config.output
                )
                config.method_timings.clear()
            return result
        return wrapper
    return decorator

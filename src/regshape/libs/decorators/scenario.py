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

from regshape.libs._http import report_secondary_failure


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

    Timings and metrics are consumed before rendering so a failed sink cannot
    trigger duplicate emission during outer cleanup. Diagnostic failures preserve
    an already-active workflow exception.

    :param name: Human-readable scenario name (e.g., ``"chunked blob upload"``)
    :type name: str
    :return: Decorator
    :rtype: callable
    """
    def decorator(func):
        """Bind one workflow to the existing scenario name and telemetry configuration."""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            """Emit one summary without replacing an active workflow failure."""
            # Import here to avoid circular import at module load time
            from regshape.libs.decorators import get_telemetry_config
            from regshape.libs.decorators.output import print_telemetry_block
            config = get_telemetry_config()
            if not config.time_scenarios_enabled:
                return func(*args, **kwargs)
            start = time.perf_counter()
            primary_error: BaseException | None = None
            try:
                result = func(*args, **kwargs)
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    elapsed = time.perf_counter() - start
                    metrics = config.metrics if config.metrics_enabled else None
                    timings = list(config.method_timings)
                    config.method_timings.clear()
                    if config.metrics_enabled:
                        from regshape.libs.decorators.metrics import PerformanceMetrics
                        config.metrics = PerformanceMetrics()
                    print_telemetry_block(
                        name, elapsed, timings, config.output, metrics=metrics,
                    )
                except BaseException as diagnostic_error:
                    if primary_error is None:
                        raise
                    report_secondary_failure(primary_error, diagnostic_error, "Scenario telemetry")
            return result
        return wrapper
    return decorator

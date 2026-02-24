#!/usr/bin/env python3

"""
:mod: `timing` - Decorator for tracking individual method execution time
=========================================================================

    module:: timing
    :platform: Unix, Windows
    :synopsis: Provides the @track_time decorator. When --time-methods is
                enabled, prints the execution time of the decorated function
                to stderr after each call.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import functools
import time


def track_time(func):
    """
    Decorator that prints execution time when ``--time-methods`` is enabled.

    When disabled, the decorator is a lightweight passthrough: it performs
    a single boolean check and immediately dispatches to the original function
    without executing any timing logic.

    Output format::

        [TIMING] <module>.<qualname> completed in <duration>s

    :param func: The function to wrap
    :type func: callable
    :return: The wrapped function
    :rtype: callable
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Import here to avoid circular import at module load time
        from regshape.libs.decorators import get_telemetry_config
        config = get_telemetry_config()
        if not config.time_methods_enabled:
            return func(*args, **kwargs)
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        print(
            f"[TIMING] {func.__module__}.{func.__qualname__} completed in {elapsed:.3f}s",
            file=config.output,
        )
        return result
    return wrapper

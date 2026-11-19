"""Click telemetry options; delegate configuration and resource lifetime to the library."""

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

from regshape.libs.decorators import TelemetryConfig, telemetry_session

_R = TypeVar("_R")


def telemetry_options(func: Callable[..., _R]) -> Callable[..., _R]:
    """Attach the six telemetry options to a leaf command.

    Consume telemetry keywords, read the root ``log_file`` context value, and
    execute the callback in a library-owned telemetry session. Verbosity zero
    disables all four feature flags without forwarding them to the callback.
    Keep the command configuration active for outer result/warning cleanup.

    :param func: Command callback, which need not accept telemetry options.
    :returns: Callback with the unchanged option names, defaults and help text.
        Callback and diagnostic errors propagate through library cleanup.
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
    @wraps(func)
    def wrapper(
        *args: Any,
        time_methods: bool = False,
        time_scenarios: bool = False,
        debug_calls: bool = False,
        metrics: bool = False,
        telemetry_format: str = "text",
        telemetry_verbosity: int = 1,
        **kwargs: Any,
    ) -> _R:
        """Map parsed flags into one managed library configuration and run the callback."""
        ctx = click.get_current_context()
        enabled = telemetry_verbosity > 0
        config = TelemetryConfig(
            time_methods_enabled=enabled and time_methods,
            time_scenarios_enabled=enabled and time_scenarios,
            debug_calls_enabled=enabled and debug_calls,
            metrics_enabled=enabled and metrics,
            verbosity=telemetry_verbosity,
            output_format=telemetry_format,
            log_file_path=ctx.obj.get("log_file") if ctx.obj else None,
        )
        with telemetry_session(config, restore_config=False):
            return func(*args, **kwargs)

    return wrapper

#!/usr/bin/env python3

"""
:mod: `output` - Shared telemetry block renderer
=================================================

    module:: output
    :platform: Unix, Windows
    :synopsis: Provides :func:`print_telemetry_block` and
               :func:`flush_telemetry` — the single rendering path for all
               ``--time-methods`` and ``--time-scenarios`` output.

               All telemetry output is collected during a command invocation
               and emitted as a single delimited block at the end, keeping
               stderr clean and easy to read.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import sys

from typing import IO

_BLOCK_WIDTH = 66
_LABEL_COL = 12        # "  scenario  " / "    method  "
_ELAPSED_WIDTH = 7     # " 0.523s" — right-justified
_NAME_WIDTH = _BLOCK_WIDTH - _LABEL_COL - _ELAPSED_WIDTH  # 47


def _format_row(indent: str, label: str, name: str, elapsed: float) -> str:
    """Format a single telemetry row.

    :param indent: Leading whitespace for the label (``"  "`` for scenario,
        ``"    "`` for method).
    :param label: Row type label (``"scenario"`` or ``"method"``).
    :param name: Scenario name or function ``__qualname__``.
    :param elapsed: Elapsed time in seconds.
    :return: Formatted string exactly :data:`_BLOCK_WIDTH` characters wide.
    """
    label_cell = f"{indent}{label}"
    elapsed_str = f"{elapsed:.3f}s"
    if len(name) > _NAME_WIDTH:
        name = name[:_NAME_WIDTH - 1] + "\u2026"   # truncate with ellipsis
    return f"{label_cell:<{_LABEL_COL}}{name:<{_NAME_WIDTH}}{elapsed_str:>{_ELAPSED_WIDTH}}"


def print_telemetry_block(
    scenario_name: str | None,
    scenario_elapsed: float | None,
    method_timings: list[tuple[str, float]],
    out: IO = None,
) -> None:
    """Render one telemetry summary block to *out*.

    Emits nothing if there is no data (both *scenario_name* is ``None`` and
    *method_timings* is empty).

    Block format::

        ── telemetry ──────────────────────────────────────────────────────
          scenario  auth login                                    0.523s
            method  _verify_credentials                           0.231s
            method  store_credentials                             0.045s
        ───────────────────────────────────────────────────────────────────

    When only methods are present (no enclosing scenario)::

        ── telemetry ──────────────────────────────────────────────────────
            method  _verify_credentials                           0.231s
        ───────────────────────────────────────────────────────────────────

    :param scenario_name: Human-readable scenario name, or ``None`` if no
        ``@track_scenario`` wrapper was active.
    :param scenario_elapsed: Total scenario elapsed time in seconds, or
        ``None`` when *scenario_name* is ``None``.
    :param method_timings: Ordered list of ``(qualname, elapsed)`` pairs
        collected by ``@track_time`` during the invocation.
    :param out: Writable stream for output. Defaults to ``sys.stderr``.
    """
    if out is None:
        out = sys.stderr
    if not scenario_name and not method_timings:
        return

    prefix = "\u2500\u2500 telemetry "   # "── telemetry "
    header = prefix + "\u2500" * (_BLOCK_WIDTH - len(prefix))
    print(header, file=out)

    if scenario_name is not None and scenario_elapsed is not None:
        print(_format_row("  ", "scenario", scenario_name, scenario_elapsed), file=out)

    for name, elapsed in method_timings:
        print(_format_row("    ", "method", name, elapsed), file=out)

    print("\u2500" * _BLOCK_WIDTH, file=out)


def flush_telemetry() -> None:
    """Print and clear any accumulated method timings that were not yet
    consumed by a ``@track_scenario`` block.

    Called automatically by the ``@telemetry_options`` wrapper after every
    leaf command returns. Handles the ``--time-methods``-only case where no
    ``@track_scenario`` decorator is present to trigger rendering.

    Is a no-op when no timings have accumulated.
    """
    from regshape.libs.decorators import get_telemetry_config
    config = get_telemetry_config()
    if config.method_timings:
        print_telemetry_block(None, None, list(config.method_timings), config.output)
        config.method_timings.clear()

#!/usr/bin/env python3

"""
:mod: `output` - Shared telemetry block renderer
=================================================

    module:: output
    :platform: Unix, Windows
    :synopsis: Provides :func:`print_telemetry_block`,
               :func:`flush_telemetry`, and :func:`telemetry_write` — the
               single rendering path for all telemetry output.

               All telemetry output is collected during a command invocation
               and emitted as a single delimited block at the end, keeping
               stderr clean and easy to read.  When ``--telemetry-format json``
               is active, output is emitted as newline-delimited JSON (NDJSON).
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys

from datetime import datetime, timezone
from typing import IO, Optional

_BLOCK_WIDTH = 66
_LABEL_COL = 12        # "  scenario  " / "    method  "
_ELAPSED_WIDTH = 7     # " 0.523s" — right-justified
_NAME_WIDTH = _BLOCK_WIDTH - _LABEL_COL - _ELAPSED_WIDTH  # 47


def telemetry_write(line: str, out: IO = None, log_file: IO = None) -> None:
    """Write a telemetry line to *out* and optionally to the log file.

    :param line: Text to write (newline appended automatically).
    :param out: Primary output stream (defaults to stderr).
    :param log_file: Secondary output stream (log file), or ``None``.
    """
    if out is None:
        out = sys.stderr
    print(line, file=out)
    if log_file is not None:
        print(line, file=log_file)


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


def _format_info_row(indent: str, label: str, info: str) -> str:
    """Format a telemetry row with an informational string instead of elapsed time.

    :param indent: Leading whitespace for the label.
    :param label: Row type label (e.g. ``"metrics"``).
    :param info: Informational text to display.
    :return: Formatted string.
    """
    label_cell = f"{indent}{label}"
    return f"{label_cell:<{_LABEL_COL}}{info}"


def _format_bytes(n: int) -> str:
    """Format a byte count into a human-readable string.

    :param n: Number of bytes.
    :return: Formatted string (e.g. ``"1.2 KB"``, ``"3.4 MB"``).
    """
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"


def _render_text_block(
    scenario_name: Optional[str],
    scenario_elapsed: Optional[float],
    method_timings: list[tuple[str, float]],
    metrics,
    out: IO,
    log_file: Optional[IO],
    verbosity: int,
) -> None:
    """Render the telemetry summary block in human-readable text format.

    :param scenario_name: Scenario name or ``None``.
    :param scenario_elapsed: Scenario elapsed time or ``None``.
    :param method_timings: List of ``(qualname, elapsed)`` pairs.
    :param metrics: :class:`PerformanceMetrics` instance or ``None``.
    :param out: Primary output stream.
    :param log_file: Secondary output stream or ``None``.
    :param verbosity: Telemetry verbosity level (1 or 2).
    """
    prefix = "\u2500\u2500 telemetry "   # "── telemetry "
    header = prefix + "\u2500" * (_BLOCK_WIDTH - len(prefix))
    telemetry_write(header, out, log_file)

    if scenario_name is not None and scenario_elapsed is not None:
        telemetry_write(
            _format_row("  ", "scenario", scenario_name, scenario_elapsed),
            out, log_file,
        )

    for name, elapsed in method_timings:
        telemetry_write(
            _format_row("    ", "method", name, elapsed),
            out, log_file,
        )

    # Metrics summary rows
    if metrics is not None and metrics.total_requests > 0:
        sent = _format_bytes(metrics.total_bytes_sent)
        recv = _format_bytes(metrics.total_bytes_received)
        telemetry_write(
            _format_info_row(
                "   ", "metrics",
                f"requests: {metrics.total_requests}  sent: {sent}  recv: {recv}",
            ),
            out, log_file,
        )
        status_parts = [
            f"{code}\u00d7{count}"
            for code, count in sorted(metrics.status_code_counts.items())
        ]
        status_str = "  ".join(status_parts)
        telemetry_write(
            _format_info_row(
                "   ", "metrics",
                f"status: {status_str}  retries: {metrics.retries}  errors: {metrics.errors}",
            ),
            out, log_file,
        )

    telemetry_write("\u2500" * _BLOCK_WIDTH, out, log_file)


def _render_json_block(
    scenario_name: Optional[str],
    scenario_elapsed: Optional[float],
    method_timings: list[tuple[str, float]],
    metrics,
    out: IO,
    log_file: Optional[IO],
) -> None:
    """Render the telemetry summary block as NDJSON events.

    :param scenario_name: Scenario name or ``None``.
    :param scenario_elapsed: Scenario elapsed time or ``None``.
    :param method_timings: List of ``(qualname, elapsed)`` pairs.
    :param metrics: :class:`PerformanceMetrics` instance or ``None``.
    :param out: Primary output stream.
    :param log_file: Secondary output stream or ``None``.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    if scenario_name is not None:
        event = {
            "type": "scenario",
            "name": scenario_name,
            "elapsed_s": round(scenario_elapsed, 6) if scenario_elapsed is not None else None,
            "methods": [
                {"name": name, "elapsed_s": round(elapsed, 6)}
                for name, elapsed in method_timings
            ],
            "timestamp": timestamp,
        }
        telemetry_write(json.dumps(event, separators=(",", ":")), out, log_file)
    elif method_timings:
        # Methods without a scenario — emit individual method events
        for name, elapsed in method_timings:
            event = {
                "type": "method",
                "name": name,
                "elapsed_s": round(elapsed, 6),
                "timestamp": timestamp,
            }
            telemetry_write(json.dumps(event, separators=(",", ":")), out, log_file)

    if metrics is not None and metrics.total_requests > 0:
        event = {
            "type": "metrics",
            "total_requests": metrics.total_requests,
            "total_elapsed_s": round(metrics.total_elapsed, 6),
            "total_bytes_sent": metrics.total_bytes_sent,
            "total_bytes_received": metrics.total_bytes_received,
            "retries": metrics.retries,
            "errors": metrics.errors,
            "status_code_counts": {
                str(k): v
                for k, v in sorted(metrics.status_code_counts.items())
            },
            "timestamp": timestamp,
        }
        telemetry_write(json.dumps(event, separators=(",", ":")), out, log_file)


def print_telemetry_block(
    scenario_name: str | None,
    scenario_elapsed: float | None,
    method_timings: list[tuple[str, float]],
    out: IO = None,
    *,
    metrics=None,
) -> None:
    """Render one telemetry summary block to *out*.

    Emits nothing if there is no data (both *scenario_name* is ``None`` and
    *method_timings* is empty and *metrics* has no requests).

    Block format (text)::

        ── telemetry ──────────────────────────────────────────────────────
          scenario  auth login                                    0.523s
            method  _verify_credentials                           0.231s
            method  store_credentials                             0.045s
           metrics  requests: 3  sent: 1.2 KB  recv: 5.5 KB
           metrics  status: 200×2  401×1  retries: 1  errors: 0
        ───────────────────────────────────────────────────────────────────

    :param scenario_name: Human-readable scenario name, or ``None`` if no
        ``@track_scenario`` wrapper was active.
    :param scenario_elapsed: Total scenario elapsed time in seconds, or
        ``None`` when *scenario_name* is ``None``.
    :param method_timings: Ordered list of ``(qualname, elapsed)`` pairs
        collected by ``@track_time`` during the invocation.
    :param out: Writable stream for output. Defaults to ``sys.stderr``.
    :param metrics: :class:`PerformanceMetrics` instance or ``None``.
    """
    from regshape.libs.decorators import get_telemetry_config
    config = get_telemetry_config()

    if out is None:
        out = config.output

    has_metrics = metrics is not None and metrics.total_requests > 0
    if not scenario_name and not method_timings and not has_metrics:
        return

    if config.output_format == "json":
        _render_json_block(
            scenario_name, scenario_elapsed, method_timings,
            metrics, out, config.log_file,
        )
    else:
        _render_text_block(
            scenario_name, scenario_elapsed, method_timings,
            metrics, out, config.log_file, config.verbosity,
        )


def flush_telemetry() -> None:
    """Print and clear any accumulated method timings that were not yet
    consumed by a ``@track_scenario`` block.

    Called automatically by the ``@telemetry_options`` wrapper after every
    leaf command returns. Handles the ``--time-methods``-only case where no
    ``@track_scenario`` decorator is present to trigger rendering.

    Also emits aggregate metrics if ``--metrics`` is enabled.

    Is a no-op when no timings have accumulated and metrics are empty.
    """
    from regshape.libs.decorators import get_telemetry_config
    config = get_telemetry_config()

    metrics = config.metrics if config.metrics_enabled else None
    has_timings = bool(config.method_timings)
    has_metrics = metrics is not None and metrics.total_requests > 0

    if has_timings or has_metrics:
        print_telemetry_block(
            None, None, list(config.method_timings), config.output,
            metrics=metrics,
        )
        config.method_timings.clear()
        if config.metrics_enabled:
            from regshape.libs.decorators.metrics import PerformanceMetrics
            config.metrics = PerformanceMetrics()

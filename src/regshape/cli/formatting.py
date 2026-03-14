#!/usr/bin/env python3

"""
:mod:`regshape.cli.formatting` - Reusable CLI output formatting helpers
========================================================================

.. module:: regshape.cli.formatting
   :platform: Unix, Windows
   :synopsis: Centralized output helpers for JSON, text, error messages,
              tables, lists, key-value pairs, and progress status.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys
from typing import Optional

import click


def emit_json(data: dict | list, output_path: Optional[str] = None, err: bool = False) -> None:
    """Format and emit a JSON object with 2-space indentation.

    :param data: Serializable dict or list.
    :param output_path: If provided, write to file instead of stdout.
    :param err: If ``True``, write to stderr instead of stdout.
    """
    content = json.dumps(data, indent=2)
    emit_text(content, output_path, err=err)


def emit_text(content: str, output_path: Optional[str] = None, err: bool = False) -> None:
    """Emit plain text content to stdout or a file.

    :param content: Text string to output.
    :param output_path: If provided, write to file instead of stdout.
    :param err: If ``True``, write to stderr instead of stdout (ignored
        when *output_path* is set).
    """
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
    else:
        click.echo(content, err=err)


def emit_error(reference: str, reason: str, exit_code: int = 1) -> None:
    """Print a standardized error message to stderr and exit.

    :param reference: Context identifier displayed in brackets.
    :param reason: Human-readable error description.
    :param exit_code: Process exit code (default: 1).
    """
    click.echo(f"Error [{reference}]: {reason}", err=True)
    sys.exit(exit_code)


def emit_table(rows: list[list[str]], headers: Optional[list[str]] = None) -> None:
    """Print tabular data with aligned columns.

    :param rows: List of row data, each row a list of string values.
    :param headers: Optional column headers.
    """
    all_rows = ([headers] + rows) if headers else rows
    if not all_rows:
        return

    num_cols = max(len(row) for row in all_rows)
    col_widths = [0] * num_cols
    for row in all_rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def _format_row(row: list[str]) -> str:
        parts = []
        for i, cell in enumerate(row):
            if i < len(col_widths):
                parts.append(cell.ljust(col_widths[i]))
            else:
                parts.append(cell)
        return "  ".join(parts).rstrip()

    if headers:
        click.echo(_format_row(headers))

    for row in rows:
        click.echo(_format_row(row))


def emit_list(items: list[str], output_path: Optional[str] = None) -> None:
    """Print a simple one-item-per-line list.

    :param items: List of string items.
    :param output_path: If provided, write to file instead of stdout.
    """
    emit_text("\n".join(items), output_path)


def format_key_value(pairs: list[tuple[str, str]], separator: str = ":") -> str:
    """Format aligned key-value pairs for display.

    :param pairs: List of (key, value) tuples.
    :param separator: Character between key and value (default: ``":"``).
    :returns: Formatted multi-line string with aligned values.
    """
    if not pairs:
        return ""
    max_key_len = max(len(k) for k, _ in pairs)
    lines = []
    for key, value in pairs:
        padded_key = key.ljust(max_key_len)
        lines.append(f"{padded_key}{separator} {value}")
    return "\n".join(lines)


def progress_status(message: str) -> None:
    """Print a transient status message to stderr.

    :param message: Status message to display.
    """
    click.echo(message, err=True)

#!/usr/bin/env python3

"""
:mod:`regshape.cli.formatting` - Reusable CLI output formatting helpers
========================================================================

.. module:: regshape.cli.formatting
   :platform: Unix, Windows
   :synopsis: Centralized output helpers for JSON, text, error messages,
              tables, lists, sizes, digests, key-value pairs, and progress status.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys
from typing import Optional

import click

from regshape.libs._http import report_secondary_failure
from regshape.libs.errors import UnsafeReferenceError


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

    Owned files are closed on every path. A secondary close failure is reported
    without replacing an active write/cancellation error; stdout is borrowed.

    :param content: Text string to output.
    :param output_path: If provided, write to file instead of stdout.
    :param err: If ``True``, write to stderr instead of stdout (ignored
        when *output_path* is set).
    """
    if output_path:
        fh = open(output_path, "w", encoding="utf-8")  # noqa: SIM115 - preserve a primary write error
        primary_error: BaseException | None = None
        try:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                fh.close()
            except BaseException as close_error:
                if primary_error is None:
                    raise
                report_secondary_failure(primary_error, close_error, "Output file cleanup")
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


def reference_error_context(reference: str, error: BaseException, *, option: str) -> str:
    """Select diagnostic context without echoing a parser-rejected unsafe value.

    :param reference: Raw reference whose parsing produced *error*.
    :param error: Original parsing failure; pass the cause when a command wraps it.
    :param option: Trusted option label to display instead of an unsafe reference.
    :returns: The option label for ``UnsafeReferenceError``, otherwise the raw
        reference so ordinary usage/operation errors retain their context.
    """
    return option if isinstance(error, UnsafeReferenceError) else reference


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


def format_size(size_bytes: int) -> str:
    """Render a byte count using the compact Docker table format.

    :param size_bytes: Image size in bytes.
    :returns: Binary-scaled B/KB/MB/GB with no separating space. KB and MB
        round to whole numbers; GB retains two decimal places.
    """
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.2f}GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.0f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


def short_digest(digest: str) -> str:
    """Abbreviate an image ID using the existing Docker table convention.

    :param digest: Image ID, optionally prefixed with ``sha256:``.
    :returns: The first 19 characters with a leading ``sha256:`` removed,
        leaving up to 12 SHA-256 hex characters. Other prefixes and
        unprefixed IDs retain up to 19 characters; shorter IDs are unchanged.
    """
    return digest[:19].removeprefix("sha256:")


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

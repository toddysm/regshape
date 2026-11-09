"""Route library diagnostics through an optional task-local, invoking-thread bridge.

Ordinary calls retain Python logging behavior. Worker messages contain rendered
text and JSON metadata, never a live exception, traceback or mutable LogRecord.
"""

import json
import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LogMessage:
    """Immutable deferred log entry; metadata has already passed existing redaction."""

    name: str
    level: int
    text: str
    metadata: str
    method: Literal["log", "error", "warning"] = "log"


log_dispatch: ContextVar[Callable[[LogMessage], None] | None] = ContextVar(
    "regshape_log_dispatch",
    default=None,
)


def emit_log(
    logger: logging.Logger,
    level: int,
    message: str,
    *args: object,
    extra: Mapping[str, object] | None = None,
    exc_info: bool = False,
    method: Literal["log", "error", "warning"] = "log",
) -> None:
    """Log directly or enqueue an immutable entry in the active worker context.

    :param extra: JSON-compatible, already sanitized library metadata.
    :param exc_info: Preserve traceback logging on the ordinary path. Deferred
        worker diagnostics retain error metadata, not worker traceback objects.
    :param method: Original logger entry point, preserving convenience-method hooks.
    :raises ValueError: If deferred metadata cannot be represented as finite JSON.
    """
    dispatch = log_dispatch.get()
    if dispatch is None:
        if method == "error":
            logger.error(message, *args, extra=extra, exc_info=exc_info)
        elif method == "warning":
            logger.warning(message, *args, extra=extra, exc_info=exc_info)
        elif logger.isEnabledFor(level):
            logger.log(level, message, *args, extra=extra, exc_info=exc_info)
    else:
        dispatch(
            LogMessage(
                logger.name,
                level,
                message % args if args else message,
                json.dumps(dict(extra or {}), allow_nan=False),
                method,
            )
        )

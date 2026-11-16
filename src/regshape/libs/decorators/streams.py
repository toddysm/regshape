"""Minimal writable-text contract for telemetry destinations and coordinating proxies."""

from typing import Protocol


class TextOutput(Protocol):
    """A borrowed text destination; telemetry does not seek, read, or close it."""

    def write(self, text: str, /) -> int | None:
        """Write text; callers do not depend on a returned character count."""
        ...

    def flush(self) -> None:
        """Flush pending output without taking ownership of the destination."""
        ...

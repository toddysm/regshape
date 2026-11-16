"""Shared artifact-reference options; resolve aliases before command side effects."""

from collections.abc import Callable
from typing import TypeVar

import click

_F = TypeVar("_F", bound=Callable[..., object])
_LEGACY_NOTICE = (
    "Legacy --image-ref / -i: Deprecated in 0.2; use --artifact / -a. "
    "Retained for compatibility."
)


def _resolve_artifact_reference(
    ctx: click.Context,
    param: click.Parameter,
    values: tuple[str, ...],
) -> str | None:
    """Collapse identical raw values without parsing or resolving remote references.

    :returns: The single target string; ``None`` only for incomplete resilient
        help/completion parsing.
    :raises click.MissingParameter: If normal parsing supplies no value.
    :raises click.UsageError: If any supplied values differ, without echoing them.
    """
    if not values:
        if ctx.resilient_parsing:
            return None
        raise click.MissingParameter(ctx=ctx, param=param)
    reference = values[0]
    if any(value != reference for value in values):
        raise click.UsageError(
            "Conflicting reference options. Supply one artifact reference.",
            ctx=ctx,
        )
    return reference


def artifact_option(
    *,
    help_text: str,
    metavar: str = "ARTIFACT",
) -> Callable[[_F], _F]:
    """Declare one required target with preferred and legacy option spellings.

    All four spellings feed the existing ``image_ref`` callback parameter as a
    string. Repeated identical targets are accepted; different targets produce
    a usage error before the command callback runs. Reference grammar and
    operation-specific restrictions remain the caller's responsibility.

    :param help_text: Operation-specific target semantics, preceding the
        legacy-only deprecation notice.
    :param metavar: Displayed value label; repository-only commands may supply
        ``"REPOSITORY"``.
    :returns: A Click decorator preserving the decorated callable's type.
    """
    return click.option(
        "--artifact",
        "-a",
        "--image-ref",
        "-i",
        "image_ref",
        required=True,
        multiple=True,
        type=click.STRING,
        callback=_resolve_artifact_reference,
        metavar=metavar,
        help=f"{help_text} {_LEGACY_NOTICE}",
    )

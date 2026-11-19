#!/usr/bin/env python3

"""
:mod:`regshape.libs.refs` - OCI artifact reference utilities
==========================================================

.. module:: regshape.libs.refs
   :platform: Unix, Windows
   :synopsis: Helpers for parsing, classifying and validating artifact references
              of the form ``registry/repo:tag`` or ``registry/repo@digest``,
              retaining the legacy image-named parser interface.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import re

from regshape.libs.errors import ReferenceValidationError

_DIGEST = re.compile(r"(?:sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128})")


def is_digest(reference: str) -> bool:
    """Classify a parsed tag-or-digest token without validating its content.

    A colon identifies a digest-shaped token because OCI tags cannot contain
    colons. Unsupported algorithms and malformed payloads still classify as
    digests so callers cannot accidentally resolve them as tags. Pass the
    parser's final token, not a full registry/repository reference.

    :param reference: Parsed tag or ``algorithm:encoded`` token.
    :returns: Whether the token has digest shape; use :func:`validate_digest`
        when a supported, complete digest is required.
    """
    return ":" in reference


def validate_digest(reference: str) -> str:
    """Return a supported digest unchanged, or reject it without side effects.

    Accept only lowercase SHA-256 with 64 hexadecimal characters or SHA-512
    with 128. No whitespace, case normalization or truncation is performed.
    This does not change the permissive legacy reference parser.

    :param reference: Parsed ``algorithm:hex`` token.
    :returns: The original validated token.
    :raises ReferenceValidationError: If the algorithm, payload or type is invalid.
    """
    if not isinstance(reference, str) or reference.partition(":")[0] not in {"sha256", "sha512"}:
        raise ReferenceValidationError("digest references must use sha256 or sha512")
    if _DIGEST.fullmatch(reference) is None:
        raise ReferenceValidationError("Digest must be lowercase sha256:64-hex or sha512:128-hex")
    return reference


def parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Parse an image reference into ``(registry, repository, reference)``.

    The registry must be embedded in the reference.  Supported formats::

        registry.io/myimage:tag
        registry.io/myrepo/myimage:tag
        registry.io/myimage@sha256:<hex>
        registry.io/myrepo/myimage@sha256:<hex>
        registry.io/myrepo/myimage          (reference defaults to "latest")
        localhost:5000/myimage:tag

    The first component of the path is treated as the registry hostname
    when it contains a dot (``.``), a colon (``:``), or equals ``localhost``.

    :param image_ref: The image reference string.
    :returns: A ``(registry, repo, reference)`` triple where *reference*
        is the tag or digest string (without the ``@`` prefix).
    :raises ValueError: If the registry or repository cannot be determined.
    """
    # Separate digest (@) from tag (:) — digest takes priority
    if "@" in image_ref:
        path_part, digest_part = image_ref.rsplit("@", 1)
        ref = digest_part
    elif ":" in image_ref:
        # Could be "registry:port/repo:tag" so split on the last ":"
        last_colon = image_ref.rfind(":")
        before_colon = image_ref[:last_colon]
        after_colon = image_ref[last_colon + 1:]
        if "/" in after_colon:
            # "registry:5000/repo/image" with no tag — use "latest"
            path_part = image_ref
            ref = "latest"
        else:
            path_part = before_colon
            ref = after_colon
    else:
        path_part = image_ref
        ref = "latest"

    parts = path_part.split("/")
    first = parts[0]
    is_registry = "." in first or ":" in first or first == "localhost"

    if is_registry:
        registry = first
        repo = "/".join(parts[1:])
    else:
        raise ValueError(
            f"Cannot determine registry from {image_ref!r}: "
            "embed the registry in the reference (e.g. acr.io/repo:tag)"
        )

    if not repo:
        raise ValueError(f"Cannot determine repository from {image_ref!r}")

    return registry, repo, ref


def parse_artifact_ref(artifact_ref: str) -> tuple[str, str, str]:
    """Parse an artifact reference using the existing image-reference grammar.

    This additive name delegates to :func:`parse_image_ref`; it does not add
    digest validation or change default-tag, registry, or repository handling.
    The legacy parser and its ``image_ref`` keyword remain supported.

    :param artifact_ref: Reference with an embedded registry and repository.
    :returns: ``(registry, repository, tag_or_digest)`` with the existing default
        of ``"latest"`` when no tag/digest is supplied.
    :raises ValueError: If the existing parser cannot determine registry or
        repository.
    """
    return parse_image_ref(artifact_ref)


def format_ref(registry: str, repo: str, reference: str) -> str:
    """Return a canonical OCI reference string.

    Uses ``@`` for digest-shaped tokens and ``:`` for tags, according to
    :func:`is_digest`. Formatting does not validate algorithms or payloads;
    unsupported and malformed digest tokens retain their digest separator.

    :param registry: Registry hostname (e.g. ``acr.example.io``).
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param reference: Tag or digest string.
    :returns: Canonical reference, e.g. ``acr.io/repo:tag`` or
              ``acr.io/repo@sha256:abc...``.
    """
    sep = "@" if is_digest(reference) else ":"
    return f"{registry}/{repo}{sep}{reference}"

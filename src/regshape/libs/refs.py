#!/usr/bin/env python3

"""
:mod:`regshape.libs.refs` - OCI image reference utilities
==========================================================

.. module:: regshape.libs.refs
   :platform: Unix, Windows
   :synopsis: Helpers for parsing and formatting OCI image references of the
              form ``registry/repo:tag`` or ``registry/repo@digest``.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""


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


def format_ref(registry: str, repo: str, reference: str) -> str:
    """Return a canonical OCI reference string.

    Uses ``@`` as separator when *reference* is a digest (starts with
    ``sha256:`` or ``sha512:``), and ``:`` for tag references.

    :param registry: Registry hostname (e.g. ``acr.example.io``).
    :param repo: Repository name (e.g. ``myrepo/myimage``).
    :param reference: Tag or digest string.
    :returns: Canonical reference, e.g. ``acr.io/repo:tag`` or
              ``acr.io/repo@sha256:abc...``.
    """
    sep = (
        "@"
        if (reference.startswith("sha256:") or reference.startswith("sha512:"))
        else ":"
    )
    return f"{registry}/{repo}{sep}{reference}"

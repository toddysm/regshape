#!/usr/bin/env python3
"""Update the RegShape Homebrew formula to a given version.

Fetches the sdist URL and sha256 for ``regshape==<version>`` from PyPI and
rewrites the top-level ``url``/``sha256`` fields of the formula (the package
source, not the dependency ``resource`` blocks).

Usage:
    python bump_formula.py <formula_path> <version>
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

EXPECTED_FORMULA_NAME = "regshape.rb"


def resolve_formula_path(raw: str) -> str:
    """Validate a caller-supplied formula path.

    Guards against path-traversal by requiring the resolved path to stay within
    the current working directory and to carry the expected formula filename.
    """
    base = os.path.realpath(os.getcwd())
    resolved = os.path.realpath(raw)
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"Formula path escapes the working directory: {raw!r}")
    if os.path.basename(resolved) != EXPECTED_FORMULA_NAME:
        raise ValueError(
            f"Unexpected formula filename {os.path.basename(resolved)!r}; "
            f"expected {EXPECTED_FORMULA_NAME!r}"
        )
    return resolved


def fetch_sdist(version: str) -> tuple[str, str]:
    url = f"https://pypi.org/pypi/regshape/{version}/json"
    last_err: Exception | None = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.load(resp)
            for entry in data["urls"]:
                if entry["packagetype"] == "sdist":
                    return entry["url"], entry["digests"]["sha256"]
            raise RuntimeError(f"No sdist found for regshape {version}")
        except Exception as err:  # noqa: BLE001 - retry on propagation delay
            last_err = err
            time.sleep(10)
    raise RuntimeError(f"regshape {version} not available on PyPI: {last_err}")


def bump(formula_path: str, version: str) -> None:
    path = resolve_formula_path(formula_path)
    sdist_url, sha256 = fetch_sdist(version)
    text = open(path, encoding="utf-8").read()
    # Only the top-level fields are indented with exactly two spaces; resource
    # blocks use four spaces, so anchored two-space matches target the package.
    text, n_url = re.subn(r'^  url ".*"$', f'  url "{sdist_url}"', text, count=1, flags=re.M)
    text, n_sha = re.subn(r'^  sha256 ".*"$', f'  sha256 "{sha256}"', text, count=1, flags=re.M)
    if n_url != 1 or n_sha != 1:
        raise RuntimeError("Failed to locate top-level url/sha256 in formula")
    open(path, "w", encoding="utf-8").write(text)
    print(f"Bumped {path} to regshape {version}")
    print(f"  url    {sdist_url}")
    print(f"  sha256 {sha256}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: bump_formula.py <formula_path> <version>")
    bump(sys.argv[1], sys.argv[2])

#!/usr/bin/env python3
"""Update the RegShape Homebrew formula to a given version.

Fetches the sdist URL and sha256 for ``regshape==<version>`` from PyPI and
rewrites the top-level ``url``/``sha256`` fields of the formula (the package
source, not the dependency ``resource`` blocks).

Usage:
    python bump_formula.py <version>

The formula is always read from and written to a fixed, constant path relative to
the current working directory, so no caller-supplied data is ever used to build a
filesystem path. Run this script from the tap checkout root.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

# Constant path (relative to CWD); never derived from user input.
FORMULA_PATH = "Formula/regshape.rb"


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


def bump(version: str) -> None:
    sdist_url, sha256 = fetch_sdist(version)
    with open(FORMULA_PATH, encoding="utf-8") as fh:
        text = fh.read()
    # Only the top-level fields are indented with exactly two spaces; resource
    # blocks use four spaces, so anchored two-space matches target the package.
    text, n_url = re.subn(r'^  url ".*"$', f'  url "{sdist_url}"', text, count=1, flags=re.M)
    text, n_sha = re.subn(r'^  sha256 ".*"$', f'  sha256 "{sha256}"', text, count=1, flags=re.M)
    if n_url != 1 or n_sha != 1:
        raise RuntimeError("Failed to locate top-level url/sha256 in formula")
    with open(FORMULA_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"Bumped {FORMULA_PATH} to regshape {version}")
    print(f"  url    {sdist_url}")
    print(f"  sha256 {sha256}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: bump_formula.py <version>")
    bump(sys.argv[1])

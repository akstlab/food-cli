"""Where the CLI keeps its files.

Everything lives under one owner-only directory. The pre-rename `~/.swiggy-cli`
is reused when it already exists, so an upgrade does not orphan somebody's saved
QR codes and downloaded images in a directory nothing reads any more.
"""

from __future__ import annotations

import os
from pathlib import Path

CURRENT_DIR_NAME = ".food-cli"
LEGACY_DIR_NAME = ".swiggy-cli"


def data_dir() -> Path:
    """The root for all local state. FOOD_CLI_HOME overrides it."""
    explicit = os.environ.get("FOOD_CLI_HOME")
    if explicit:
        return Path(explicit)
    legacy = Path.home() / LEGACY_DIR_NAME
    if legacy.exists():
        return legacy
    return Path.home() / CURRENT_DIR_NAME


def subdir(name: str) -> Path:
    return data_dir() / name

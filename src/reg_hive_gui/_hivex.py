"""Import helper for hivex with Debian/Ubuntu system-site fallback."""
from __future__ import annotations

from pathlib import Path
import importlib
import site
import sys


SYSTEM_SITE_DIRS = (
    "/usr/lib/python3/dist-packages",
    "/usr/local/lib/python3/dist-packages",
)


def load_hivex():
    try:
        return importlib.import_module("hivex")
    except ModuleNotFoundError:
        for candidate in SYSTEM_SITE_DIRS:
            path = Path(candidate)
            if path.exists():
                site.addsitedir(str(path))
        return importlib.import_module("hivex")


hivex = load_hivex()

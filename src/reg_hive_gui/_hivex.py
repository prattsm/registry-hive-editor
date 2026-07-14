"""Import helper for hivex with Debian/Ubuntu system-site fallback.

The import is intentionally lazy so the package, CLI, and platform-neutral
utilities remain usable on systems where hivex is not installed.
"""
from __future__ import annotations

import importlib
import site
from pathlib import Path

SYSTEM_SITE_DIRS = (
    "/usr/lib/python3/dist-packages",
    "/usr/local/lib/python3/dist-packages",
)


_hivex_module = None


def load_hivex():
    global _hivex_module
    if _hivex_module is not None:
        return _hivex_module
    try:
        module = importlib.import_module("hivex")
    except ModuleNotFoundError as original_error:
        for candidate in SYSTEM_SITE_DIRS:
            path = Path(candidate)
            if path.exists():
                site.addsitedir(str(path))
        try:
            module = importlib.import_module("hivex")
        except ModuleNotFoundError:
            raise RuntimeError(
                "The hivex backend is not installed. On Ubuntu/WSL install "
                "python3-hivex and libhivex0. On Windows use the native backend."
            ) from original_error
    _hivex_module = module
    return module

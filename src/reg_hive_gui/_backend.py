"""Select the platform-native registry hive backend lazily."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from ._hivex import load_hivex


def load_backend():
    if sys.platform == "win32":
        from .offreg import OffregHive

        return SimpleNamespace(Hivex=OffregHive)
    return load_hivex()

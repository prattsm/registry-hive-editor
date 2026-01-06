"""Plugin loading helpers."""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable

from .hive import Hive


@dataclass(frozen=True)
class Plugin:
    name: str
    description: str
    analyze: Callable[[Hive], list[dict[str, object]]]
    module: ModuleType


def load_plugins(search_paths: Iterable[Path]) -> list[Plugin]:
    plugins: list[Plugin] = []
    for base in search_paths:
        if not base.exists():
            continue
        for path in sorted(base.glob("*.py")):
            if path.name.startswith("__"):
                continue
            module = _load_module(path)
            if module is None:
                continue
            analyze = getattr(module, "analyze", None)
            if not callable(analyze):
                continue
            name = getattr(module, "PLUGIN_NAME", path.stem)
            description = getattr(module, "PLUGIN_DESCRIPTION", "")
            plugins.append(Plugin(name=name, description=description, analyze=analyze, module=module))
    return plugins


def _load_module(path: Path) -> ModuleType | None:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        return None
    return module

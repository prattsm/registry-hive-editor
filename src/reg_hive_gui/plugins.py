"""Deferred plugin discovery, validation, and isolated execution."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Callable

from .hive import Hive

MAX_PLUGIN_ROWS = 100_000
MAX_PLUGIN_COLUMNS = 256
MAX_PLUGIN_TEXT_LENGTH = 1_000_000
DEFAULT_PLUGIN_TIMEOUT_SECONDS = 120


def user_plugin_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RegistryHiveEditor" / "plugins"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reg_hive_gui" / "plugins"


@dataclass(frozen=True)
class Plugin:
    name: str
    description: str
    path: Path
    trusted: bool = False
    version: str = "1"
    target_hives: tuple[str, ...] = ()
    required_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginLoadError:
    path: Path
    message: str


def discover_plugins(
    search_paths: Iterable[Path | tuple[Path, bool]],
) -> tuple[list[Plugin], list[PluginLoadError]]:
    """Inspect plugin metadata without importing or executing plugin code."""
    plugins: list[Plugin] = []
    errors: list[PluginLoadError] = []
    seen_paths: set[Path] = set()
    for entry in search_paths:
        base, trusted = entry if isinstance(entry, tuple) else (entry, False)
        if not base.exists():
            continue
        for path in sorted(base.glob("*.py")):
            if path.name.startswith("__"):
                continue
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                plugin = _inspect_plugin(path, trusted=trusted)
            except (OSError, SyntaxError, TypeError, ValueError) as exc:
                errors.append(PluginLoadError(path=path, message=str(exc)))
            else:
                plugins.append(plugin)
    plugins.sort(key=lambda plugin: (plugin.name.casefold(), str(plugin.path).casefold()))
    return plugins, errors


def _inspect_plugin(path: Path, *, trusted: bool) -> Plugin:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    metadata: dict[str, object] = {}
    has_analyze = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "analyze":
            has_analyze = True
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in {
                "PLUGIN_NAME",
                "PLUGIN_DESCRIPTION",
                "PLUGIN_VERSION",
                "PLUGIN_TARGET_HIVES",
                "PLUGIN_REQUIRED_PATHS",
            }:
                continue
            try:
                metadata[target.id] = ast.literal_eval(value)
            except (ValueError, TypeError) as exc:
                raise TypeError(f"{target.id} must be literal metadata") from exc
    if not has_analyze:
        raise ValueError("Plugin must define analyze(hive)")
    name = _literal_text(metadata.get("PLUGIN_NAME", path.stem), "PLUGIN_NAME").strip()
    description = _literal_text(
        metadata.get("PLUGIN_DESCRIPTION", ""), "PLUGIN_DESCRIPTION"
    ).strip()
    version = _literal_text(metadata.get("PLUGIN_VERSION", "1"), "PLUGIN_VERSION").strip()
    target_hives = _literal_text_sequence(
        metadata.get("PLUGIN_TARGET_HIVES", ()), "PLUGIN_TARGET_HIVES"
    )
    required_paths = _literal_text_sequence(
        metadata.get("PLUGIN_REQUIRED_PATHS", ()), "PLUGIN_REQUIRED_PATHS"
    )
    if not name:
        raise ValueError("PLUGIN_NAME cannot be empty")
    if not version:
        raise ValueError("PLUGIN_VERSION cannot be empty")
    return Plugin(
        name=name,
        description=description,
        path=path.resolve(),
        trusted=trusted,
        version=version,
        target_hives=target_hives,
        required_paths=required_paths,
    )


def _literal_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a literal string")
    return value


def _literal_text_sequence(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise TypeError(f"{name} must be a literal list or tuple of non-empty strings")
    return tuple(item.strip() for item in value)


def plugin_applicability(plugin: Plugin, hive: Hive | None) -> tuple[bool, str]:
    """Return whether a plugin has at least one of its identifying paths."""
    targets = ", ".join(plugin.target_hives) or "any hive"
    if hive is None:
        return False, f"Open a hive first. Intended for: {targets}."
    if not plugin.required_paths:
        return True, f"Intended for: {targets}."
    for path in plugin.required_paths:
        try:
            if hive.get_node(path) is not None:
                return True, f"Matched identifying path: {path}. Intended for: {targets}."
        except Exception:  # noqa: BLE001
            continue
    return False, f"No identifying paths found. Intended for: {targets}."


def load_plugin_module(plugin: Plugin) -> ModuleType:
    module_name = f"reg_hive_gui_plugin_{abs(hash(plugin.path))}"
    spec = importlib.util.spec_from_file_location(module_name, plugin.path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin: {plugin.path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    analyze = getattr(module, "analyze", None)
    if not callable(analyze):
        raise TypeError("Plugin analyze attribute is not callable")
    return module


def execute_plugin(plugin: Plugin, hive: Hive) -> list[dict[str, object]]:
    module = load_plugin_module(plugin)
    analyze: Callable[[Hive], object] = module.analyze
    return normalize_plugin_rows(analyze(hive))


def normalize_plugin_rows(result: object) -> list[dict[str, object]]:
    if not isinstance(result, list):
        raise TypeError("Plugin analyze() must return a list of row dictionaries")
    if len(result) > MAX_PLUGIN_ROWS:
        raise ValueError(f"Plugin returned more than {MAX_PLUGIN_ROWS} rows")
    rows: list[dict[str, object]] = []
    for row_index, raw_row in enumerate(result):
        if not isinstance(raw_row, Mapping):
            raise TypeError(f"Plugin row {row_index} is not a dictionary")
        if len(raw_row) > MAX_PLUGIN_COLUMNS:
            raise ValueError(f"Plugin row {row_index} has more than {MAX_PLUGIN_COLUMNS} columns")
        row: dict[str, object] = {}
        for raw_key, raw_value in raw_row.items():
            if not isinstance(raw_key, str):
                raise TypeError(f"Plugin row {row_index} contains a non-text column name")
            row[raw_key] = _normalize_plugin_value(raw_value)
        rows.append(row)
    return rows


def _normalize_plugin_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_PLUGIN_TEXT_LENGTH:
            raise ValueError("Plugin text value exceeds the size limit")
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(" ")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized = {str(key): _normalize_plugin_value(item) for key, item in value.items()}
        return json.dumps(normalized, sort_keys=True)
    if isinstance(value, Sequence):
        normalized = [_normalize_plugin_value(item) for item in value]
        return json.dumps(normalized)
    raise TypeError(f"Plugin returned an unsupported value type: {type(value).__name__}")


def run_plugin_subprocess(
    plugin: Plugin,
    hive_path: Path,
    *,
    timeout_seconds: int = DEFAULT_PLUGIN_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    command = [
        sys.executable,
        "-m",
        "reg_hive_gui.plugin_runner",
        str(plugin.path),
        str(hive_path),
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=creation_flags,
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired as exc:
            if cancelled is not None and cancelled():
                _stop_plugin_process(process)
                raise CancelledError("Plugin execution cancelled") from exc
            if time.monotonic() >= deadline:
                _stop_plugin_process(process)
                raise TimeoutError(
                    f"Plugin exceeded the {timeout_seconds}-second time limit"
                ) from exc
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail = stderr.strip() or "plugin process returned invalid output"
        raise RuntimeError(detail) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Plugin process returned an invalid response")
    if process.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "Plugin failed"))
    return normalize_plugin_rows(payload.get("rows"))


def _stop_plugin_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()

"""Child-process entrypoint for plugin execution."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

from .hive import Hive
from .plugins import Plugin, execute_plugin


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(json.dumps({"ok": False, "error": "Expected a plugin path and hive path"}))
        return 2
    plugin_path, hive_path = map(Path, args)
    plugin = Plugin(name=plugin_path.stem, description="", path=plugin_path)
    captured_output = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_output), contextlib.redirect_stderr(captured_output):
            with Hive(hive_path, write=False) as hive:
                rows = execute_plugin(plugin, hive)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        log = captured_output.getvalue().strip()
        if log:
            error = f"{error}\n\nPlugin output:\n{log[-4000:]}"
        print(json.dumps({"ok": False, "error": error}))
        return 1
    print(json.dumps({"ok": True, "rows": rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Application entrypoint for the Registry Hive GUI."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtWidgets

from .gui import HiveMainWindow


def resolve_hive_argument(args: list[str]) -> tuple[Path | None, str | None]:
    """Validate the optional hive path before constructing the GUI."""
    if len(args) > 1:
        return None, "Expected at most one hive path."
    if not args:
        return None, None
    path = Path(args[0]).expanduser()
    if not path.exists():
        return None, f"Hive file does not exist: {path}"
    if not path.is_file():
        return None, f"Hive path is not a file: {path}"
    return path.resolve(), None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    hive_path, error = resolve_hive_argument(args)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    app = QtWidgets.QApplication([sys.argv[0], *args])
    window = HiveMainWindow()
    window.show()
    if hive_path is not None:
        window.load_hive(hive_path)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

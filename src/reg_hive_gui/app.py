"""Application entrypoint for the Registry Hive GUI."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtWidgets

from .gui import HiveMainWindow


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    app = QtWidgets.QApplication(sys.argv)
    window = HiveMainWindow()
    window.show()
    if args:
        hive_path = Path(args[0])
        if hive_path.exists():
            window.load_hive(hive_path)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

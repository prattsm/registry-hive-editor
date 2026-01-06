#!/usr/bin/env python3
"""Launch the Registry Hive GUI."""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reg_hive_gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())

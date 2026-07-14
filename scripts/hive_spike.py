#!/usr/bin/env python3
"""Cross-platform backend smoke test using the application's safe export path."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reg_hive_gui.hive import Hive, RegistryType  # noqa: E402

INPUT_HIVE = ROOT_DIR / "sample_hives" / "SOFTWARE"
OUTPUT_HIVE = ROOT_DIR / "out" / "SOFTWARE_modified"
TARGET_PATH = "Microsoft\\Windows NT\\CurrentVersion"


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    if not INPUT_HIVE.is_file():
        raise SystemExit(f"Input hive missing: {INPUT_HIVE}")
    original_hash = sha256_file(INPUT_HIVE)
    with Hive(INPUT_HIVE, write=True) as hive:
        print(f"Backend: {hive.backend_name}")
        for name in ("ProductName", "EditionID", "CurrentBuild", "ProductId"):
            value = hive.get_value(TARGET_PATH, name)
            if value is not None:
                print(f"{TARGET_PATH}\\{value.name} = {value.decoded}")
                break
        spike_path = "CodexHiveSpike"
        if hive.get_node(spike_path) is None:
            hive.create_key(spike_path)
        existing = hive.get_value(spike_path, "TestValue")
        if existing is None:
            hive.create_value(
                spike_path, "TestValue", RegistryType.REG_SZ, "Created by hive_spike.py"
            )
        else:
            hive.replace_value(
                spike_path,
                existing.name,
                existing.name,
                RegistryType.REG_SZ,
                "Created by hive_spike.py",
            )
        hive.export(OUTPUT_HIVE)
    if sha256_file(INPUT_HIVE) != original_hash:
        raise SystemExit("Original hive hash changed")
    print(f"Original preserved; modified hive written to {OUTPUT_HIVE}")


if __name__ == "__main__":
    main()

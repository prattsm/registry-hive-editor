#!/usr/bin/env python3
"""Feasibility spike: hive in/out using hivex (offline registry)."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from typing import Iterable

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reg_hive_gui._hivex import hivex


INPUT_HIVE = ROOT_DIR / "sample_hives" / "SOFTWARE"
OUTPUT_HIVE = ROOT_DIR / "out" / "SOFTWARE_modified"
REG_SZ = getattr(hivex, "REG_SZ", 1)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_child_path(handle: hivex.Hivex, path_parts: Iterable[str]) -> int | None:
    node = handle.root()
    for part in path_parts:
        child = handle.node_get_child(node, part)
        if not child:
            return None
        node = child
    return node


def iter_node_values(handle: hivex.Hivex, node: int) -> list[int]:
    if hasattr(handle, "node_get_values"):
        return list(handle.node_get_values(node))
    if hasattr(handle, "node_values"):
        return list(handle.node_values(node))
    return []


def get_value_by_name(handle: hivex.Hivex, node: int, name: str) -> int | None:
    if hasattr(handle, "node_get_value"):
        value = handle.node_get_value(node, name)
        return value or None
    for value in iter_node_values(handle, node):
        if handle.value_key(value) == name:
            return value
    return None


def decode_reg_sz(raw: bytes) -> str:
    try:
        text = raw.decode("utf-16le", errors="replace")
    except Exception:
        return repr(raw)
    return text.rstrip("\x00")


def encode_reg_sz(text: str) -> bytes:
    return (text + "\x00").encode("utf-16le")


def normalize_value_type(value_type: object) -> int:
    if isinstance(value_type, tuple) and value_type:
        return int(value_type[0])
    return int(value_type)


def normalize_value_data(value_data: object) -> tuple[int | None, bytes]:
    if isinstance(value_data, tuple):
        if len(value_data) == 2 and isinstance(value_data[1], (bytes, bytearray)):
            return int(value_data[0]), bytes(value_data[1])
        if len(value_data) == 3 and isinstance(value_data[2], (bytes, bytearray)):
            return int(value_data[1]), bytes(value_data[2])
    if isinstance(value_data, bytearray):
        return None, bytes(value_data)
    return None, value_data


def main() -> None:
    if not INPUT_HIVE.exists():
        raise SystemExit(f"Input hive missing: {INPUT_HIVE}")

    original_hash_before = sha256_file(INPUT_HIVE)
    print(f"Original hive SHA256 (before): {original_hash_before}")

    handle = hivex.Hivex(str(INPUT_HIVE), write=True)
    try:
        target_path = ["Microsoft", "Windows NT", "CurrentVersion"]
        target_node = find_child_path(handle, target_path)
        candidate_names = ["ProductName", "EditionID", "CurrentBuild", "ProductId"]
        value = None
        if target_node is not None:
            for name in candidate_names:
                value = get_value_by_name(handle, target_node, name)
                if value is not None:
                    selected_name = name
                    break
            else:
                selected_name = None
        else:
            value = None

        if target_node is not None and value is not None and selected_name is not None:
            value_type = normalize_value_type(handle.value_type(value))
            value_data = handle.value_value(value)
            inferred_type, data_bytes = normalize_value_data(value_data)
            if inferred_type is not None:
                value_type = inferred_type
            decoded = decode_reg_sz(data_bytes) if value_type == REG_SZ else data_bytes
            print("Read known value:")
            print(f"  Path: {'\\\\'.join(target_path)}")
            print(f"  Name: {selected_name}")
            print(f"  Type: {value_type}")
            print(f"  Data: {decoded}")
        else:
            root = handle.root()
            children = handle.node_children(root)
            if not children:
                raise SystemExit("No keys found in hive; cannot read value.")
            first_child = children[0]
            values = iter_node_values(handle, first_child)
            if not values:
                raise SystemExit("First key has no values; cannot read value.")
            first_value = values[0]
            print("Known path/value not found; fallback to first available value:")
            print(f"  Path: {handle.node_name(first_child)}")
            print(f"  Name: {handle.value_key(first_value)}")
            value_type = normalize_value_type(handle.value_type(first_value))
            raw = handle.value_value(first_value)
            inferred_type, data_bytes = normalize_value_data(raw)
            if inferred_type is not None:
                value_type = inferred_type
            print(f"  Type: {value_type}")
            print(f"  Data (raw): {data_bytes}")

        root = handle.root()
        spike_key = handle.node_get_child(root, "CodexHiveSpike")
        if not spike_key:
            spike_key = handle.node_add_child(root, "CodexHiveSpike")

        test_value = {
            "key": "TestValue",
            "t": REG_SZ,
            "value": encode_reg_sz("Created by hive_spike.py"),
        }
        handle.node_set_value(spike_key, test_value)

        OUTPUT_HIVE.parent.mkdir(parents=True, exist_ok=True)
        if OUTPUT_HIVE.exists():
            OUTPUT_HIVE.unlink()
        handle.commit(str(OUTPUT_HIVE))
    finally:
        if hasattr(handle, "close"):
            handle.close()

    original_hash_after = sha256_file(INPUT_HIVE)
    print(f"Original hive SHA256 (after):  {original_hash_after}")
    if original_hash_before != original_hash_after:
        raise SystemExit("Original hive hash changed! Aborting.")

    print(f"Modified hive written to: {OUTPUT_HIVE}")


if __name__ == "__main__":
    main()

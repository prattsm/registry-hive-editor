"""Report export helpers for hive data."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from .hive import Hive, HiveValue


def value_to_row(path: str, value: HiveValue) -> dict[str, object]:
    return {
        "kind": "value",
        "path": path,
        "value_name": value.name,
        "value_type": value.type,
        "value_type_name": value.type_name,
        "value_data_hex": value.data.hex(" "),
        "value_decoded": value.decoded,
    }


def key_to_row(path: str, timestamp: str | None) -> dict[str, object]:
    return {
        "kind": "key",
        "path": path,
        "timestamp": timestamp or "",
    }


def subtree_to_rows(hive: Hive, start_path: str | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path, node in hive.iter_key_nodes(start_path):
        timestamp = hive.get_node_timestamp(node)
        rows.append(key_to_row(path, timestamp.isoformat() if timestamp else None))
        for value in hive.iter_values_for_node(node):
            rows.append(value_to_row(path, value))
    return rows


def export_rows(rows: Iterable[dict[str, object]], output_path: Path | str) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    if suffix == ".json":
        with output.open("w", encoding="utf-8") as handle:
            json.dump(list(rows), handle, indent=2, default=str)
        return output
    if suffix == ".csv":
        rows_list = list(rows)
        fieldnames = sorted({key for row in rows_list for key in row.keys()})
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows_list)
        return output
    raise ValueError("Output path must end with .json or .csv")

"""Report export helpers for hive data."""
from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from .fileio import staged_output
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


def key_to_row(
    path: str, timestamp: str | None, timestamp_raw: int | None = None
) -> dict[str, object]:
    return {
        "kind": "key",
        "path": path,
        "timestamp": timestamp or "",
        "timestamp_raw": timestamp_raw,
    }


SUBTREE_FIELDNAMES = (
    "kind",
    "path",
    "timestamp",
    "timestamp_raw",
    "value_name",
    "value_type",
    "value_type_name",
    "value_data_hex",
    "value_decoded",
)


def iter_subtree_rows(
    hive: Hive, start_path: str | None = None
) -> Iterator[dict[str, object]]:
    for path, node in hive.iter_key_nodes(start_path):
        timestamp = hive.get_node_timestamp_info(node)
        yield key_to_row(path, timestamp.display, timestamp.raw)
        for value in hive.iter_values_for_node(node):
            yield value_to_row(path, value)


def subtree_to_rows(hive: Hive, start_path: str | None = None) -> list[dict[str, object]]:
    return list(iter_subtree_rows(hive, start_path))


def export_rows(
    rows: Iterable[dict[str, object]],
    output_path: Path | str,
    *,
    fieldnames: Sequence[str] | None = None,
) -> Path:
    output = Path(output_path)
    suffix = output.suffix.lower()
    if suffix == ".json":
        with staged_output(output) as temporary:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write("[\n")
                first = True
                for row in rows:
                    if not first:
                        handle.write(",\n")
                    handle.write("  ")
                    json.dump(row, handle, default=str)
                    first = False
                handle.write("\n]\n")
        return output
    if suffix == ".csv":
        rows_list: list[dict[str, object]] | None = None
        if fieldnames is None:
            rows_list = list(rows)
            fieldnames = sorted({key for row in rows_list for key in row.keys()})
        with staged_output(output) as temporary:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows if rows_list is None else rows_list)
        return output
    raise ValueError("Output path must end with .json or .csv")


def export_subtree(
    hive: Hive,
    start_path: str | None,
    output_path: Path | str,
) -> Path:
    return export_rows(
        iter_subtree_rows(hive, start_path),
        output_path,
        fieldnames=SUBTREE_FIELDNAMES,
    )

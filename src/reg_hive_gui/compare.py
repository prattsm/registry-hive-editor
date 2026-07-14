"""Scalable, case-insensitive hive comparison utilities."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .hive import Hive, decode_value, filetime_to_datetime


@dataclass(frozen=True)
class DiffEntry:
    kind: str  # "key" or "value"
    change: str  # "added", "removed", "modified"
    path: str
    value_name: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    old_value_name: str | None = None
    new_value_name: str | None = None
    old_type: int | None = None
    new_type: int | None = None
    old_data: bytes | None = None
    new_data: bytes | None = None
    old_decoded: object | None = None
    new_decoded: object | None = None
    old_timestamp: datetime | None = None
    new_timestamp: datetime | None = None
    old_timestamp_raw: int | None = None
    new_timestamp_raw: int | None = None
    correlation: str | None = None
    correlated_value_name: str | None = None
    first_changed_offset: int | None = None
    changed_byte_count: int | None = None
    changed_ranges: tuple[tuple[int, int], ...] = ()


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise CancelledError("Hive comparison cancelled")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE keys (
            side INTEGER NOT NULL,
            normalized_path TEXT NOT NULL,
            path TEXT NOT NULL,
            timestamp_raw TEXT,
            PRIMARY KEY (side, normalized_path)
        ) WITHOUT ROWID;
        CREATE TABLE registry_values (
            side INTEGER NOT NULL,
            normalized_path TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            name TEXT NOT NULL,
            value_type INTEGER NOT NULL,
            data BLOB NOT NULL,
            PRIMARY KEY (side, normalized_path, normalized_name)
        ) WITHOUT ROWID;
        """
    )


def _index_hive(
    connection: sqlite3.Connection,
    hive: Hive,
    side: int,
    cancelled: Callable[[], bool] | None,
) -> None:
    try:
        with connection:
            for path, node in hive.iter_key_nodes():
                _check_cancelled(cancelled)
                timestamp = hive.get_node_timestamp_info(node)
                normalized_path = path.casefold()
                connection.execute(
                    "INSERT INTO keys VALUES (?, ?, ?, ?)",
                    (
                        side,
                        normalized_path,
                        path,
                        str(timestamp.raw) if timestamp.raw is not None else None,
                    ),
                )
                value_rows: list[tuple[object, ...]] = []
                for value in hive.iter_values_for_node(node):
                    _check_cancelled(cancelled)
                    value_rows.append(
                        (
                            side,
                            normalized_path,
                            value.name.casefold(),
                            value.name,
                            value.type,
                            sqlite3.Binary(value.data),
                        )
                    )
                    if len(value_rows) >= 1_000:
                        connection.executemany(
                            "INSERT INTO registry_values VALUES (?, ?, ?, ?, ?, ?)",
                            value_rows,
                        )
                        value_rows.clear()
                connection.executemany(
                    "INSERT INTO registry_values VALUES (?, ?, ?, ?, ?, ?)", value_rows
                )
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            "Hive contains duplicate key paths or value names that differ only by case"
        ) from exc


def _raw_timestamp(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _iter_key_pairs(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    yield from connection.execute(
        """
        WITH paths AS (
            SELECT normalized_path FROM keys WHERE side = 0
            UNION
            SELECT normalized_path FROM keys WHERE side = 1
        )
        SELECT paths.normalized_path,
               left_key.path AS old_path,
               left_key.timestamp_raw AS old_timestamp_raw,
               right_key.path AS new_path,
               right_key.timestamp_raw AS new_timestamp_raw
        FROM paths
        LEFT JOIN keys AS left_key
          ON left_key.side = 0 AND left_key.normalized_path = paths.normalized_path
        LEFT JOIN keys AS right_key
          ON right_key.side = 1 AND right_key.normalized_path = paths.normalized_path
        ORDER BY paths.normalized_path
        """
    )


def _iter_value_pairs(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    yield from connection.execute(
        """
        WITH value_names AS (
            SELECT normalized_path, normalized_name FROM registry_values WHERE side = 0
            UNION
            SELECT normalized_path, normalized_name FROM registry_values WHERE side = 1
        )
        SELECT value_names.normalized_path,
               left_key.path AS old_path,
               right_key.path AS new_path,
               left_value.name AS old_name,
               left_value.value_type AS old_type,
               left_value.data AS old_data,
               right_value.name AS new_name,
               right_value.value_type AS new_type,
               right_value.data AS new_data
        FROM value_names
        LEFT JOIN registry_values AS left_value
          ON left_value.side = 0
         AND left_value.normalized_path = value_names.normalized_path
         AND left_value.normalized_name = value_names.normalized_name
        LEFT JOIN registry_values AS right_value
          ON right_value.side = 1
         AND right_value.normalized_path = value_names.normalized_path
         AND right_value.normalized_name = value_names.normalized_name
        LEFT JOIN keys AS left_key
          ON left_key.side = 0 AND left_key.normalized_path = value_names.normalized_path
        LEFT JOIN keys AS right_key
          ON right_key.side = 1 AND right_key.normalized_path = value_names.normalized_path
        ORDER BY value_names.normalized_path, value_names.normalized_name
        """
    )


def _key_diff(row: sqlite3.Row) -> DiffEntry | None:
    old_path = row["old_path"]
    new_path = row["new_path"]
    old_raw = _raw_timestamp(row["old_timestamp_raw"])
    new_raw = _raw_timestamp(row["new_timestamp_raw"])
    if old_path is None:
        return DiffEntry(
            kind="key",
            change="added",
            path=new_path,
            new_path=new_path,
            new_timestamp=filetime_to_datetime(new_raw),
            new_timestamp_raw=new_raw,
        )
    if new_path is None:
        return DiffEntry(
            kind="key",
            change="removed",
            path=old_path,
            old_path=old_path,
            old_timestamp=filetime_to_datetime(old_raw),
            old_timestamp_raw=old_raw,
        )
    if old_path != new_path or old_raw != new_raw:
        return DiffEntry(
            kind="key",
            change="modified",
            path=new_path,
            old_path=old_path,
            new_path=new_path,
            old_timestamp=filetime_to_datetime(old_raw),
            new_timestamp=filetime_to_datetime(new_raw),
            old_timestamp_raw=old_raw,
            new_timestamp_raw=new_raw,
        )
    return None


def _value_diff(row: sqlite3.Row) -> DiffEntry | None:
    old_name = row["old_name"]
    new_name = row["new_name"]
    old_type = row["old_type"]
    new_type = row["new_type"]
    old_data = bytes(row["old_data"]) if row["old_data"] is not None else None
    new_data = bytes(row["new_data"]) if row["new_data"] is not None else None
    old_path = row["old_path"]
    new_path = row["new_path"]
    if old_name is None:
        return DiffEntry(
            kind="value",
            change="added",
            path=new_path,
            value_name=new_name,
            new_path=new_path,
            new_value_name=new_name,
            new_type=new_type,
            new_data=new_data,
            new_decoded=decode_value(new_type, new_data),
        )
    if new_name is None:
        return DiffEntry(
            kind="value",
            change="removed",
            path=old_path,
            value_name=old_name,
            old_path=old_path,
            old_value_name=old_name,
            old_type=old_type,
            old_data=old_data,
            old_decoded=decode_value(old_type, old_data),
        )
    if old_name != new_name or old_type != new_type or old_data != new_data:
        first_offset, changed_count, changed_ranges = _binary_change_details(
            old_data, new_data
        )
        return DiffEntry(
            kind="value",
            change="modified",
            path=new_path,
            value_name=new_name,
            old_path=old_path,
            new_path=new_path,
            old_value_name=old_name,
            new_value_name=new_name,
            old_type=old_type,
            new_type=new_type,
            old_data=old_data,
            new_data=new_data,
            old_decoded=decode_value(old_type, old_data),
            new_decoded=decode_value(new_type, new_data),
            first_changed_offset=first_offset,
            changed_byte_count=changed_count,
            changed_ranges=changed_ranges,
        )
    return None


def _binary_change_details(
    old_data: bytes, new_data: bytes
) -> tuple[int | None, int, tuple[tuple[int, int], ...]]:
    """Describe differing byte positions as half-open contiguous ranges."""
    ranges: list[tuple[int, int]] = []
    range_start: int | None = None
    changed_count = 0
    for offset in range(max(len(old_data), len(new_data))):
        changed = (
            offset >= len(old_data)
            or offset >= len(new_data)
            or old_data[offset] != new_data[offset]
        )
        if changed:
            changed_count += 1
            if range_start is None:
                range_start = offset
        elif range_start is not None:
            ranges.append((range_start, offset))
            range_start = None
    if range_start is not None:
        ranges.append((range_start, max(len(old_data), len(new_data))))
    first_offset = ranges[0][0] if ranges else None
    return first_offset, changed_count, tuple(ranges)


def correlate_probable_value_renames(entries: Iterable[DiffEntry]) -> list[DiffEntry]:
    """Annotate unambiguous same-key/type/data remove-add pairs as probable renames."""
    result = list(entries)
    removed: dict[tuple[str, int, bytes], list[int]] = {}
    added: dict[tuple[str, int, bytes], list[int]] = {}
    for index, entry in enumerate(result):
        if entry.kind != "value":
            continue
        if entry.change == "removed" and entry.old_type is not None and entry.old_data is not None:
            key = (entry.path.casefold(), entry.old_type, entry.old_data)
            removed.setdefault(key, []).append(index)
        elif entry.change == "added" and entry.new_type is not None and entry.new_data is not None:
            key = (entry.path.casefold(), entry.new_type, entry.new_data)
            added.setdefault(key, []).append(index)
    for key in removed.keys() & added.keys():
        old_indexes = removed[key]
        new_indexes = added[key]
        if len(old_indexes) != 1 or len(new_indexes) != 1:
            continue
        old_index = old_indexes[0]
        new_index = new_indexes[0]
        old_name = result[old_index].old_value_name or ""
        new_name = result[new_index].new_value_name or ""
        if old_name.casefold() == new_name.casefold():
            continue
        result[old_index] = replace(
            result[old_index],
            correlation="probable_value_rename",
            correlated_value_name=new_name,
        )
        result[new_index] = replace(
            result[new_index],
            correlation="probable_value_rename",
            correlated_value_name=old_name,
        )
    return result


def summarize_diffs(entries: Iterable[DiffEntry]) -> dict[str, int]:
    entries_list = list(entries)
    return {
        "total": len(entries_list),
        "added": sum(entry.change == "added" for entry in entries_list),
        "removed": sum(entry.change == "removed" for entry in entries_list),
        "modified": sum(entry.change == "modified" for entry in entries_list),
        "probable_value_renames": sum(
            entry.correlation == "probable_value_rename" for entry in entries_list
        )
        // 2,
    }


def diff_hives(
    left: Hive,
    right: Hive,
    *,
    cancelled: Callable[[], bool] | None = None,
    index_directory: Path | None = None,
) -> list[DiffEntry]:
    """Compare two hives using a bounded-memory temporary on-disk index."""
    descriptor, raw_path = tempfile.mkstemp(
        prefix="reg_hive_compare_", suffix=".sqlite", dir=index_directory
    )
    os.close(descriptor)
    index_path = Path(raw_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(index_path)
        connection.row_factory = sqlite3.Row
        _create_schema(connection)
        _index_hive(connection, left, 0, cancelled)
        _index_hive(connection, right, 1, cancelled)
        diffs: list[DiffEntry] = []
        for row in _iter_key_pairs(connection):
            _check_cancelled(cancelled)
            entry = _key_diff(row)
            if entry is not None:
                diffs.append(entry)
        for row in _iter_value_pairs(connection):
            _check_cancelled(cancelled)
            entry = _value_diff(row)
            if entry is not None:
                diffs.append(entry)
        diffs.sort(
            key=lambda entry: (
                entry.path.casefold(),
                0 if entry.kind == "key" else 1,
                (entry.value_name or "").casefold(),
                entry.change,
            )
        )
        return correlate_probable_value_renames(diffs)
    finally:
        if connection is not None:
            connection.close()
        try:
            index_path.unlink()
        except FileNotFoundError:
            pass


def diff_entries_to_rows(entries: Iterable[DiffEntry]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for entry in entries:
        row: dict[str, object] = {
            "kind": entry.kind,
            "change": entry.change,
            "path": entry.path,
            "old_path": entry.old_path or "",
            "new_path": entry.new_path or "",
            "value_name": entry.value_name or "",
            "old_value_name": entry.old_value_name or "",
            "new_value_name": entry.new_value_name or "",
            "old_type": entry.old_type,
            "new_type": entry.new_type,
            "old_data_hex": entry.old_data.hex(" ") if entry.old_data is not None else "",
            "new_data_hex": entry.new_data.hex(" ") if entry.new_data is not None else "",
            "old_decoded": entry.old_decoded,
            "new_decoded": entry.new_decoded,
            "old_timestamp": entry.old_timestamp.isoformat() if entry.old_timestamp else "",
            "new_timestamp": entry.new_timestamp.isoformat() if entry.new_timestamp else "",
            "old_timestamp_raw": entry.old_timestamp_raw,
            "new_timestamp_raw": entry.new_timestamp_raw,
            "correlation": entry.correlation or "",
            "correlated_value_name": entry.correlated_value_name or "",
            "first_changed_offset": entry.first_changed_offset,
            "changed_byte_count": entry.changed_byte_count,
            "changed_ranges": ", ".join(
                f"0x{start:08X}-0x{end - 1:08X}" for start, end in entry.changed_ranges
            ),
        }
        rows.append(row)
    return rows

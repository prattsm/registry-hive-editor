"""Hive comparison utilities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .hive import Hive, HiveValue


@dataclass(frozen=True)
class DiffEntry:
    kind: str  # "key" or "value"
    change: str  # "added", "removed", "modified"
    path: str
    value_name: str | None = None
    old_type: int | None = None
    new_type: int | None = None
    old_data: bytes | None = None
    new_data: bytes | None = None
    old_decoded: object | None = None
    new_decoded: object | None = None
    old_timestamp: datetime | None = None
    new_timestamp: datetime | None = None


@dataclass(frozen=True)
class _KeySnapshot:
    timestamp: datetime | None
    values: dict[str, HiveValue]


def _snapshot_hive(hive: Hive) -> dict[str, _KeySnapshot]:
    snapshot: dict[str, _KeySnapshot] = {}
    for path, node in hive.iter_key_nodes():
        values = {value.name: value for value in hive.iter_values_for_node(node)}
        snapshot[path] = _KeySnapshot(timestamp=hive.get_node_timestamp(node), values=values)
    return snapshot


def diff_hives(left: Hive, right: Hive) -> list[DiffEntry]:
    left_snapshot = _snapshot_hive(left)
    right_snapshot = _snapshot_hive(right)

    left_keys = set(left_snapshot)
    right_keys = set(right_snapshot)

    diffs: list[DiffEntry] = []

    for path in sorted(right_keys - left_keys):
        entry = DiffEntry(
            kind="key",
            change="added",
            path=path,
            new_timestamp=right_snapshot[path].timestamp,
        )
        diffs.append(entry)
        for value in right_snapshot[path].values.values():
            diffs.append(
                DiffEntry(
                    kind="value",
                    change="added",
                    path=path,
                    value_name=value.name,
                    new_type=value.type,
                    new_data=value.data,
                    new_decoded=value.decoded,
                )
            )

    for path in sorted(left_keys - right_keys):
        entry = DiffEntry(
            kind="key",
            change="removed",
            path=path,
            old_timestamp=left_snapshot[path].timestamp,
        )
        diffs.append(entry)
        for value in left_snapshot[path].values.values():
            diffs.append(
                DiffEntry(
                    kind="value",
                    change="removed",
                    path=path,
                    value_name=value.name,
                    old_type=value.type,
                    old_data=value.data,
                    old_decoded=value.decoded,
                )
            )

    for path in sorted(left_keys & right_keys):
        left_key = left_snapshot[path]
        right_key = right_snapshot[path]
        if left_key.timestamp != right_key.timestamp:
            diffs.append(
                DiffEntry(
                    kind="key",
                    change="modified",
                    path=path,
                    old_timestamp=left_key.timestamp,
                    new_timestamp=right_key.timestamp,
                )
            )
        left_values = left_key.values
        right_values = right_key.values
        left_names = set(left_values)
        right_names = set(right_values)

        for name in sorted(right_names - left_names):
            value = right_values[name]
            diffs.append(
                DiffEntry(
                    kind="value",
                    change="added",
                    path=path,
                    value_name=name,
                    new_type=value.type,
                    new_data=value.data,
                    new_decoded=value.decoded,
                )
            )

        for name in sorted(left_names - right_names):
            value = left_values[name]
            diffs.append(
                DiffEntry(
                    kind="value",
                    change="removed",
                    path=path,
                    value_name=name,
                    old_type=value.type,
                    old_data=value.data,
                    old_decoded=value.decoded,
                )
            )

        for name in sorted(left_names & right_names):
            left_value = left_values[name]
            right_value = right_values[name]
            if left_value.type != right_value.type or left_value.data != right_value.data:
                diffs.append(
                    DiffEntry(
                        kind="value",
                        change="modified",
                        path=path,
                        value_name=name,
                        old_type=left_value.type,
                        new_type=right_value.type,
                        old_data=left_value.data,
                        new_data=right_value.data,
                        old_decoded=left_value.decoded,
                        new_decoded=right_value.decoded,
                    )
                )
    return diffs


def diff_entries_to_rows(entries: Iterable[DiffEntry]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for entry in entries:
        row: dict[str, object] = {
            "kind": entry.kind,
            "change": entry.change,
            "path": entry.path,
            "value_name": entry.value_name or "",
            "old_type": entry.old_type,
            "new_type": entry.new_type,
            "old_data_hex": entry.old_data.hex(" ") if entry.old_data else "",
            "new_data_hex": entry.new_data.hex(" ") if entry.new_data else "",
            "old_decoded": entry.old_decoded,
            "new_decoded": entry.new_decoded,
            "old_timestamp": entry.old_timestamp.isoformat() if entry.old_timestamp else "",
            "new_timestamp": entry.new_timestamp.isoformat() if entry.new_timestamp else "",
        }
        rows.append(row)
    return rows

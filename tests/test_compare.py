from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import CancelledError
from pathlib import Path

import pytest

from reg_hive_gui.compare import diff_entries_to_rows, diff_hives, summarize_diffs
from reg_hive_gui.hive import HiveTimestamp, HiveValue, RegistryType, encode_value


class FakeHive:
    def __init__(
        self,
        keys: dict[str, tuple[int | None, list[tuple[str, int, object]]]],
    ) -> None:
        self._keys = list(keys.items())

    def iter_key_nodes(self) -> Iterator[tuple[str, int]]:
        for node, (path, _data) in enumerate(self._keys, start=1):
            yield path, node

    def get_node_timestamp_info(self, node: int) -> HiveTimestamp:
        raw = self._keys[node - 1][1][0]
        return HiveTimestamp(raw=raw, value=None)

    def iter_values_for_node(self, node: int) -> Iterator[HiveValue]:
        for name, value_type, value in self._keys[node - 1][1][1]:
            data = encode_value(value_type, value)
            yield HiveValue(name, value_type, data, value)


def test_identical_hives_have_no_differences(workspace_tmp_path: Path) -> None:
    keys = {
        "": (100, [("Name", RegistryType.REG_SZ, "same")]),
        "Software\\Example": (200, [("Count", RegistryType.REG_DWORD, 3)]),
    }

    assert diff_hives(
        FakeHive(keys), FakeHive(keys), index_directory=workspace_tmp_path
    ) == []
    assert not list(workspace_tmp_path.glob("reg_hive_compare_*.sqlite"))


def test_added_removed_and_modified_data_are_reported(workspace_tmp_path: Path) -> None:
    left = FakeHive(
        {
            "": (100, []),
            "Removed": (200, [("Old", RegistryType.REG_SZ, "before")]),
            "Changed": (300, [("Data", RegistryType.REG_DWORD, 1)]),
        }
    )
    right = FakeHive(
        {
            "": (100, []),
            "Added": (400, [("New", RegistryType.REG_SZ, "after")]),
            "Changed": (301, [("Data", RegistryType.REG_DWORD, 2)]),
        }
    )

    diffs = diff_hives(left, right, index_directory=workspace_tmp_path)

    assert {(entry.kind, entry.change, entry.path, entry.value_name) for entry in diffs} == {
        ("key", "added", "Added", None),
        ("value", "added", "Added", "New"),
        ("key", "modified", "Changed", None),
        ("value", "modified", "Changed", "Data"),
        ("key", "removed", "Removed", None),
        ("value", "removed", "Removed", "Old"),
    }
    modified_value = next(entry for entry in diffs if entry.kind == "value" and entry.change == "modified")
    assert modified_value.old_decoded == 1
    assert modified_value.new_decoded == 2
    assert modified_value.first_changed_offset == 0
    assert modified_value.changed_byte_count == 1
    assert modified_value.changed_ranges == ((0, 1),)


def test_case_only_names_are_modifications_not_add_remove_pairs(
    workspace_tmp_path: Path,
) -> None:
    left = FakeHive(
        {"Software\\MixedCase": (100, [("ValueName", RegistryType.REG_SZ, "same")])}
    )
    right = FakeHive(
        {"software\\MIXEDCASE": (100, [("VALUENAME", RegistryType.REG_SZ, "same")])}
    )

    diffs = diff_hives(left, right, index_directory=workspace_tmp_path)

    assert [(entry.kind, entry.change) for entry in diffs] == [
        ("key", "modified"),
        ("value", "modified"),
    ]
    assert diffs[0].old_path == "Software\\MixedCase"
    assert diffs[0].new_path == "software\\MIXEDCASE"
    assert diffs[1].old_value_name == "ValueName"
    assert diffs[1].new_value_name == "VALUENAME"


def test_invalid_timestamp_raw_values_are_retained_and_compared(
    workspace_tmp_path: Path,
) -> None:
    left = FakeHive({"BadTime": (10**30, [])})
    right = FakeHive({"BadTime": (10**30 + 1, [])})

    [entry] = diff_hives(left, right, index_directory=workspace_tmp_path)
    [row] = diff_entries_to_rows([entry])

    assert entry.old_timestamp is None and entry.new_timestamp is None
    assert row["old_timestamp_raw"] == 10**30
    assert row["new_timestamp_raw"] == 10**30 + 1


def test_default_and_empty_binary_values_are_not_mistaken_for_missing(
    workspace_tmp_path: Path,
) -> None:
    left = FakeHive({"": (0, [("", RegistryType.REG_BINARY, b"")])})
    right = FakeHive({"": (0, [("", RegistryType.REG_BINARY, b"\x00")])})

    [entry] = diff_hives(left, right, index_directory=workspace_tmp_path)

    assert entry.kind == "value" and entry.change == "modified"
    assert entry.value_name == ""
    assert entry.old_data == b""
    assert entry.new_data == b"\x00"


def test_duplicate_case_insensitive_names_are_rejected_and_index_is_cleaned(
    workspace_tmp_path: Path,
) -> None:
    malformed = FakeHive({"Key": (1, []), "KEY": (2, [])})

    with pytest.raises(ValueError, match="differ only by case"):
        diff_hives(malformed, FakeHive({}), index_directory=workspace_tmp_path)

    assert not list(workspace_tmp_path.glob("reg_hive_compare_*.sqlite"))


def test_comparison_cancellation_cleans_temporary_index(workspace_tmp_path: Path) -> None:
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    with pytest.raises(CancelledError):
        diff_hives(
            FakeHive({"A": (1, []), "B": (2, [])}),
            FakeHive({}),
            cancelled=cancelled,
            index_directory=workspace_tmp_path,
        )

    assert not list(workspace_tmp_path.glob("reg_hive_compare_*.sqlite"))


def test_unique_remove_add_pair_is_annotated_as_probable_value_rename(
    workspace_tmp_path: Path,
) -> None:
    left = FakeHive({"Key": (1, [("C", RegistryType.REG_SZ, "same data")])})
    right = FakeHive(
        {"Key": (1, [("Changed from C to D", RegistryType.REG_SZ, "same data")])}
    )

    diffs = diff_hives(left, right, index_directory=workspace_tmp_path)
    value_diffs = [entry for entry in diffs if entry.kind == "value"]

    assert [(entry.change, entry.value_name) for entry in value_diffs] == [
        ("removed", "C"),
        ("added", "Changed from C to D"),
    ]
    assert all(entry.correlation == "probable_value_rename" for entry in value_diffs)
    assert {entry.correlated_value_name for entry in value_diffs} == {
        "C",
        "Changed from C to D",
    }
    assert summarize_diffs(diffs)["probable_value_renames"] == 1


def test_ambiguous_identical_values_are_not_correlated_as_renames(
    workspace_tmp_path: Path,
) -> None:
    left = FakeHive(
        {
            "Key": (
                1,
                [
                    ("Old1", RegistryType.REG_BINARY, b"same"),
                    ("Old2", RegistryType.REG_BINARY, b"same"),
                ],
            )
        }
    )
    right = FakeHive(
        {
            "Key": (
                1,
                [
                    ("New1", RegistryType.REG_BINARY, b"same"),
                    ("New2", RegistryType.REG_BINARY, b"same"),
                ],
            )
        }
    )

    diffs = diff_hives(left, right, index_directory=workspace_tmp_path)

    assert all(entry.correlation is None for entry in diffs)


def test_binary_diff_reports_disjoint_offsets_and_ranges(workspace_tmp_path: Path) -> None:
    left = FakeHive({"Key": (1, [("Blob", RegistryType.REG_BINARY, b"abcXef")])})
    right = FakeHive({"Key": (1, [("Blob", RegistryType.REG_BINARY, b"abcYefZZ")])})

    [entry] = diff_hives(left, right, index_directory=workspace_tmp_path)
    [row] = diff_entries_to_rows([entry])

    assert entry.first_changed_offset == 3
    assert entry.changed_byte_count == 3
    assert entry.changed_ranges == ((3, 4), (6, 8))
    assert row["changed_ranges"] == "0x00000003-0x00000003, 0x00000006-0x00000007"

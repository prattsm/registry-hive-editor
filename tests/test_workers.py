from __future__ import annotations

from pathlib import Path

import pytest

import reg_hive_gui.gui as gui
from reg_hive_gui.gui import SearchWorker, TimelineWorker
from reg_hive_gui.hive import HiveValue, RegistryType, encode_value


class SearchHive:
    opened: list[tuple[Path, bool]] = []

    def __init__(self, path: Path, *, write: bool) -> None:
        self.opened.append((path, write))

    def __enter__(self) -> "SearchHive":
        return self

    def __exit__(self, *_args) -> None:
        pass

    def iter_key_nodes(self):
        yield "Software\\Example", 1

    def iter_values_for_node(self, _node: int):
        data = encode_value(RegistryType.REG_SZ, "Needle data")
        yield HiveValue("ValueName", RegistryType.REG_SZ, data, "Needle data")


def test_search_worker_opens_an_independent_read_only_hive(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SearchHive.opened.clear()
    monkeypatch.setattr(gui, "Hive", SearchHive)
    results: list[tuple[int, list[gui.SearchResult]]] = []
    completions: list[tuple[int, bool, bool, int, int]] = []
    finished: list[int] = []
    hive_path = workspace_tmp_path / "snapshot"
    worker = SearchWorker(hive_path, "needle", 7)
    worker.results_batch.connect(lambda request_id, rows: results.append((request_id, rows)))
    worker.completed.connect(lambda *args: completions.append(args))
    worker.finished.connect(finished.append)

    worker.run()

    assert SearchHive.opened == [(hive_path, False)]
    assert finished == [7]
    assert results[0][0] == 7
    assert [(row.kind, row.path) for row in results[0][1]] == [
        ("value", "Software\\Example")
    ]
    assert completions == [(7, False, False, 1, 1)]


def test_cancelled_search_emits_partial_cancelled_result(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui, "Hive", SearchHive)
    emissions: list[object] = []
    completions: list[tuple[int, bool, bool, int, int]] = []
    worker = SearchWorker(workspace_tmp_path / "snapshot", "anything", 8)
    worker.results_batch.connect(lambda *args: emissions.append(args))
    worker.completed.connect(lambda *args: completions.append(args))
    worker.cancel()

    worker.run()

    assert emissions == []
    assert completions == [(8, True, False, 0, 0)]


def test_search_error_always_finishes_without_false_results(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenHive:
        def __init__(self, *_args, **_kwargs) -> None:
            raise OSError("cannot open snapshot")

    monkeypatch.setattr(gui, "Hive", BrokenHive)
    errors: list[tuple[int, str]] = []
    results: list[object] = []
    finished: list[int] = []
    worker = SearchWorker(workspace_tmp_path / "snapshot", "query", 9)
    worker.error.connect(lambda *args: errors.append(args))
    worker.results_batch.connect(lambda *args: results.append(args))
    worker.finished.connect(finished.append)

    worker.run()

    assert errors == [(9, "cannot open snapshot")]
    assert results == []
    assert finished == [9]


def test_search_limit_and_value_preview_bound_result_memory(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LargeSearchHive(SearchHive):
        def iter_key_nodes(self):
            for index in range(10):
                yield f"Needle{index}", index

        def iter_values_for_node(self, _node: int):
            return iter(())

    monkeypatch.setattr(gui, "Hive", LargeSearchHive)
    batches: list[tuple[int, list[gui.SearchResult]]] = []
    completions: list[tuple[int, bool, bool, int, int]] = []
    worker = SearchWorker(
        workspace_tmp_path / "snapshot", "needle", 11, max_results=3
    )
    worker.results_batch.connect(lambda *args: batches.append(args))
    worker.completed.connect(lambda *args: completions.append(args))

    worker.run()

    assert [row.path for row in batches[0][1]] == ["Needle0", "Needle1", "Needle2"]
    assert completions == [(11, False, True, 3, 3)]


def test_hex_search_matches_raw_bytes_and_reports_offset(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BinarySearchHive(SearchHive):
        def iter_values_for_node(self, _node: int):
            yield HiveValue("Blob", RegistryType.REG_BINARY, b"prefix\xde\xad\xbe\xef", b"")

    monkeypatch.setattr(gui, "Hive", BinarySearchHive)
    batches: list[tuple[int, list[gui.SearchResult]]] = []
    worker = SearchWorker(
        workspace_tmp_path / "snapshot", "DE AD BE EF", 12, mode="hex"
    )
    worker.results_batch.connect(lambda *args: batches.append(args))

    worker.run()

    result = batches[0][1][0]
    assert result.kind == "value"
    assert result.match_offset == 6
    assert "de ad be ef" in (result.value_data or "")


def test_cancelled_timeline_finishes_without_emitting_rows(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui, "Hive", SearchHive)
    results: list[object] = []
    finished: list[int] = []
    worker = TimelineWorker(workspace_tmp_path / "snapshot", 10)
    worker.results_ready.connect(lambda *args: results.append(args))
    worker.finished.connect(finished.append)
    worker.cancel()

    worker.run()

    assert results == []
    assert finished == [10]

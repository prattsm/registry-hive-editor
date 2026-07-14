from __future__ import annotations

from PySide6 import QtGui

from reg_hive_gui.gui import LOADED_ROLE, PATH_ROLE, HiveMainWindow


class WideHive:
    def __init__(self, width: int) -> None:
        self.width = width
        self.get_node_calls = 0

    def get_node(self, path: str) -> int:
        self.get_node_calls += 1
        assert path == ""
        return 1

    def iter_subkeys_for_node(self, node: int):
        assert node == 1
        for index in range(self.width):
            yield f"Child{index}", index + 2, index % 2 == 0

    def list_subkeys(self, _path: str):
        raise AssertionError("slow path-based child listing must not be used")

    def has_subkeys(self, _path: str):
        raise AssertionError("child paths must not be re-resolved")

    def close(self) -> None:
        pass


def test_wide_tree_population_resolves_parent_once(qtbot) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)
    hive = WideHive(2_000)
    window._hive = hive
    item = QtGui.QStandardItem("ROOT")
    item.setData("", PATH_ROLE)
    item.setData(False, LOADED_ROLE)

    window._populate_item(item)

    assert item.rowCount() == 2_000
    assert hive.get_node_calls == 1

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reg_hive_gui.compare import diff_hives
from reg_hive_gui.gui import HiveMainWindow
from reg_hive_gui.hive import Hive, RegistryType, encode_value
from reg_hive_gui.offreg import OffregHive, native_backend_available
from reg_hive_gui.plugins import Plugin, run_plugin_subprocess

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "win32" or not native_backend_available(),
        reason="Windows Offline Registry Library is unavailable",
    ),
]


def create_synthetic_hive(path: Path) -> None:
    backend = OffregHive.create()
    try:
        root = backend.root()
        software = backend.node_add_child(root, "Software")
        nested = backend.node_add_child(software, "Nested")
        leaf = backend.node_add_child(nested, "Leaf")
        backend.node_set_value(
            software,
            {
                "key": "Greeting",
                "t": RegistryType.REG_SZ,
                "value": encode_value(RegistryType.REG_SZ, "hello"),
            },
        )
        backend.node_set_value(
            software,
            {
                "key": "Count",
                "t": RegistryType.REG_DWORD,
                "value": encode_value(RegistryType.REG_DWORD, 7),
            },
        )
        backend.node_set_value(
            software,
            {"key": "Empty", "t": RegistryType.REG_BINARY, "value": b""},
        )
        backend.node_set_value(
            software,
            {
                "key": "",
                "t": RegistryType.REG_SZ,
                "value": encode_value(RegistryType.REG_SZ, "default data"),
            },
        )
        backend.node_set_value(
            software,
            {
                "key": "Qword",
                "t": RegistryType.REG_QWORD,
                "value": encode_value(RegistryType.REG_QWORD, 1 << 63),
            },
        )
        backend.node_set_value(
            software,
            {
                "key": "Multi",
                "t": RegistryType.REG_MULTI_SZ,
                "value": encode_value(RegistryType.REG_MULTI_SZ, ["one", "two"]),
            },
        )
        backend.node_set_value(
            software,
            {"key": "Opaque", "t": 0x1234, "value": b"\x00\xff"},
        )
        backend.node_set_value(
            leaf,
            {
                "key": "Deep",
                "t": RegistryType.REG_SZ,
                "value": encode_value(RegistryType.REG_SZ, "remove me"),
            },
        )
        backend.commit(path)
    finally:
        backend.close()


def test_native_open_edit_export_and_reopen_preserves_source(
    workspace_tmp_path: Path,
) -> None:
    source = workspace_tmp_path / "source.hive"
    output = workspace_tmp_path / "edited.hive"
    create_synthetic_hive(source)
    original_bytes = source.read_bytes()

    with Hive(source, write=False) as hive:
        assert hive.backend_name == "Windows Offline Registry Library"
        assert hive.get_node("software\\nested\\leaf") is not None
        assert hive.get_value("SOFTWARE", "greeting").decoded == "hello"
        assert hive.get_value("Software", "COUNT").decoded == 7
        assert hive.get_value("Software", "empty").data == b""
        assert hive.get_value("Software", "").decoded == "default data"
        assert hive.get_value("Software", "qword").decoded == 1 << 63
        assert hive.get_value("Software", "multi").decoded == ["one", "two"]
        assert hive.get_value("Software", "opaque").data == b"\x00\xff"

    with Hive(source, write=True) as hive:
        hive.create_key("software\\Added")
        hive.create_value("Software\\Added", "Enabled", RegistryType.REG_DWORD, 1)
        hive.replace_value("Software", "Greeting", "Message", RegistryType.REG_SZ, "updated")
        assert hive.delete_value("Software", "EMPTY")
        assert hive.delete_key("Software\\Nested")
        assert hive.export(output) == output

    assert source.read_bytes() == original_bytes
    assert output.read_bytes()[:4] == b"regf"

    with Hive(output, write=False) as hive:
        assert hive.get_value("Software", "Greeting") is None
        assert hive.get_value("Software", "Message").decoded == "updated"
        assert hive.get_value("Software", "").decoded == "default data"
        assert hive.get_value("Software", "Opaque").data == b"\x00\xff"
        assert hive.get_value("Software\\Added", "Enabled").decoded == 1
        assert hive.get_value("Software", "Empty") is None
        assert hive.get_node("Software\\Nested") is None

    with Hive(source, write=False) as left, Hive(output, write=False) as right:
        diffs = diff_hives(left, right, index_directory=workspace_tmp_path)
    assert any(entry.change == "added" and entry.path == "Software\\Added" for entry in diffs)
    assert any(entry.change == "removed" and entry.path == "Software\\Nested" for entry in diffs)


def test_native_backend_rejects_corrupt_input(workspace_tmp_path: Path) -> None:
    corrupt = workspace_tmp_path / "corrupt.hive"
    corrupt.write_bytes(b"regf-not-a-complete-hive")

    with pytest.raises(OSError):
        Hive(corrupt, write=False)


def test_recursive_delete_failure_restores_complete_working_copy(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = workspace_tmp_path / "source.hive"
    create_synthetic_hive(source)

    with Hive(source, write=True) as hive:
        hive.create_value("Software", "BeforeDelete", RegistryType.REG_DWORD, 9)
        backend = hive._handle
        delete_leaf = backend._delete_key_leaf
        calls = 0

        def fail_after_first_leaf(node) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated recursive delete failure")
            delete_leaf(node)

        monkeypatch.setattr(backend, "_delete_key_leaf", fail_after_first_leaf)
        with pytest.raises(OSError, match="simulated"):
            hive.delete_key("Software\\Nested")

        assert hive.get_node("Software\\Nested\\Leaf") is not None
        assert hive.get_value("Software\\Nested\\Leaf", "Deep").decoded == "remove me"
        assert hive.get_value("Software", "BeforeDelete").decoded == 9


def test_plugin_child_process_uses_native_backend(workspace_tmp_path: Path) -> None:
    hive_path = workspace_tmp_path / "source.hive"
    plugin_path = workspace_tmp_path / "backend_plugin.py"
    create_synthetic_hive(hive_path)
    plugin_path.write_text(
        "\n".join(
            [
                "PLUGIN_NAME = 'Backend Check'",
                "PLUGIN_DESCRIPTION = 'Integration test'",
                "def analyze(hive):",
                "    return [{'backend': hive.backend_name, 'keys': len(list(hive.iter_keys()))}]",
            ]
        ),
        encoding="utf-8",
    )

    rows = run_plugin_subprocess(
        Plugin("Backend Check", "", plugin_path, trusted=False), hive_path
    )

    assert rows[0]["backend"] == "Windows Offline Registry Library"
    assert rows[0]["keys"] == 4


def test_native_hive_loads_and_navigates_in_gui(workspace_tmp_path: Path, qtbot) -> None:
    hive_path = workspace_tmp_path / "source.hive"
    create_synthetic_hive(hive_path)
    window = HiveMainWindow()
    qtbot.addWidget(window)

    window.load_hive(hive_path)

    assert window._hive is not None
    assert window._hive.backend_name == "Windows Offline Registry Library"
    assert window.tree.model().item(0).child(0).text() == "Software"
    assert window._select_path("software")
    assert window.values_table.rowCount() == 7

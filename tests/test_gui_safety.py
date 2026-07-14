from __future__ import annotations

from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from reg_hive_gui.gui import (
    VALUE_NAME_ROLE,
    HiveMainWindow,
    ResultsDialog,
    ValueEditorDialog,
)
from reg_hive_gui.hive import HiveValue, RegistryType
from reg_hive_gui.plugins import Plugin


def test_edit_revisions_distinguish_modified_and_exported_state(qtbot) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)

    assert not window._has_unexported_changes()
    window._set_dirty(True)
    assert window._has_unexported_changes()

    window._exported_revision = window._edit_revision
    assert not window._has_unexported_changes()

    window._set_dirty(True)
    assert window._has_unexported_changes()
    window._set_dirty(False)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(QtWidgets.QMessageBox.Yes, True), (QtWidgets.QMessageBox.No, False)],
)
def test_unexported_change_confirmation_respects_user_choice(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    answer: QtWidgets.QMessageBox.StandardButton,
    expected: bool,
) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)
    window._set_dirty(True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args, **kwargs: answer)

    assert window._confirm_discard_changes("Testing") is expected
    window._set_dirty(False)


def test_exported_revision_does_not_prompt(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)
    window._set_dirty(True)
    window._exported_revision = window._edit_revision

    def unexpected_prompt(*_args, **_kwargs):
        raise AssertionError("exported changes must not prompt")

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", unexpected_prompt)
    assert window._confirm_discard_changes("Testing")
    window._set_dirty(False)


def test_every_session_starts_read_only_even_if_old_setting_was_false(qtbot) -> None:
    settings = QtCore.QSettings("reg_hive_gui", "RegHiveGUI")
    previous = settings.value("read_only")
    settings.setValue("read_only", False)
    try:
        window = HiveMainWindow()
        qtbot.addWidget(window)
        assert window._read_only
        assert window.read_only_action.isChecked()
    finally:
        if previous is None:
            settings.remove("read_only")
        else:
            settings.setValue("read_only", previous)


def test_invalid_value_dialog_input_does_not_accept(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = QtWidgets.QWidget()
    qtbot.addWidget(parent)
    dialog = ValueEditorDialog(parent, "Value", "Name", RegistryType.REG_BINARY, "not hex")
    qtbot.addWidget(dialog)
    warnings: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda _parent, _title, message, *args, **kwargs: warnings.append(message),
    )

    dialog._validate_and_accept()

    assert dialog.result() != QtWidgets.QDialog.Accepted
    assert warnings and "hexadecimal" in warnings[0]


class UnsupportedValueHive:
    def __init__(self) -> None:
        self.replace_called = False

    def get_value(self, _path: str, _name: str) -> HiveValue:
        return HiveValue("Opaque", RegistryType.REG_NONE, b"raw", b"raw")

    def replace_value(self, *_args) -> None:
        self.replace_called = True

    def close(self) -> None:
        pass


def test_unsupported_value_type_cannot_be_silently_converted(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)
    hive = UnsupportedValueHive()
    window._hive = hive
    window._read_only = False
    window.values_table.setRowCount(1)
    item = QtWidgets.QTableWidgetItem("Opaque")
    item.setData(VALUE_NAME_ROLE, "Opaque")
    window.values_table.setItem(0, 0, item)
    monkeypatch.setattr(window, "_current_path", lambda: "")
    messages: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "information",
        lambda _parent, _title, message, *args, **kwargs: messages.append(message),
    )

    window._edit_value(0)

    assert not hive.replace_called
    assert messages and "cannot be edited safely" in messages[0]
    window._hive = None


def test_dirty_close_can_be_cancelled(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)
    window._set_dirty(True)
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.No,
    )
    event = QtGui.QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    window._set_dirty(False)


def test_external_plugin_decline_does_not_create_snapshot(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = HiveMainWindow()
    qtbot.addWidget(window)
    window._hive_path = Path.home() / "hive"
    plugin = Plugin("External", "", Path(window._settings.fileName()), trusted=False)
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: QtWidgets.QMessageBox.No,
    )
    monkeypatch.setattr(
        window,
        "_snapshot_path",
        lambda: (_ for _ in ()).throw(AssertionError("snapshot should not be created")),
    )

    window._run_plugin(plugin)

    window._hive_path = None


def test_results_dialog_filter_uses_cached_search_text(qtbot) -> None:
    parent = QtWidgets.QWidget()
    qtbot.addWidget(parent)
    dialog = ResultsDialog(parent, "Results", [{"name": "Needle"}, {"name": "Other"}])
    qtbot.addWidget(dialog)

    dialog._apply_filter("needle")

    assert dialog.table.rowCount() == 1
    assert dialog._filtered == [{"name": "Needle"}]

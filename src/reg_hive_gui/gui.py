"""PySide6 GUI for registry hive editing."""
from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import tempfile
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .compare import DiffEntry, diff_entries_to_rows, diff_hives
from .hive import Hive, HiveValue, RegistryType
from .plugins import Plugin, load_plugins
from .reporting import export_rows, subtree_to_rows

PATH_ROLE = QtCore.Qt.UserRole + 1
LOADED_ROLE = QtCore.Qt.UserRole + 2
VALUE_NAME_ROLE = QtCore.Qt.UserRole + 3


@dataclass(frozen=True)
class SearchResult:
    kind: str
    path: str
    value_name: str | None = None
    value_data: str | None = None


class SearchWorker(QtCore.QObject):
    results_ready = QtCore.Signal(list, bool)
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal()

    def __init__(self, hive: Hive, query: str) -> None:
        super().__init__()
        self._hive = hive
        self._query = query.lower()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _should_cancel(self) -> bool:
        thread = QtCore.QThread.currentThread()
        return self._cancelled or (thread is not None and thread.isInterruptionRequested())

    @QtCore.Slot()
    def run(self) -> None:
        results: list[SearchResult] = []
        total = 0
        matched = 0
        cancelled = False
        for path, node in self._hive.iter_key_nodes():
            if self._should_cancel():
                cancelled = True
                break
            total += 1
            if self._query and self._query in path.lower():
                results.append(SearchResult(kind="key", path=path))
                matched += 1
            for value in self._hive.iter_values_for_node(node):
                if self._should_cancel():
                    cancelled = True
                    break
                value_name = value.name or "(Default)"
                data_text = format_value_data(value)
                haystack = f"{value_name}\n{data_text}".lower()
                if self._query in haystack:
                    results.append(
                        SearchResult(
                            kind="value",
                            path=path,
                            value_name=value.name,
                            value_data=data_text,
                        )
                    )
                    matched += 1
            if cancelled:
                break
            if total % 200 == 0:
                self.progress.emit(total, matched)
        self.results_ready.emit(results, cancelled)
        self.finished.emit()


class ValueEditorDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent: QtWidgets.QWidget,
        title: str,
        name: str,
        value_type: RegistryType,
        data_text: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        self.name_edit = QtWidgets.QLineEdit(name)
        self.type_combo = QtWidgets.QComboBox()
        self.data_edit = QtWidgets.QPlainTextEdit(data_text)
        self.data_edit.setPlaceholderText("Enter value data")

        for reg_type in (
            RegistryType.REG_SZ,
            RegistryType.REG_EXPAND_SZ,
            RegistryType.REG_MULTI_SZ,
            RegistryType.REG_DWORD,
            RegistryType.REG_QWORD,
            RegistryType.REG_BINARY,
        ):
            self.type_combo.addItem(reg_type.name, reg_type)

        index = self.type_combo.findData(value_type)
        if index != -1:
            self.type_combo.setCurrentIndex(index)

        form = QtWidgets.QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.type_combo)
        form.addRow("Data", self.data_edit)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def get_value(self) -> tuple[str, RegistryType, object]:
        name = self.name_edit.text()
        value_type = self.type_combo.currentData()
        data_text = self.data_edit.toPlainText()
        parsed = parse_value_input(value_type, data_text)
        return name, value_type, parsed


class ResultsDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, title: str, rows: list[dict[str, object]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(900, 500)

        self._rows = rows
        self._filtered: list[dict[str, object]] = rows

        self.filter_input = QtWidgets.QLineEdit()
        self.filter_input.setPlaceholderText("Filter results...")
        self.filter_input.textChanged.connect(self._apply_filter)

        self.table = QtWidgets.QTableWidget()
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        self.export_button = QtWidgets.QPushButton("Export...")
        self.export_button.clicked.connect(self._export_rows)
        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(self.close)

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.export_button)
        button_layout.addStretch(1)
        button_layout.addWidget(close_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.filter_input)
        layout.addWidget(self.table, 1)
        layout.addLayout(button_layout)

        self._load_rows(rows)

    def _load_rows(self, rows: list[dict[str, object]]) -> None:
        if not rows:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            return
        columns = sorted({key for row in rows for key in row.keys()})
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col_index, key in enumerate(columns):
                value = row.get(key, "")
                item = QtWidgets.QTableWidgetItem(str(value))
                self.table.setItem(row_index, col_index, item)
        self.table.resizeColumnsToContents()

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        if not query:
            self._filtered = self._rows
        else:
            filtered = []
            for row in self._rows:
                haystack = " ".join(str(value) for value in row.values()).lower()
                if query in haystack:
                    filtered.append(row)
            self._filtered = filtered
        self._load_rows(self._filtered)

    def _export_rows(self) -> None:
        if not self._filtered:
            QtWidgets.QMessageBox.information(self, "Export", "No rows to export.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Report",
            str(Path.home() / "report.json"),
            "JSON (*.json);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            export_rows(self._filtered, path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(exc))


class PluginWorker(QtCore.QObject):
    results_ready = QtCore.Signal(str, list)
    error = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, plugin: Plugin, hive_path: Path) -> None:
        super().__init__()
        self._plugin = plugin
        self._hive_path = hive_path

    @QtCore.Slot()
    def run(self) -> None:
        try:
            with Hive(self._hive_path, write=False) as hive:
                results = self._plugin.analyze(hive)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        else:
            self.results_ready.emit(self._plugin.name, results)
        finally:
            self.finished.emit()


class CompareWorker(QtCore.QObject):
    results_ready = QtCore.Signal(list)
    error = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, left_path: Path, right_path: Path) -> None:
        super().__init__()
        self._left_path = left_path
        self._right_path = right_path

    @QtCore.Slot()
    def run(self) -> None:
        try:
            with Hive(self._left_path, write=False) as left, Hive(self._right_path, write=False) as right:
                results = diff_hives(left, right)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        else:
            self.results_ready.emit(results)
        finally:
            self.finished.emit()


class TimelineWorker(QtCore.QObject):
    results_ready = QtCore.Signal(list)
    error = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, hive_path: Path) -> None:
        super().__init__()
        self._hive_path = hive_path

    @QtCore.Slot()
    def run(self) -> None:
        try:
            rows: list[dict[str, object]] = []
            with Hive(self._hive_path, write=False) as hive:
                for path, node in hive.iter_key_nodes():
                    timestamp = hive.get_node_timestamp(node)
                    rows.append(
                        {
                            "path": path,
                            "timestamp": timestamp.isoformat() if timestamp else "",
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        else:
            self.results_ready.emit(rows)
        finally:
            self.finished.emit()


class HiveMainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Registry Hive GUI")
        self.resize(1200, 720)

        settings = QtCore.QSettings("reg_hive_gui", "RegHiveGUI")
        self._settings = settings
        self._read_only = settings.value("read_only", False, type=bool)
        self._hive: Hive | None = None
        self._hive_path: Path | None = None
        self._dirty = False
        self._search_thread: QtCore.QThread | None = None
        self._search_worker: SearchWorker | None = None
        self._path_to_item: dict[str, QtGui.QStandardItem] = {}
        self._search_active = False
        self._search_last_count = 0
        self._search_cancelled = False
        self._results_visible = True
        self._right_splitter_sizes: list[int] | None = None
        self._plugins: list[Plugin] = []
        self._plugin_thread: QtCore.QThread | None = None
        self._plugin_worker: PluginWorker | None = None
        self._plugin_temp_path: Path | None = None
        self._compare_thread: QtCore.QThread | None = None
        self._compare_worker: CompareWorker | None = None
        self._compare_temp_path: Path | None = None
        self._timeline_thread: QtCore.QThread | None = None
        self._timeline_worker: TimelineWorker | None = None
        self._timeline_temp_path: Path | None = None
        self._last_diff_entries: list[DiffEntry] = []
        self._bookmarks: list[str] = []
        self._bookmark_syncing = False
        self._temp_paths: list[Path] = []
        self._tree_selection_model: QtCore.QItemSelectionModel | None = None

        self._create_actions()
        self._build_ui()
        self._set_ui_enabled(False)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self._stop_search()
        if self._hive is not None:
            self._hive.close()
        self._settings.setValue("results_visible", self._results_visible)
        if self._right_splitter_sizes is None:
            self._right_splitter_sizes = self.right_splitter.sizes()
        if self._right_splitter_sizes is not None:
            self._settings.setValue("results_sizes", self._right_splitter_sizes)
        self._settings.setValue("read_only", self._read_only)
        self._save_bookmarks()
        for path in self._temp_paths:
            try:
                path.unlink()
            except Exception:
                pass
        super().closeEvent(event)

    def _create_actions(self) -> None:
        self.open_action = QtGui.QAction("Open Hive...", self)
        self.open_action.triggered.connect(self.open_hive_dialog)

        self.export_action = QtGui.QAction("Export Hive As...", self)
        self.export_action.triggered.connect(self.export_hive_dialog)

        self.export_subtree_action = QtGui.QAction("Export Selected Subtree...", self)
        self.export_subtree_action.triggered.connect(self.export_subtree_report)

        self.export_search_action = QtGui.QAction("Export Search Results...", self)
        self.export_search_action.triggered.connect(self.export_search_report)

        self.compare_action = QtGui.QAction("Compare to Hive...", self)
        self.compare_action.triggered.connect(self.compare_hive_dialog)

        self.export_diff_action = QtGui.QAction("Export Diff Report...", self)
        self.export_diff_action.triggered.connect(self.export_diff_report)

        self.read_only_action = QtGui.QAction("Read-only Mode", self)
        self.read_only_action.setCheckable(True)
        self.read_only_action.triggered.connect(self._toggle_read_only)

        self.jump_action = QtGui.QAction("Jump to Path...", self)
        self.jump_action.setShortcut(QtGui.QKeySequence("Ctrl+L"))
        self.jump_action.triggered.connect(self.jump_to_path)

        self.timeline_action = QtGui.QAction("Timeline View...", self)
        self.timeline_action.triggered.connect(self.show_timeline)

        self.add_bookmark_action = QtGui.QAction("Add Bookmark", self)
        self.add_bookmark_action.triggered.connect(self.add_bookmark_current)

        self.remove_bookmark_action = QtGui.QAction("Remove Bookmark", self)
        self.remove_bookmark_action.triggered.connect(self.remove_selected_bookmark)

        self.exit_action = QtGui.QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)

    def _build_ui(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.read_only_action)
        file_menu.addAction(self.export_action)
        file_menu.addAction(self.export_subtree_action)
        file_menu.addAction(self.export_search_action)
        file_menu.addSeparator()
        file_menu.addAction(self.compare_action)
        file_menu.addAction(self.export_diff_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        navigate_menu = menu.addMenu("Navigate")
        navigate_menu.addAction(self.jump_action)
        navigate_menu.addSeparator()
        navigate_menu.addAction(self.add_bookmark_action)
        navigate_menu.addAction(self.remove_bookmark_action)

        view_menu = menu.addMenu("View")
        view_menu.addAction(self.timeline_action)

        self.plugins_menu = menu.addMenu("Plugins")
        self.read_only_action.setChecked(self._read_only)

        self._apply_action_icons()
        main_toolbar = QtWidgets.QToolBar("Main")
        main_toolbar.setMovable(False)
        main_toolbar.setIconSize(QtCore.QSize(16, 16))
        main_toolbar.addAction(self.open_action)
        main_toolbar.addAction(self.export_action)
        main_toolbar.addSeparator()
        main_toolbar.addAction(self.compare_action)
        main_toolbar.addAction(self.export_diff_action)
        main_toolbar.addSeparator()
        main_toolbar.addAction(self.read_only_action)
        main_toolbar.addSeparator()
        main_toolbar.addAction(self.jump_action)
        main_toolbar.addAction(self.timeline_action)
        self.addToolBar(QtCore.Qt.TopToolBarArea, main_toolbar)

        self.tree = QtWidgets.QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.expanded.connect(self._on_tree_expanded)
        # selection model is wired after a model is attached

        self.bookmarks_list = QtWidgets.QListWidget()
        self.bookmarks_list.itemActivated.connect(self._on_bookmark_activated)
        self.bookmark_add_button = QtWidgets.QToolButton()
        self.bookmark_add_button.setText("+")
        self.bookmark_add_button.setToolTip("Add bookmark for selected key")
        self.bookmark_add_button.clicked.connect(self.add_bookmark_current)
        self.bookmark_remove_button = QtWidgets.QToolButton()
        self.bookmark_remove_button.setText("-")
        self.bookmark_remove_button.setToolTip("Remove selected bookmark")
        self.bookmark_remove_button.clicked.connect(self.remove_selected_bookmark)

        bookmarks_header = QtWidgets.QHBoxLayout()
        bookmarks_header.addWidget(QtWidgets.QLabel("Bookmarks"))
        bookmarks_header.addStretch(1)
        bookmarks_header.addWidget(self.bookmark_add_button)
        bookmarks_header.addWidget(self.bookmark_remove_button)

        bookmarks_panel = QtWidgets.QFrame()
        bookmarks_panel.setObjectName("BookmarksPanel")
        bookmarks_layout = QtWidgets.QVBoxLayout(bookmarks_panel)
        bookmarks_layout.setContentsMargins(8, 8, 8, 8)
        bookmarks_layout.addLayout(bookmarks_header)
        bookmarks_layout.addWidget(self.bookmarks_list, 1)

        left_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        left_splitter.addWidget(self.tree)
        left_splitter.addWidget(bookmarks_panel)
        left_splitter.setStretchFactor(0, 3)
        left_splitter.setStretchFactor(1, 1)
        left_splitter.setChildrenCollapsible(False)

        self.values_table = QtWidgets.QTableWidget(0, 3)
        self.values_table.setHorizontalHeaderLabels(["Name", "Type", "Data"])
        self.values_table.horizontalHeader().setStretchLastSection(True)
        self.values_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.values_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.values_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.values_table.customContextMenuRequested.connect(self._value_context_menu)
        self.values_table.itemSelectionChanged.connect(self._on_value_selection_changed)

        self.value_details = QtWidgets.QGroupBox("Value Details")
        details_layout = QtWidgets.QVBoxLayout(self.value_details)
        self.value_meta = QtWidgets.QLabel("No value selected.")
        self.value_meta.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.value_decoded = QtWidgets.QPlainTextEdit()
        self.value_decoded.setReadOnly(True)
        self.value_decoded.setPlaceholderText("Decoded view")
        self.value_raw = QtWidgets.QPlainTextEdit()
        self.value_raw.setReadOnly(True)
        self.value_raw.setPlaceholderText("Hex / raw view")
        details_layout.addWidget(self.value_meta)
        details_layout.addWidget(QtWidgets.QLabel("Decoded"))
        details_layout.addWidget(self.value_decoded)
        details_layout.addWidget(QtWidgets.QLabel("Hex / Raw"))
        details_layout.addWidget(self.value_raw)

        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Search keys and values...")
        self.search_input.returnPressed.connect(self._start_search)
        self.search_button = QtWidgets.QPushButton("Search")
        self.search_button.clicked.connect(self._start_search)
        self.search_cancel_button = QtWidgets.QPushButton("Cancel")
        self.search_cancel_button.clicked.connect(self._cancel_search)
        self.search_cancel_button.setEnabled(False)
        self.search_status = QtWidgets.QLabel("Idle")
        self.search_status.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.search_status.setMinimumWidth(200)
        self.search_status.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred
        )
        self.search_status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        search_frame = QtWidgets.QFrame()
        search_frame.setObjectName("SearchFrame")
        search_layout = QtWidgets.QHBoxLayout(search_frame)
        search_layout.setContentsMargins(8, 6, 8, 6)
        search_layout.setSpacing(8)
        search_layout.addWidget(QtWidgets.QLabel("Search"))
        search_layout.addWidget(self.search_input, 1)
        search_layout.addWidget(self.search_button)
        search_layout.addWidget(self.search_cancel_button)
        search_layout.addWidget(self.search_status)

        self.search_results = QtWidgets.QListWidget()
        self.search_results.itemActivated.connect(self._on_search_item_activated)

        self.results_container = QtWidgets.QWidget()
        results_layout = QtWidgets.QVBoxLayout(self.results_container)
        results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_toggle = QtWidgets.QToolButton()
        self.results_toggle.setCheckable(True)
        self.results_toggle.setChecked(True)
        self.results_toggle.setText("Search Results")
        self.results_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.results_toggle.setArrowType(QtCore.Qt.DownArrow)
        self.results_toggle.setAutoRaise(True)
        self.results_toggle.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
        )
        self.results_toggle.toggled.connect(self._toggle_results)
        results_layout.addWidget(self.results_toggle)
        results_layout.addWidget(self.search_results, 1)

        self.key_info_frame = QtWidgets.QFrame()
        self.key_info_frame.setObjectName("KeyInfoFrame")
        key_layout = QtWidgets.QHBoxLayout(self.key_info_frame)
        key_layout.setContentsMargins(8, 6, 8, 6)
        key_layout.setSpacing(8)
        self.key_path_label = QtWidgets.QLabel("Path: ")
        self.key_path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.key_path_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self.key_time_label = QtWidgets.QLabel("Last write: -")
        self.key_time_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.copy_path_button = QtWidgets.QToolButton()
        self.copy_path_button.setText("Copy Path")
        self.copy_path_button.clicked.connect(self._copy_current_path)
        self.bookmark_toggle = QtWidgets.QToolButton()
        self.bookmark_toggle.setCheckable(True)
        self.bookmark_toggle.setText("Bookmark")
        self.bookmark_toggle.toggled.connect(self._toggle_bookmark_current)
        key_layout.addWidget(self.key_path_label, 1)
        key_layout.addWidget(self.key_time_label)
        key_layout.addWidget(self.copy_path_button)
        key_layout.addWidget(self.bookmark_toggle)

        self.right_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.right_splitter.addWidget(self.values_table)
        self.right_splitter.addWidget(self.value_details)
        self.right_splitter.addWidget(self.results_container)
        self.right_splitter.setStretchFactor(0, 2)
        self.right_splitter.setStretchFactor(1, 1)
        self.right_splitter.setStretchFactor(2, 1)
        self.right_splitter.setChildrenCollapsible(False)

        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(self.key_info_frame)
        right_layout.addWidget(search_frame)
        right_layout.addWidget(self.right_splitter, 1)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(left_splitter)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(1, 1)

        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QHBoxLayout(central)
        central_layout.setContentsMargins(8, 8, 8, 8)
        central_layout.addWidget(splitter)
        self.setCentralWidget(central)
        self._restore_results_state()
        self._apply_styles()
        self.statusBar().showMessage("Open a hive to begin.")

        self._load_bookmarks()
        self._load_plugins()

    def _set_ui_enabled(self, enabled: bool) -> None:
        self.export_action.setEnabled(enabled)
        self.export_subtree_action.setEnabled(enabled)
        self.export_search_action.setEnabled(enabled and self.search_results.count() > 0)
        self.compare_action.setEnabled(enabled)
        self.export_diff_action.setEnabled(enabled and bool(self._last_diff_entries))
        self.tree.setEnabled(enabled)
        self.values_table.setEnabled(enabled)
        self.search_input.setEnabled(enabled)
        self.search_button.setEnabled(enabled)
        self.search_cancel_button.setEnabled(enabled and self._search_active)
        self.search_results.setEnabled(enabled)
        self.jump_action.setEnabled(enabled)
        self.timeline_action.setEnabled(enabled)
        self.bookmark_add_button.setEnabled(enabled)
        self.bookmark_remove_button.setEnabled(enabled)
        self.copy_path_button.setEnabled(enabled)
        self.bookmark_toggle.setEnabled(enabled)
        self.add_bookmark_action.setEnabled(enabled)
        self.remove_bookmark_action.setEnabled(enabled)

    def _set_dirty(self, dirty: bool = True) -> None:
        self._dirty = dirty
        self._update_title()

    def _update_title(self) -> None:
        if self._hive_path is None:
            self.setWindowTitle("Registry Hive GUI")
            return
        marker = "*" if self._dirty else ""
        mode = " (Read-only)" if self._read_only else ""
        self.setWindowTitle(f"Registry Hive GUI - {self._hive_path}{marker}{mode}")

    def _apply_action_icons(self) -> None:
        style = self.style()
        self.open_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_DirOpenIcon))
        self.export_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_DialogSaveButton))
        self.compare_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView))
        self.export_diff_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView))
        self.read_only_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_MessageBoxWarning))
        self.jump_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_ArrowRight))
        self.timeline_action.setIcon(style.standardIcon(QtWidgets.QStyle.SP_FileDialogInfoView))

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f5f7fa; }
            QToolBar { background: #eef1f5; border-bottom: 1px solid #d5dbe3; spacing: 6px; }
            QTreeView, QTableWidget, QListWidget, QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #d6dbe3;
                border-radius: 6px;
            }
            QHeaderView::section {
                background: #f3f4f6;
                border: none;
                border-bottom: 1px solid #d6dbe3;
                padding: 4px 6px;
            }
            QGroupBox {
                font-weight: 600;
                border: 1px solid #d6dbe3;
                border-radius: 8px;
                margin-top: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            QFrame#KeyInfoFrame, QFrame#SearchFrame, QFrame#BookmarksPanel {
                background: #ffffff;
                border: 1px solid #d6dbe3;
                border-radius: 8px;
            }
            QLabel { color: #223046; }
            QToolButton, QPushButton { padding: 4px 10px; }
            QLineEdit { padding: 4px 8px; }
            """
        )

    def _load_plugins(self) -> None:
        root = Path(__file__).resolve().parents[2]
        search_paths = [
            root / "plugins",
            Path.home() / ".config" / "reg_hive_gui" / "plugins",
        ]
        self._plugins = load_plugins(search_paths)
        self.plugins_menu.clear()
        if not self._plugins:
            action = QtGui.QAction("No plugins found", self)
            action.setEnabled(False)
            self.plugins_menu.addAction(action)
            return
        for plugin in self._plugins:
            action = QtGui.QAction(plugin.name, self)
            action.setToolTip(plugin.description)
            action.triggered.connect(lambda _checked=False, p=plugin: self._run_plugin(p))
            self.plugins_menu.addAction(action)

    def _snapshot_path(self) -> Path | None:
        if self._hive is None or self._hive_path is None:
            return None
        if not self._dirty:
            return self._hive_path
        temp = tempfile.NamedTemporaryFile(delete=False, prefix="reg_hive_", suffix=".hive")
        temp.close()
        temp_path = Path(temp.name)
        try:
            self._hive.export(temp_path)
        except Exception:
            try:
                temp_path.unlink()
            except Exception:
                pass
            raise
        self._temp_paths.append(temp_path)
        return temp_path

    def open_hive_dialog(self) -> None:
        if self._dirty:
            confirm = QtWidgets.QMessageBox.question(
                self,
                "Discard Changes?",
                "Opening a new hive will discard unexported changes. Continue?",
            )
            if confirm != QtWidgets.QMessageBox.Yes:
                return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open Registry Hive",
            str(Path.home()),
            "Hive Files (*)",
        )
        if not path:
            return
        self.load_hive(Path(path))

    def _toggle_read_only(self) -> None:
        new_state = self.read_only_action.isChecked()
        if self._read_only == new_state:
            return
        if self._dirty:
            confirm = QtWidgets.QMessageBox.question(
                self,
                "Discard Changes?",
                "Switching read-only mode will discard unexported changes. Continue?",
            )
            if confirm != QtWidgets.QMessageBox.Yes:
                self.read_only_action.setChecked(self._read_only)
                return
        self._read_only = new_state
        if self._hive_path is not None:
            self.load_hive(self._hive_path)

    def load_hive(self, path: Path) -> None:
        try:
            new_hive = Hive(path, write=not self._read_only)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Open Hive Failed", str(exc))
            self.statusBar().showMessage("Failed to open hive.")
            return
        if self._hive is not None:
            self._hive.close()
        self._hive = new_hive
        self._hive_path = path
        self._path_to_item.clear()
        self._dirty = False

        model = QtGui.QStandardItemModel()
        root_item = QtGui.QStandardItem("ROOT")
        root_item.setEditable(False)
        root_item.setData("", PATH_ROLE)
        root_item.setData(False, LOADED_ROLE)
        model.appendRow(root_item)
        self.tree.setModel(model)
        if self._tree_selection_model is not None:
            try:
                self._tree_selection_model.selectionChanged.disconnect(self._on_tree_selection_changed)
            except (TypeError, RuntimeError):
                pass
        self._tree_selection_model = self.tree.selectionModel()
        if self._tree_selection_model is not None:
            self._tree_selection_model.selectionChanged.connect(self._on_tree_selection_changed)
        self.tree.expand(model.index(0, 0))
        self._populate_item(root_item)
        self.tree.expand(model.index(0, 0))
        self._set_ui_enabled(True)
        self._update_title()
        self._update_key_info("")
        self.statusBar().showMessage(f"Loaded hive: {path}")

    def export_hive_dialog(self) -> None:
        if self._hive is None or self._hive_path is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Registry Hive",
            str(self._hive_path.with_name(self._hive_path.name + "_modified")),
            "Hive Files (*)",
        )
        if not path:
            return
        try:
            if self._read_only:
                output_path = Path(path)
                if output_path.resolve() == self._hive_path.resolve():
                    raise ValueError("Refusing to overwrite the input hive")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._hive_path, output_path)
                output = output_path
            else:
                output = self._hive.export(path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported hive to {output}")

    def export_subtree_report(self) -> None:
        if self._hive is None:
            return
        path = self._current_path()
        rows = subtree_to_rows(self._hive, path or None)
        if not rows:
            QtWidgets.QMessageBox.information(self, "Export Subtree", "No rows to export.")
            return
        self._export_rows_dialog(rows, "subtree_report")

    def export_search_report(self) -> None:
        rows = self._search_results_to_rows()
        if not rows:
            QtWidgets.QMessageBox.information(self, "Export Search Results", "No search results to export.")
            return
        self._export_rows_dialog(rows, "search_report")

    def compare_hive_dialog(self) -> None:
        if self._hive is None or self._hive_path is None:
            return
        other_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Compare With Hive",
            str(Path.home()),
            "Hive Files (*)",
        )
        if not other_path:
            return
        try:
            left_path = self._snapshot_path()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Compare Failed", str(exc))
            return
        if left_path is None:
            return
        self._compare_temp_path = (
            left_path if self._hive_path is None or left_path != self._hive_path else None
        )
        self._start_compare(left_path, Path(other_path))

    def export_diff_report(self) -> None:
        if not self._last_diff_entries:
            QtWidgets.QMessageBox.information(self, "Export Diff", "No diff results available.")
            return
        rows = diff_entries_to_rows(self._last_diff_entries)
        self._export_rows_dialog(rows, "diff_report")

    def jump_to_path(self) -> None:
        if self._hive is None:
            return
        path, ok = QtWidgets.QInputDialog.getText(self, "Jump to Path", "Registry path")
        if not ok or not path:
            return
        if not self._select_path(path):
            QtWidgets.QMessageBox.information(self, "Jump to Path", "Path not found.")

    def show_timeline(self) -> None:
        if self._hive is None:
            return
        if self._timeline_thread is not None:
            QtWidgets.QMessageBox.information(self, "Timeline", "Timeline is already running.")
            return
        try:
            snapshot_path = self._snapshot_path()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Timeline Failed", str(exc))
            return
        if snapshot_path is None:
            return
        self._timeline_temp_path = (
            snapshot_path if self._hive_path is None or snapshot_path != self._hive_path else None
        )
        self.statusBar().showMessage("Building timeline...")
        self._timeline_thread = QtCore.QThread(self)
        self._timeline_worker = TimelineWorker(snapshot_path)
        self._timeline_worker.moveToThread(self._timeline_thread)
        self._timeline_thread.started.connect(self._timeline_worker.run)
        self._timeline_worker.results_ready.connect(self._on_timeline_results)
        self._timeline_worker.error.connect(self._on_timeline_error)
        self._timeline_worker.finished.connect(self._timeline_thread.quit)
        self._timeline_worker.finished.connect(self._timeline_worker.deleteLater)
        self._timeline_thread.finished.connect(self._timeline_thread.deleteLater)
        self._timeline_thread.finished.connect(self._on_timeline_finished)
        self._timeline_thread.start()

    def _export_rows_dialog(self, rows: list[dict[str, object]], basename: str) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Report",
            str(Path.home() / f"{basename}.json"),
            "JSON (*.json);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            export_rows(rows, path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(exc))

    def _search_results_to_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index in range(self.search_results.count()):
            item = self.search_results.item(index)
            result = item.data(QtCore.Qt.UserRole)
            if not isinstance(result, SearchResult):
                continue
            rows.append(
                {
                    "kind": result.kind,
                    "path": result.path,
                    "value_name": result.value_name or "",
                    "value_data": result.value_data or "",
                }
            )
        return rows

    def _start_compare(self, left_path: Path, right_path: Path) -> None:
        if self._compare_thread is not None:
            QtWidgets.QMessageBox.information(self, "Compare", "A comparison is already running.")
            return
        self.statusBar().showMessage("Comparing hives...")
        self._compare_thread = QtCore.QThread(self)
        self._compare_worker = CompareWorker(left_path, right_path)
        self._compare_worker.moveToThread(self._compare_thread)
        self._compare_thread.started.connect(self._compare_worker.run)
        self._compare_worker.results_ready.connect(self._on_compare_results)
        self._compare_worker.error.connect(self._on_compare_error)
        self._compare_worker.finished.connect(self._compare_thread.quit)
        self._compare_worker.finished.connect(self._compare_worker.deleteLater)
        self._compare_thread.finished.connect(self._compare_thread.deleteLater)
        self._compare_thread.finished.connect(self._on_compare_finished)
        self._compare_thread.start()

    def _on_compare_results(self, results: list[DiffEntry]) -> None:
        self._last_diff_entries = results
        self.export_diff_action.setEnabled(bool(results))
        rows = diff_entries_to_rows(results)
        dialog = ResultsDialog(self, "Hive Diff", rows)
        dialog.show()
        self.statusBar().showMessage(f"Compare complete: {len(results)} differences")

    def _on_compare_error(self, message: str) -> None:
        QtWidgets.QMessageBox.critical(self, "Compare Failed", message)
        self.statusBar().showMessage("Compare failed")

    def _on_compare_finished(self) -> None:
        self._compare_worker = None
        self._compare_thread = None
        if self._compare_temp_path is not None:
            try:
                self._compare_temp_path.unlink()
            except Exception:
                pass
            if self._compare_temp_path in self._temp_paths:
                self._temp_paths.remove(self._compare_temp_path)
            self._compare_temp_path = None

    def _on_timeline_results(self, rows: list[dict[str, object]]) -> None:
        dialog = ResultsDialog(self, "Timeline", rows)
        dialog.show()
        self.statusBar().showMessage(f"Timeline ready: {len(rows)} keys")

    def _on_timeline_error(self, message: str) -> None:
        QtWidgets.QMessageBox.critical(self, "Timeline Failed", message)
        self.statusBar().showMessage("Timeline failed")

    def _on_timeline_finished(self) -> None:
        self._timeline_worker = None
        self._timeline_thread = None
        if self._timeline_temp_path is not None:
            try:
                self._timeline_temp_path.unlink()
            except Exception:
                pass
            if self._timeline_temp_path in self._temp_paths:
                self._temp_paths.remove(self._timeline_temp_path)
            self._timeline_temp_path = None

    def _run_plugin(self, plugin: Plugin) -> None:
        if self._hive_path is None:
            return
        if self._plugin_thread is not None:
            QtWidgets.QMessageBox.information(self, "Plugin", "A plugin is already running.")
            return
        try:
            snapshot_path = self._snapshot_path()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Plugin Failed", str(exc))
            return
        if snapshot_path is None:
            return
        self._plugin_temp_path = (
            snapshot_path if self._hive_path is None or snapshot_path != self._hive_path else None
        )
        self.statusBar().showMessage(f"Running plugin: {plugin.name}")
        self._plugin_thread = QtCore.QThread(self)
        self._plugin_worker = PluginWorker(plugin, snapshot_path)
        self._plugin_worker.moveToThread(self._plugin_thread)
        self._plugin_thread.started.connect(self._plugin_worker.run)
        self._plugin_worker.results_ready.connect(self._on_plugin_results)
        self._plugin_worker.error.connect(self._on_plugin_error)
        self._plugin_worker.finished.connect(self._plugin_thread.quit)
        self._plugin_worker.finished.connect(self._plugin_worker.deleteLater)
        self._plugin_thread.finished.connect(self._plugin_thread.deleteLater)
        self._plugin_thread.finished.connect(self._on_plugin_finished)
        self._plugin_thread.start()

    def _on_plugin_results(self, name: str, results: list[dict[str, object]]) -> None:
        dialog = ResultsDialog(self, f"Plugin: {name}", results)
        dialog.show()
        self.statusBar().showMessage(f"Plugin complete: {name} ({len(results)} rows)")

    def _on_plugin_error(self, message: str) -> None:
        QtWidgets.QMessageBox.critical(self, "Plugin Failed", message)
        self.statusBar().showMessage("Plugin failed")

    def _on_plugin_finished(self) -> None:
        self._plugin_worker = None
        self._plugin_thread = None
        if self._plugin_temp_path is not None:
            try:
                self._plugin_temp_path.unlink()
            except Exception:
                pass
            if self._plugin_temp_path in self._temp_paths:
                self._temp_paths.remove(self._plugin_temp_path)
            self._plugin_temp_path = None

    def _populate_item(self, item: QtGui.QStandardItem) -> None:
        if self._hive is None:
            return
        if item.data(LOADED_ROLE):
            return
        path = item.data(PATH_ROLE)
        if path is None:
            path = ""
        if not path:
            self._path_to_item.clear()
        else:
            prefix = f"{path}\\"
            for existing in list(self._path_to_item):
                if existing == path or existing.startswith(prefix):
                    self._path_to_item.pop(existing, None)
        item.removeRows(0, item.rowCount())
        subkeys = self._hive.list_subkeys(path)
        for name in sorted(subkeys, key=str.casefold):
            child_item = QtGui.QStandardItem(name)
            child_item.setEditable(False)
            child_path = f"{path}\\{name}" if path else name
            child_item.setData(child_path, PATH_ROLE)
            child_item.setData(False, LOADED_ROLE)
            self._path_to_item[child_path] = child_item
            if self._hive.list_subkeys(child_path):
                child_item.appendRow(QtGui.QStandardItem(""))
            item.appendRow(child_item)
        item.setData(True, LOADED_ROLE)

    def _on_tree_expanded(self, index: QtCore.QModelIndex) -> None:
        item = self._get_item(index)
        if item is not None:
            self._populate_item(item)

    def _on_tree_selection_changed(self, selected: QtCore.QItemSelection, _deselected: QtCore.QItemSelection) -> None:
        if not selected.indexes():
            return
        item = self._get_item(selected.indexes()[0])
        if item is None:
            return
        path = item.data(PATH_ROLE)
        self._refresh_values(path)
        self._update_key_info(path)

    def _refresh_values(self, path: str) -> None:
        if self._hive is None:
            return
        values = list(self._hive.list_values(path))
        self.values_table.setRowCount(len(values))
        for row, value in enumerate(values):
            name_display = value.name if value.name else "(Default)"
            name_item = QtWidgets.QTableWidgetItem(name_display)
            name_item.setData(VALUE_NAME_ROLE, value.name)
            type_item = QtWidgets.QTableWidgetItem(value.type_name)
            data_item = QtWidgets.QTableWidgetItem(format_value_data(value))
            self.values_table.setItem(row, 0, name_item)
            self.values_table.setItem(row, 1, type_item)
            self.values_table.setItem(row, 2, data_item)
        self.values_table.resizeColumnsToContents()
        self.statusBar().showMessage(f"{path or 'ROOT'}: {len(values)} values")
        self._update_value_details(None)

    def _on_value_selection_changed(self) -> None:
        if self._hive is None:
            self._update_value_details(None)
            return
        selected = self.values_table.selectedItems()
        if not selected:
            self._update_value_details(None)
            return
        row = selected[0].row()
        name_item = self.values_table.item(row, 0)
        if name_item is None:
            self._update_value_details(None)
            return
        value_name = name_item.data(VALUE_NAME_ROLE) or ""
        path = self._current_path()
        value = self._hive.get_value(path, value_name)
        self._update_value_details(value)

    def _update_value_details(self, value: HiveValue | None) -> None:
        if value is None:
            self.value_meta.setText("No value selected.")
            self.value_decoded.setPlainText("")
            self.value_raw.setPlainText("")
            return
        name_display = value.name if value.name else "(Default)"
        meta = f"Name: {name_display} | Type: {value.type_name} | Size: {len(value.data)} bytes"
        self.value_meta.setText(meta)
        if isinstance(value.decoded, list):
            decoded_text = "\n".join(str(item) for item in value.decoded)
        elif isinstance(value.decoded, bytes):
            decoded_text = value.decoded.hex(" ")
        else:
            decoded_text = str(value.decoded)
        self.value_decoded.setPlainText(decoded_text)
        self.value_raw.setPlainText(value.data.hex(" "))

    def _update_key_info(self, path: str | None) -> None:
        path = path or ""
        display_path = path if path else "ROOT"
        timestamp = self._hive.get_key_timestamp(path) if self._hive is not None else None
        time_text = timestamp.isoformat() if timestamp else "-"
        self.key_path_label.setText(f"Path: {display_path}")
        self.key_time_label.setText(f"Last write: {time_text}")
        self._sync_bookmark_toggle(path)

    def _copy_current_path(self) -> None:
        path = self._current_path()
        QtWidgets.QApplication.clipboard().setText(path)

    def add_bookmark_current(self) -> None:
        path = self._current_path()
        if not path:
            return
        self._add_bookmark(path)
        self._sync_bookmark_toggle(path)

    def remove_selected_bookmark(self) -> None:
        item = self.bookmarks_list.currentItem()
        if item is None:
            return
        self._remove_bookmark(item.text())
        self._sync_bookmark_toggle(self._current_path())

    def _toggle_bookmark_current(self, checked: bool) -> None:
        if self._bookmark_syncing:
            return
        path = self._current_path()
        if not path:
            return
        if checked:
            self._add_bookmark(path)
        else:
            self._remove_bookmark(path)

    def _sync_bookmark_toggle(self, path: str) -> None:
        self._bookmark_syncing = True
        self.bookmark_toggle.setChecked(path in self._bookmarks)
        self._bookmark_syncing = False

    def _tree_context_menu(self, pos: QtCore.QPoint) -> None:
        if self._hive is None:
            return
        index = self.tree.indexAt(pos)
        item = self._get_item(index)
        if item is None:
            return
        path = item.data(PATH_ROLE)
        menu = QtWidgets.QMenu(self)
        new_action = menu.addAction("New Key")
        rename_action = menu.addAction("Rename Key")
        delete_action = menu.addAction("Delete Key")
        menu.addSeparator()
        copy_action = menu.addAction("Copy Path")
        bookmark_action = menu.addAction("Add Bookmark" if path not in self._bookmarks else "Remove Bookmark")

        is_root = path == ""
        rename_action.setEnabled(not is_root and not self._read_only)
        delete_action.setEnabled(not is_root and not self._read_only)
        new_action.setEnabled(not self._read_only)

        action = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if action == new_action:
            self._create_key(path)
        elif action == rename_action:
            self._rename_key(path)
        elif action == delete_action:
            self._delete_key(path)
        elif action == copy_action:
            QtWidgets.QApplication.clipboard().setText(path)
        elif action == bookmark_action:
            if path in self._bookmarks:
                self._remove_bookmark(path)
            else:
                self._add_bookmark(path)

    def _value_context_menu(self, pos: QtCore.QPoint) -> None:
        if self._hive is None:
            return
        index = self.values_table.indexAt(pos)
        menu = QtWidgets.QMenu(self)
        new_action = menu.addAction("New Value")
        edit_action = menu.addAction("Edit Value")
        delete_action = menu.addAction("Delete Value")
        menu.addSeparator()
        copy_name_action = menu.addAction("Copy Value Name")
        copy_data_action = menu.addAction("Copy Value Data")

        has_selection = index.isValid()
        edit_action.setEnabled(has_selection and not self._read_only)
        delete_action.setEnabled(has_selection and not self._read_only)
        new_action.setEnabled(not self._read_only)
        copy_name_action.setEnabled(has_selection)
        copy_data_action.setEnabled(has_selection)

        action = menu.exec(self.values_table.viewport().mapToGlobal(pos))
        if action == new_action:
            self._add_value()
        elif action == edit_action:
            self._edit_value(index.row())
        elif action == delete_action:
            self._delete_value(index.row())
        elif action == copy_name_action:
            name_item = self.values_table.item(index.row(), 0)
            QtWidgets.QApplication.clipboard().setText(name_item.text())
        elif action == copy_data_action:
            data_item = self.values_table.item(index.row(), 2)
            QtWidgets.QApplication.clipboard().setText(data_item.text())

    def _create_key(self, parent_path: str) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "New Key", "Key name")
        if not ok or not name:
            return
        new_path = f"{parent_path}\\{name}" if parent_path else name
        try:
            self._hive.create_key(new_path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Create Key Failed", str(exc))
            return
        parent_item = self._path_to_item.get(parent_path) if parent_path else self.tree.model().item(0)
        if parent_item is not None:
            parent_item.setData(False, LOADED_ROLE)
            self._populate_item(parent_item)
            self._select_path(new_path)
            self._set_dirty(True)

    def _rename_key(self, path: str) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        parts = path.split("\\") if path else []
        current_name = parts[-1] if parts else ""
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename Key", "New key name", text=current_name
        )
        if not ok or not new_name or new_name == current_name:
            return
        try:
            success = self._hive.rename_key(path, new_name)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Rename Failed", str(exc))
            return
        if not success:
            QtWidgets.QMessageBox.warning(self, "Rename Failed", "Key not found.")
            return
        parent_path = "\\".join(parts[:-1])
        parent_item = self._path_to_item.get(parent_path) if parent_path else self.tree.model().item(0)
        if parent_item is not None:
            parent_item.setData(False, LOADED_ROLE)
            self._populate_item(parent_item)
            new_path = f"{parent_path}\\{new_name}" if parent_path else new_name
            self._select_path(new_path)
            self._set_dirty(True)

    def _delete_key(self, path: str) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Key",
            f"Delete key '{path}' and all subkeys?",
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        try:
            success = self._hive.delete_key(path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Delete Failed", str(exc))
            return
        if not success:
            QtWidgets.QMessageBox.warning(self, "Delete Failed", "Key not found.")
            return
        parent_path = "\\".join(path.split("\\")[:-1])
        parent_item = self._path_to_item.get(parent_path) if parent_path else self.tree.model().item(0)
        if parent_item is not None:
            parent_item.setData(False, LOADED_ROLE)
            self._populate_item(parent_item)
            self._refresh_values(parent_path)
            self._set_dirty(True)

    def _add_value(self) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        path = self._current_path()
        dialog = ValueEditorDialog(self, "New Value", "", RegistryType.REG_SZ, "")
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            name, value_type, parsed = dialog.get_value()
            self._hive.set_value(path, name, value_type, parsed)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Add Value Failed", str(exc))
            return
        self._refresh_values(path)
        self._set_dirty(True)

    def _edit_value(self, row: int) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        path = self._current_path()
        name_item = self.values_table.item(row, 0)
        if name_item is None:
            return
        value_name = name_item.data(VALUE_NAME_ROLE) or ""
        value = self._hive.get_value(path, value_name)
        if value is None:
            QtWidgets.QMessageBox.warning(self, "Edit Failed", "Value not found.")
            return
        data_text = format_value_edit_text(value)
        try:
            reg_type = RegistryType(value.type)
        except ValueError:
            reg_type = RegistryType.REG_BINARY
        dialog = ValueEditorDialog(self, "Edit Value", value.name, reg_type, data_text)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            name, value_type, parsed = dialog.get_value()
            if name != value.name:
                self._hive.delete_value(path, value.name)
            self._hive.set_value(path, name, value_type, parsed)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Edit Value Failed", str(exc))
            return
        self._refresh_values(path)
        self._set_dirty(True)

    def _delete_value(self, row: int) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        path = self._current_path()
        name_item = self.values_table.item(row, 0)
        if name_item is None:
            return
        value_name = name_item.data(VALUE_NAME_ROLE) or ""
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Value",
            f"Delete value '{name_item.text()}'?",
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        try:
            success = self._hive.delete_value(path, value_name)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Delete Failed", str(exc))
            return
        if not success:
            QtWidgets.QMessageBox.warning(self, "Delete Failed", "Value not found.")
            return
        self._refresh_values(path)
        self._set_dirty(True)

    def _current_path(self) -> str:
        index = self.tree.currentIndex()
        item = self._get_item(index)
        if item is None:
            return ""
        return item.data(PATH_ROLE) or ""

    def _select_path(self, path: str) -> bool:
        if self._hive is None:
            return False
        parts = path.split("\\") if path else []
        item = self.tree.model().item(0)
        current_path = ""
        for part in parts:
            if item is None:
                return False
            self._populate_item(item)
            parent_index = self.tree.model().indexFromItem(item)
            if parent_index.isValid():
                self.tree.expand(parent_index)
            next_item = None
            for row in range(item.rowCount()):
                child = item.child(row)
                if child.text() == part:
                    next_item = child
                    break
            if next_item is None:
                return False
            current_path = f"{current_path}\\{part}" if current_path else part
            item = next_item
        if item is not None:
            index = self.tree.model().indexFromItem(item)
            self.tree.setCurrentIndex(index)
            self.tree.scrollTo(index)
            return True
        return False

    def _start_search(self) -> None:
        if self._hive is None:
            return
        query = self.search_input.text().strip()
        if not query:
            return
        self._stop_search()
        self.search_results.clear()
        self.export_search_action.setEnabled(False)
        self._search_active = True
        self._search_last_count = 0
        self._search_cancelled = False
        self.search_cancel_button.setEnabled(True)
        self.search_status.setText("Searching...")
        self.statusBar().showMessage("Searching...")
        self._search_thread = QtCore.QThread(self)
        self._search_worker = SearchWorker(self._hive, query)
        self._search_worker.moveToThread(self._search_thread)
        self._search_thread.started.connect(self._search_worker.run)
        self._search_worker.results_ready.connect(self._on_search_results)
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.finished.connect(self._search_thread.quit)
        self._search_worker.finished.connect(self._search_worker.deleteLater)
        self._search_thread.finished.connect(self._search_thread.deleteLater)
        self._search_thread.finished.connect(self._on_search_finished)
        self._search_thread.start()

    def _stop_search(self) -> None:
        if self._search_worker is not None:
            self._search_worker.cancel()
        if self._search_thread is not None:
            self._search_thread.requestInterruption()
            try:
                self._search_thread.quit()
            except RuntimeError:
                pass

    def _on_search_finished(self) -> None:
        self._search_worker = None
        self._search_thread = None
        self._search_active = False
        self.search_cancel_button.setEnabled(False)
        if not self._search_last_count and not self._search_cancelled:
            self.search_status.setText("Idle")

    def _on_search_progress(self, total: int, matched: int) -> None:
        self.statusBar().showMessage(f"Searching... scanned {total} keys, {matched} matches")
        self.search_status.setText(f"Scanned {total} keys, {matched} matches")

    def _on_search_results(self, results: list[SearchResult], cancelled: bool) -> None:
        self._search_last_count = len(results)
        self._search_cancelled = cancelled
        for result in results:
            if result.kind == "key":
                label = f"[Key] {result.path}"
            else:
                value_name = result.value_name or "(Default)"
                label = f"[Value] {result.path} \\ {value_name}"
                if result.value_data:
                    label += f" = {result.value_data}"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, result)
            self.search_results.addItem(item)
        self.export_search_action.setEnabled(bool(results))
        if cancelled:
            self.search_status.setText(f"Cancelled ({len(results)} results)")
            self.statusBar().showMessage(f"Search cancelled: {len(results)} results")
        else:
            self.search_status.setText(f"Results: {len(results)}")
            self.statusBar().showMessage(f"Search complete: {len(results)} results")

    def _cancel_search(self) -> None:
        if not self._search_active:
            return
        self._stop_search()
        self.search_status.setText("Cancelling...")
        self.statusBar().showMessage("Cancelling search...")
        self.search_cancel_button.setEnabled(False)

    def _toggle_results(self, visible: bool) -> None:
        if visible:
            self.results_container.show()
            self.search_results.show()
            if self._right_splitter_sizes:
                self.right_splitter.setSizes(self._right_splitter_sizes)
            else:
                self.right_splitter.setSizes([3, 2, 1])
            self._results_visible = True
            self.results_toggle.setArrowType(QtCore.Qt.DownArrow)
            self.results_container.setMaximumHeight(16777215)
            self.results_container.setMinimumHeight(self._results_header_height())
            return
        if self.results_container.isVisible():
            self._right_splitter_sizes = self.right_splitter.sizes()
        self.search_results.hide()
        header_height = self._results_header_height()
        self.results_container.setMinimumHeight(header_height)
        self.results_container.setMaximumHeight(header_height)
        self.right_splitter.setSizes([2, 1, 0])
        self._results_visible = False
        self.results_toggle.setArrowType(QtCore.Qt.RightArrow)

    def _restore_results_state(self) -> None:
        visible = self._settings.value("results_visible", True, type=bool)
        sizes = self._settings.value("results_sizes", None)
        if sizes is not None:
            try:
                parsed_sizes = [int(value) for value in sizes]
            except (TypeError, ValueError):
                parsed_sizes = None
            else:
                self._right_splitter_sizes = parsed_sizes
        self.results_toggle.setChecked(bool(visible))
        self._toggle_results(bool(visible))

    def _results_header_height(self) -> int:
        layout = self.results_container.layout()
        if layout is None:
            return self.results_toggle.sizeHint().height()
        margins = layout.contentsMargins()
        return self.results_toggle.sizeHint().height() + margins.top() + margins.bottom()

    def _load_bookmarks(self) -> None:
        bookmarks = self._settings.value("bookmarks", [])
        if isinstance(bookmarks, str):
            bookmarks = [bookmarks]
        if not isinstance(bookmarks, list):
            bookmarks = []
        self._bookmarks = [str(item) for item in bookmarks if str(item)]
        self.bookmarks_list.clear()
        self.bookmarks_list.addItems(self._bookmarks)

    def _save_bookmarks(self) -> None:
        self._settings.setValue("bookmarks", self._bookmarks)

    def _add_bookmark(self, path: str) -> None:
        if not path or path in self._bookmarks:
            return
        self._bookmarks.append(path)
        self._bookmarks.sort(key=str.casefold)
        self.bookmarks_list.clear()
        self.bookmarks_list.addItems(self._bookmarks)
        self._save_bookmarks()

    def _remove_bookmark(self, path: str) -> None:
        if path not in self._bookmarks:
            return
        self._bookmarks.remove(path)
        self.bookmarks_list.clear()
        self.bookmarks_list.addItems(self._bookmarks)
        self._save_bookmarks()

    def _on_bookmark_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        self._select_path(item.text())

    def _on_search_item_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        result = item.data(QtCore.Qt.UserRole)
        if not isinstance(result, SearchResult):
            return
        self._select_path(result.path)
        if result.kind == "value" and result.value_name is not None:
            self._highlight_value(result.value_name)

    def _highlight_value(self, value_name: str) -> None:
        for row in range(self.values_table.rowCount()):
            item = self.values_table.item(row, 0)
            if item and item.data(VALUE_NAME_ROLE) == value_name:
                self.values_table.selectRow(row)
                return

    def _get_item(self, index: QtCore.QModelIndex) -> QtGui.QStandardItem | None:
        if not index.isValid():
            return None
        model = self.tree.model()
        if not isinstance(model, QtGui.QStandardItemModel):
            return None
        return model.itemFromIndex(index)


def format_value_data(value: HiveValue) -> str:
    if isinstance(value.decoded, list):
        return "; ".join(str(item) for item in value.decoded)
    if isinstance(value.decoded, bytes):
        return value.data.hex(" ")
    return str(value.decoded)


def format_value_edit_text(value: HiveValue) -> str:
    if value.type == RegistryType.REG_MULTI_SZ:
        if isinstance(value.decoded, list):
            return "\n".join(value.decoded)
        return str(value.decoded)
    if value.type in (RegistryType.REG_BINARY, RegistryType.REG_NONE):
        return value.data.hex(" ")
    return str(value.decoded)


def parse_value_input(value_type: RegistryType, text: str) -> object:
    if value_type == RegistryType.REG_MULTI_SZ:
        return [line for line in text.splitlines() if line.strip()]
    if value_type == RegistryType.REG_DWORD:
        cleaned = text.strip()
        return int(cleaned, 0) if cleaned else 0
    if value_type == RegistryType.REG_QWORD:
        cleaned = text.strip()
        return int(cleaned, 0) if cleaned else 0
    if value_type == RegistryType.REG_BINARY:
        cleaned = re.sub(r"[^0-9a-fA-F]", "", text)
        if len(cleaned) % 2 != 0:
            raise ValueError("Binary hex must contain an even number of digits")
        return bytes.fromhex(cleaned) if cleaned else b""
    return text

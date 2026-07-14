"""PySide6 GUI for registry hive editing."""
from __future__ import annotations

import tempfile
from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .annotations import Annotation, AnnotationStore, utc_now
from .binary_view import format_hex_ascii, format_match_context, parse_hex_pattern
from .compare import DiffEntry, diff_entries_to_rows, diff_hives, summarize_diffs
from .fileio import atomic_copy_file
from .hive import Hive, HiveValue, RegistryType
from .plugins import (
    Plugin,
    discover_plugins,
    plugin_applicability,
    run_plugin_subprocess,
    user_plugin_directory,
)
from .provenance import HiveProvenance, hash_file, inspect_hive
from .reporting import export_rows, export_subtree
from .validation import (
    EDITABLE_VALUE_TYPES,
    parse_value_text,
    validate_key_name,
    validate_value_name,
)

PATH_ROLE = QtCore.Qt.UserRole + 1
LOADED_ROLE = QtCore.Qt.UserRole + 2
VALUE_NAME_ROLE = QtCore.Qt.UserRole + 3
NODE_ROLE = QtCore.Qt.UserRole + 4

SEARCH_BATCH_SIZE = 200
MAX_SEARCH_RESULTS = 50_000
SEARCH_RESULT_PREVIEW_CHARS = 2_048
TABLE_VALUE_PREVIEW_CHARS = 4_096
DETAIL_VALUE_PREVIEW_CHARS = 131_072


@dataclass(frozen=True)
class SearchResult:
    kind: str
    path: str
    value_name: str | None = None
    value_data: str | None = None
    value_data_truncated: bool = False
    match_offset: int | None = None


class FileHashThread(QtCore.QThread):
    hash_ready = QtCore.Signal(str)
    hash_error = QtCore.Signal(str)
    hash_progress = QtCore.Signal(int, int)

    def __init__(self, path: Path, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        try:
            digest = hash_file(
                self._path,
                cancelled=self.isInterruptionRequested,
                progress=self.hash_progress.emit,
            )
        except CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self.hash_error.emit(str(exc))
        else:
            self.hash_ready.emit(digest)


class HiveInformationDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, provenance: HiveProvenance) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hive Information")
        self.resize(720, 480)
        self._provenance = provenance
        self._sha256 = "Calculating..."

        form = QtWidgets.QFormLayout()
        header = provenance.header
        self._add_field(form, "Path", str(provenance.path))
        self._add_field(form, "Size", f"{provenance.size:,} bytes")
        self._add_field(form, "File modified (UTC)", provenance.modified_at.isoformat())
        self._hash_label = self._add_field(form, "SHA-256", self._sha256)
        self._add_field(form, "Embedded name", header.embedded_name or "(none)")
        self._add_field(
            form,
            "Hive last write (UTC)",
            header.last_write.isoformat()
            if header.last_write is not None
            else f"Unavailable (raw {header.last_write_raw})",
        )
        self._add_field(form, "Format version", f"{header.major_version}.{header.minor_version}")
        self._add_field(form, "File type / format", f"{header.file_type} / {header.file_format}")
        self._add_field(form, "Root cell offset", f"0x{header.root_cell_offset:08x}")
        self._add_field(form, "Hive bins size", f"{header.hive_bins_size:,} bytes")
        self._add_field(form, "Clustering factor", str(header.clustering_factor))
        sequence_state = "consistent" if header.sequence_consistent else "MISMATCH (possibly unclean)"
        self._add_field(
            form,
            "Sequence numbers",
            f"{header.primary_sequence} / {header.secondary_sequence} — {sequence_state}",
        )
        checksum_state = "valid" if header.checksum_valid else "INVALID"
        self._add_field(
            form,
            "Header checksum",
            f"0x{header.stored_checksum:08x} ({checksum_state}; "
            f"calculated 0x{header.calculated_checksum:08x})",
        )
        logs = "\n".join(str(path) for path in provenance.transaction_logs)
        self._add_field(form, "Transaction log sidecars", logs or "None detected")

        self._progress = QtWidgets.QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)

        copy_button = QtWidgets.QPushButton("Copy Summary")
        copy_button.clicked.connect(self._copy_summary)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addWidget(copy_button)
        button_row.addStretch(1)
        button_row.addWidget(buttons)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._progress)
        layout.addLayout(button_row)

        self._hash_thread = FileHashThread(provenance.path, self)
        self._hash_thread.hash_ready.connect(self._on_hash_ready)
        self._hash_thread.hash_error.connect(self._on_hash_error)
        self._hash_thread.hash_progress.connect(self._on_hash_progress)
        self._hash_thread.start()

    @staticmethod
    def _add_field(form: QtWidgets.QFormLayout, name: str, value: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(value)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        form.addRow(f"{name}:", label)
        return label

    def _on_hash_ready(self, digest: str) -> None:
        self._sha256 = digest
        self._hash_label.setText(digest)
        self._progress.setValue(100)

    def _on_hash_error(self, message: str) -> None:
        self._sha256 = f"Unavailable: {message}"
        self._hash_label.setText(self._sha256)
        self._progress.setVisible(False)

    def _on_hash_progress(self, processed: int, total: int) -> None:
        self._progress.setValue(100 if total == 0 else int(processed * 100 / total))

    def _copy_summary(self) -> None:
        info = self._provenance
        header = info.header
        logs = ", ".join(str(path) for path in info.transaction_logs) or "none"
        summary = "\n".join(
            [
                f"Path: {info.path}",
                f"Size: {info.size} bytes",
                f"SHA-256: {self._sha256}",
                f"Embedded name: {header.embedded_name}",
                f"Header version: {header.major_version}.{header.minor_version}",
                f"Sequences: {header.primary_sequence}/{header.secondary_sequence}",
                f"Checksum valid: {header.checksum_valid}",
                f"Transaction logs: {logs}",
            ]
        )
        QtWidgets.QApplication.clipboard().setText(summary)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self._hash_thread.isRunning():
            self._hash_thread.requestInterruption()
            self._hash_thread.wait(2000)
        super().closeEvent(event)


class SearchWorker(QtCore.QObject):
    results_batch = QtCore.Signal(int, list)
    completed = QtCore.Signal(int, bool, bool, int, int)
    progress = QtCore.Signal(int, int, int)
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(int)

    def __init__(
        self,
        hive_path: Path,
        query: str,
        request_id: int,
        *,
        mode: str = "text",
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> None:
        super().__init__()
        self._hive_path = hive_path
        if mode not in {"text", "hex"}:
            raise ValueError(f"Unsupported search mode: {mode}")
        self._mode = mode
        self._query = query.casefold()
        self._query_bytes = parse_hex_pattern(query) if mode == "hex" else b""
        self._request_id = request_id
        self._max_results = max_results
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def request_id(self) -> int:
        return self._request_id

    def _should_cancel(self) -> bool:
        thread = QtCore.QThread.currentThread()
        return self._cancelled or (thread is not None and thread.isInterruptionRequested())

    @QtCore.Slot()
    def run(self) -> None:
        pending: list[SearchResult] = []
        total = 0
        matched = 0
        cancelled = False
        truncated = False
        failed = False
        try:
            with Hive(self._hive_path, write=False) as hive:
                for path, node in hive.iter_key_nodes():
                    if self._should_cancel():
                        cancelled = True
                        break
                    total += 1
                    if self._mode == "text" and self._query in path.casefold():
                        pending.append(SearchResult(kind="key", path=path))
                        matched += 1
                        if matched >= self._max_results:
                            truncated = True
                            break
                        if len(pending) >= SEARCH_BATCH_SIZE:
                            self.results_batch.emit(self._request_id, pending)
                            pending = []
                    for value in hive.iter_values_for_node(node):
                        if self._should_cancel():
                            cancelled = True
                            break
                        value_name = value.name or "(Default)"
                        if self._mode == "hex":
                            match_offset = value.data.find(self._query_bytes)
                            is_match = match_offset >= 0
                            preview = (
                                format_match_context(
                                    value.data, match_offset, len(self._query_bytes)
                                )
                                if is_match
                                else ""
                            )
                            was_truncated = False
                        else:
                            match_offset = None
                            data_text = format_value_data(value)
                            haystack = f"{value_name}\n{data_text}".casefold()
                            is_match = self._query in haystack
                            preview, was_truncated = _truncate_search_preview(data_text)
                        if is_match:
                            pending.append(
                                SearchResult(
                                    kind="value",
                                    path=path,
                                    value_name=value.name,
                                    value_data=preview,
                                    value_data_truncated=was_truncated,
                                    match_offset=match_offset,
                                )
                            )
                            matched += 1
                            if matched >= self._max_results:
                                truncated = True
                                break
                        if len(pending) >= SEARCH_BATCH_SIZE:
                            self.results_batch.emit(self._request_id, pending)
                            pending = []
                    if cancelled:
                        break
                    if truncated:
                        break
                    if total % 200 == 0:
                        self.progress.emit(self._request_id, total, matched)
        except Exception as exc:  # noqa: BLE001
            failed = True
            self.error.emit(self._request_id, str(exc))
        finally:
            if not failed:
                if pending:
                    self.results_batch.emit(self._request_id, pending)
                self.completed.emit(
                    self._request_id, cancelled, truncated, total, matched
                )
            self.finished.emit(self._request_id)


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
        self._parsed_value: tuple[str, RegistryType, object] | None = None

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
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def get_value(self) -> tuple[str, RegistryType, object]:
        if self._parsed_value is not None:
            return self._parsed_value
        name = self.name_edit.text()
        validate_value_name(name)
        value_type = self.type_combo.currentData()
        data_text = self.data_edit.toPlainText()
        parsed = parse_value_input(value_type, data_text)
        return name, value_type, parsed

    def _validate_and_accept(self) -> None:
        try:
            self._parsed_value = self.get_value()
        except (TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid Value", str(exc))
            return
        self.accept()


class ResultsDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, title: str, rows: list[dict[str, object]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(900, 500)

        self._rows = rows
        self._filtered: list[dict[str, object]] = rows
        self._searchable_rows = [
            (row, " ".join(str(value) for value in row.values()).casefold()) for row in rows
        ]

        self.filter_input = QtWidgets.QLineEdit()
        self.filter_input.setPlaceholderText("Filter results...")
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.filter_input.textChanged.connect(self._schedule_filter)

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
        sorting_enabled = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        if not rows:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.table.setUpdatesEnabled(True)
            self.table.setSortingEnabled(sorting_enabled)
            return
        try:
            columns = sorted({key for row in rows for key in row.keys()})
            self.table.setColumnCount(len(columns))
            self.table.setHorizontalHeaderLabels(columns)
            self.table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                for col_index, key in enumerate(columns):
                    value = row.get(key, "")
                    item = QtWidgets.QTableWidgetItem(str(value))
                    self.table.setItem(row_index, col_index, item)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.setSortingEnabled(sorting_enabled)
        if len(rows) <= 1_000:
            self.table.resizeColumnsToContents()

    def _schedule_filter(self, _text: str) -> None:
        self._filter_timer.start()

    def _apply_filter(self, text: str | None = None) -> None:
        query = (self.filter_input.text() if text is None else text).strip().casefold()
        if not query:
            self._filtered = self._rows
        else:
            self._filtered = [row for row, haystack in self._searchable_rows if query in haystack]
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
    results_ready = QtCore.Signal(int, str, list)
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(int)

    def __init__(self, plugin: Plugin, hive_path: Path, job_id: int) -> None:
        super().__init__()
        self._plugin = plugin
        self._hive_path = hive_path
        self._job_id = job_id
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _should_cancel(self) -> bool:
        thread = QtCore.QThread.currentThread()
        return self._cancelled or (thread is not None and thread.isInterruptionRequested())

    @QtCore.Slot()
    def run(self) -> None:
        try:
            results = run_plugin_subprocess(
                self._plugin, self._hive_path, cancelled=self._should_cancel
            )
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.error.emit(self._job_id, str(exc))
        else:
            self.results_ready.emit(self._job_id, self._plugin.name, results)
        finally:
            self.finished.emit(self._job_id)


class CompareWorker(QtCore.QObject):
    results_ready = QtCore.Signal(int, list)
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(int)

    def __init__(self, left_path: Path, right_path: Path, job_id: int) -> None:
        super().__init__()
        self._left_path = left_path
        self._right_path = right_path
        self._job_id = job_id
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _should_cancel(self) -> bool:
        thread = QtCore.QThread.currentThread()
        return self._cancelled or (thread is not None and thread.isInterruptionRequested())

    @QtCore.Slot()
    def run(self) -> None:
        try:
            with Hive(self._left_path, write=False) as left, Hive(self._right_path, write=False) as right:
                results = diff_hives(left, right, cancelled=self._should_cancel)
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.error.emit(self._job_id, str(exc))
        else:
            self.results_ready.emit(self._job_id, results)
        finally:
            self.finished.emit(self._job_id)


class TimelineWorker(QtCore.QObject):
    results_ready = QtCore.Signal(int, list)
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(int)

    def __init__(self, hive_path: Path, job_id: int) -> None:
        super().__init__()
        self._hive_path = hive_path
        self._job_id = job_id
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _should_cancel(self) -> bool:
        thread = QtCore.QThread.currentThread()
        return self._cancelled or (thread is not None and thread.isInterruptionRequested())

    @QtCore.Slot()
    def run(self) -> None:
        try:
            rows: list[dict[str, object]] = []
            with Hive(self._hive_path, write=False) as hive:
                for path, node in hive.iter_key_nodes():
                    if self._should_cancel():
                        return
                    timestamp = hive.get_node_timestamp_info(node)
                    rows.append(
                        {
                            "path": path,
                            "timestamp": timestamp.display,
                            "timestamp_raw": timestamp.raw,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            self.error.emit(self._job_id, str(exc))
        else:
            self.results_ready.emit(self._job_id, rows)
        finally:
            self.finished.emit(self._job_id)


class HiveMainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Registry Hive GUI")
        self.resize(1200, 720)

        settings = QtCore.QSettings("reg_hive_gui", "RegHiveGUI")
        self._settings = settings
        self._hive_generation = 0
        # Editing must be explicitly enabled for every application session.
        self._read_only = True
        self._hive: Hive | None = None
        self._hive_path: Path | None = None
        self._dirty = False
        self._edit_revision = 0
        self._exported_revision = 0
        self._search_thread: QtCore.QThread | None = None
        self._search_worker: SearchWorker | None = None
        self._search_generation = 0
        self._search_temp_paths: dict[int, Path] = {}
        self._path_to_item: dict[str, QtGui.QStandardItem] = {}
        self._search_active = False
        self._search_last_count = 0
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
        self._annotations: dict[str, Annotation] = {}
        self._annotation_store = AnnotationStore()
        self._hive_sha256: str | None = None
        self._annotation_hash_thread: FileHashThread | None = None
        self._bookmark_syncing = False
        self._temp_paths: list[Path] = []
        self._tree_selection_model: QtCore.QItemSelectionModel | None = None

        self._create_actions()
        self._build_ui()
        self._set_ui_enabled(False)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if not self._confirm_discard_changes("Closing the application"):
            event.ignore()
            return
        if not self._stop_background_jobs(wait=True):
            QtWidgets.QMessageBox.warning(
                self,
                "Background Task Still Running",
                "A background task is still stopping. Please wait a moment and close again.",
            )
            event.ignore()
            return
        if self._hive is not None:
            self._hive.close()
        self._settings.setValue("results_visible", self._results_visible)
        if self._right_splitter_sizes is None:
            self._right_splitter_sizes = self.right_splitter.sizes()
        if self._right_splitter_sizes is not None:
            self._settings.setValue("results_sizes", self._right_splitter_sizes)
        for path in self._temp_paths:
            try:
                path.unlink()
            except Exception:
                pass
        super().closeEvent(event)

    def _create_actions(self) -> None:
        self.open_action = QtGui.QAction("Open Hive...", self)
        self.open_action.triggered.connect(self.open_hive_dialog)

        self.hive_info_action = QtGui.QAction("Hive Information...", self)
        self.hive_info_action.triggered.connect(self.show_hive_information)

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

        self.edit_bookmark_note_action = QtGui.QAction("Edit Bookmark Note...", self)
        self.edit_bookmark_note_action.triggered.connect(self.edit_selected_bookmark_note)

        self.exit_action = QtGui.QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)

    def _build_ui(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.hive_info_action)
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
        navigate_menu.addAction(self.edit_bookmark_note_action)

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
        self.bookmark_note_button = QtWidgets.QToolButton()
        self.bookmark_note_button.setText("Note")
        self.bookmark_note_button.setToolTip("Edit note for selected bookmark")
        self.bookmark_note_button.clicked.connect(self.edit_selected_bookmark_note)

        bookmarks_header = QtWidgets.QHBoxLayout()
        bookmarks_header.addWidget(QtWidgets.QLabel("Bookmarks"))
        bookmarks_header.addStretch(1)
        bookmarks_header.addWidget(self.bookmark_add_button)
        bookmarks_header.addWidget(self.bookmark_remove_button)
        bookmarks_header.addWidget(self.bookmark_note_button)

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
        self.value_raw.setPlaceholderText("Offset / hex / ASCII view")
        details_layout.addWidget(self.value_meta)
        details_layout.addWidget(QtWidgets.QLabel("Decoded"))
        details_layout.addWidget(self.value_decoded)
        details_layout.addWidget(QtWidgets.QLabel("Hex / ASCII"))
        details_layout.addWidget(self.value_raw)

        self.search_mode = QtWidgets.QComboBox()
        self.search_mode.addItem("Text", "text")
        self.search_mode.addItem("Hex Bytes", "hex")
        self.search_mode.currentIndexChanged.connect(self._on_search_mode_changed)
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
        search_layout.addWidget(self.search_mode)
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

        self._render_bookmarks()
        self._load_plugins()

    def _set_ui_enabled(self, enabled: bool) -> None:
        self.hive_info_action.setEnabled(enabled)
        self.export_action.setEnabled(enabled)
        self.export_subtree_action.setEnabled(enabled)
        self.export_search_action.setEnabled(enabled and self.search_results.count() > 0)
        self.compare_action.setEnabled(enabled)
        self.export_diff_action.setEnabled(enabled and bool(self._last_diff_entries))
        self.tree.setEnabled(enabled)
        self.values_table.setEnabled(enabled)
        self.search_input.setEnabled(enabled)
        self.search_mode.setEnabled(enabled)
        self.search_button.setEnabled(enabled)
        self.search_cancel_button.setEnabled(enabled and self._search_active)
        self.search_results.setEnabled(enabled)
        self.jump_action.setEnabled(enabled)
        self.timeline_action.setEnabled(enabled)
        self.bookmark_add_button.setEnabled(enabled)
        self.bookmark_remove_button.setEnabled(enabled)
        self.bookmark_note_button.setEnabled(enabled)
        self.copy_path_button.setEnabled(enabled)
        self.bookmark_toggle.setEnabled(enabled)
        self.add_bookmark_action.setEnabled(enabled)
        self.remove_bookmark_action.setEnabled(enabled)
        self.edit_bookmark_note_action.setEnabled(enabled)

    def _set_annotation_ui_enabled(self, enabled: bool) -> None:
        self.bookmark_add_button.setEnabled(enabled)
        self.bookmark_remove_button.setEnabled(enabled)
        self.bookmark_note_button.setEnabled(enabled)
        self.bookmark_toggle.setEnabled(enabled)
        self.add_bookmark_action.setEnabled(enabled)
        self.remove_bookmark_action.setEnabled(enabled)
        self.edit_bookmark_note_action.setEnabled(enabled)

    def _set_dirty(self, dirty: bool = True) -> None:
        self._dirty = dirty
        if dirty:
            self._edit_revision += 1
        else:
            self._edit_revision = 0
            self._exported_revision = 0
        self._update_title()

    def _has_unexported_changes(self) -> bool:
        return self._dirty and self._edit_revision != self._exported_revision

    def _confirm_discard_changes(self, action: str) -> bool:
        if not self._has_unexported_changes():
            return True
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Discard Unexported Changes?",
            f"{action} will discard changes that have not been exported. Continue?",
        )
        return confirm == QtWidgets.QMessageBox.Yes

    def _update_title(self) -> None:
        if self._hive_path is None:
            self.setWindowTitle("Registry Hive GUI")
            return
        marker = "*" if self._has_unexported_changes() else ""
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
        search_paths = [
            (Path(__file__).with_name("builtin_plugins"), True),
            (user_plugin_directory(), False),
        ]
        self._plugins, errors = discover_plugins(search_paths)
        self.plugins_menu.clear()
        if not self._plugins:
            action = QtGui.QAction("No plugins found", self)
            action.setEnabled(False)
            self.plugins_menu.addAction(action)
            return
        for plugin in self._plugins:
            label = plugin.name if plugin.trusted else f"External: {plugin.name}"
            if plugin.target_hives:
                label += f" [{'/'.join(plugin.target_hives)}]"
            action = QtGui.QAction(label, self)
            applicable, reason = plugin_applicability(plugin, self._hive)
            action.setEnabled(applicable)
            source = f"\nVersion: {plugin.version}\nSource: {plugin.path}"
            action.setToolTip(f"{plugin.description}\n{reason}{source}")
            action.triggered.connect(lambda _checked=False, p=plugin: self._run_plugin(p))
            self.plugins_menu.addAction(action)
        if errors:
            self.plugins_menu.addSeparator()
            action = QtGui.QAction(f"{len(errors)} invalid plugin(s) skipped", self)
            action.setEnabled(False)
            action.setToolTip("\n".join(f"{error.path}: {error.message}" for error in errors))
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

    def _cleanup_temp_path(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
        if path in self._temp_paths:
            self._temp_paths.remove(path)

    @staticmethod
    def _request_worker_stop(worker: QtCore.QObject | None, thread: QtCore.QThread | None) -> None:
        if worker is not None:
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
        if thread is not None:
            try:
                thread.requestInterruption()
                thread.quit()
            except RuntimeError:
                pass

    def _stop_background_jobs(self, *, wait: bool) -> bool:
        jobs = [
            (self._search_worker, self._search_thread),
            (self._plugin_worker, self._plugin_thread),
            (self._compare_worker, self._compare_thread),
            (self._timeline_worker, self._timeline_thread),
        ]
        for worker, thread in jobs:
            self._request_worker_stop(worker, thread)
        annotation_thread = self._annotation_hash_thread
        if annotation_thread is not None and annotation_thread.isRunning():
            annotation_thread.requestInterruption()
        all_stopped = True
        if wait:
            for _worker, thread in jobs:
                if thread is not None and thread.isRunning() and not thread.wait(5000):
                    all_stopped = False
            if (
                annotation_thread is not None
                and annotation_thread.isRunning()
                and not annotation_thread.wait(5000)
            ):
                all_stopped = False
        if not all_stopped:
            return False

        self._search_worker = None
        self._search_thread = None
        self._plugin_worker = None
        self._plugin_thread = None
        self._compare_worker = None
        self._compare_thread = None
        self._timeline_worker = None
        self._timeline_thread = None
        self._annotation_hash_thread = None
        for request_id, path in list(self._search_temp_paths.items()):
            self._cleanup_temp_path(path)
            self._search_temp_paths.pop(request_id, None)
        self._cleanup_temp_path(self._plugin_temp_path)
        self._plugin_temp_path = None
        self._cleanup_temp_path(self._compare_temp_path)
        self._compare_temp_path = None
        self._cleanup_temp_path(self._timeline_temp_path)
        self._timeline_temp_path = None
        return True

    def open_hive_dialog(self) -> None:
        if not self._confirm_discard_changes("Opening a new hive"):
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
        if not new_state:
            confirm = QtWidgets.QMessageBox.warning(
                self,
                "Enable Editing?",
                "Editing changes an in-memory working copy. Preserve the original evidence file and "
                "export changes to a separate verified hive. Enable editing?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if confirm != QtWidgets.QMessageBox.Yes:
                self.read_only_action.setChecked(True)
                return
        if not self._confirm_discard_changes("Switching read-only mode"):
            self.read_only_action.setChecked(self._read_only)
            return
        self._read_only = new_state
        if self._hive_path is not None:
            self.load_hive(self._hive_path)

    def load_hive(self, path: Path) -> None:
        self._hive_generation += 1
        self._search_generation += 1
        if not self._stop_background_jobs(wait=True):
            QtWidgets.QMessageBox.warning(
                self,
                "Open Hive Delayed",
                "A background task is still stopping. Please try again in a moment.",
            )
            return
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
        self._hive_sha256 = None
        self._annotations.clear()
        self._bookmarks.clear()
        self._render_bookmarks()
        self._path_to_item.clear()
        self._dirty = False
        self._edit_revision = 0
        self._exported_revision = 0
        self._last_diff_entries = []
        self.search_results.clear()
        self.search_status.setText("Idle")

        model = QtGui.QStandardItemModel()
        root_item = QtGui.QStandardItem("ROOT")
        root_item.setEditable(False)
        root_item.setData("", PATH_ROLE)
        root_item.setData(False, LOADED_ROLE)
        root_item.setData(self._hive.get_node(""), NODE_ROLE)
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
        self._set_annotation_ui_enabled(False)
        self._load_plugins()
        self._update_title()
        self._update_key_info("")
        self.statusBar().showMessage(
            f"Loaded hive with {new_hive.backend_name}; calculating evidence SHA-256: {path}"
        )
        self._start_annotation_hash(path, self._hive_generation)

    def _start_annotation_hash(self, path: Path, generation: int) -> None:
        thread = FileHashThread(path, self)
        self._annotation_hash_thread = thread
        thread.hash_ready.connect(
            lambda digest, job_id=generation: self._on_annotation_hash_ready(job_id, digest)
        )
        thread.hash_error.connect(
            lambda message, job_id=generation: self._on_annotation_hash_error(job_id, message)
        )
        thread.finished.connect(lambda current=thread: self._on_annotation_hash_finished(current))
        thread.start()

    def _on_annotation_hash_ready(self, generation: int, digest: str) -> None:
        if generation != self._hive_generation:
            return
        self._hive_sha256 = digest
        try:
            annotations = self._annotation_store.load(digest)
        except Exception as exc:  # noqa: BLE001
            self._annotations.clear()
            self._bookmarks.clear()
            self._render_bookmarks()
            QtWidgets.QMessageBox.warning(
                self,
                "Annotations Unavailable",
                f"The annotation sidecar could not be loaded safely:\n\n{exc}",
            )
        else:
            self._annotations = {annotation.path: annotation for annotation in annotations}
            self._bookmarks = sorted(self._annotations, key=str.casefold)
            self._render_bookmarks()
        self._set_annotation_ui_enabled(True)
        self._sync_bookmark_toggle(self._current_path())
        self.statusBar().showMessage(f"Evidence identity ready: SHA-256 {digest}")

    def _on_annotation_hash_error(self, generation: int, message: str) -> None:
        if generation != self._hive_generation:
            return
        self.statusBar().showMessage(f"Evidence hashing failed; annotations disabled: {message}")

    def _on_annotation_hash_finished(self, thread: FileHashThread) -> None:
        if self._annotation_hash_thread is thread:
            self._annotation_hash_thread = None

    def show_hive_information(self) -> None:
        if self._hive_path is None:
            return
        try:
            provenance = inspect_hive(self._hive_path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Hive Information Failed", str(exc))
            return
        HiveInformationDialog(self, provenance).exec()

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
                output = atomic_copy_file(self._hive_path, path)
                digest = hash_file(output)
            else:
                output = self._hive.export(path)
                digest = self._hive.last_export_sha256 or hash_file(output)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(exc))
            return
        self._exported_revision = self._edit_revision
        self._update_title()
        self.statusBar().showMessage(f"Exported hive to {output} — SHA-256 {digest}")

    def export_subtree_report(self) -> None:
        if self._hive is None:
            return
        start_path = self._current_path()
        output_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Subtree Report",
            str(Path.home() / "subtree_report.json"),
            "JSON (*.json);;CSV (*.csv)",
        )
        if not output_path:
            return
        try:
            export_subtree(self._hive, start_path or None, output_path)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported subtree report to {output_path}")

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
        self._timeline_worker = TimelineWorker(snapshot_path, self._hive_generation)
        self._timeline_worker.moveToThread(self._timeline_thread)
        self._timeline_thread.started.connect(self._timeline_worker.run)
        self._timeline_worker.results_ready.connect(self._on_timeline_results)
        self._timeline_worker.error.connect(self._on_timeline_error)
        self._timeline_worker.finished.connect(self._timeline_thread.quit)
        self._timeline_worker.finished.connect(self._timeline_worker.deleteLater)
        self._timeline_worker.finished.connect(self._on_timeline_finished)
        self._timeline_thread.finished.connect(self._timeline_thread.deleteLater)
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
                    "value_data_truncated": result.value_data_truncated,
                    "match_offset": result.match_offset,
                }
            )
        return rows

    def _start_compare(self, left_path: Path, right_path: Path) -> None:
        if self._compare_thread is not None:
            QtWidgets.QMessageBox.information(self, "Compare", "A comparison is already running.")
            return
        self.statusBar().showMessage("Comparing hives...")
        self._compare_thread = QtCore.QThread(self)
        self._compare_worker = CompareWorker(left_path, right_path, self._hive_generation)
        self._compare_worker.moveToThread(self._compare_thread)
        self._compare_thread.started.connect(self._compare_worker.run)
        self._compare_worker.results_ready.connect(self._on_compare_results)
        self._compare_worker.error.connect(self._on_compare_error)
        self._compare_worker.finished.connect(self._compare_thread.quit)
        self._compare_worker.finished.connect(self._compare_worker.deleteLater)
        self._compare_worker.finished.connect(self._on_compare_finished)
        self._compare_thread.finished.connect(self._compare_thread.deleteLater)
        self._compare_thread.start()

    def _on_compare_results(self, job_id: int, results: list[DiffEntry]) -> None:
        if job_id != self._hive_generation:
            return
        self._last_diff_entries = results
        self.export_diff_action.setEnabled(bool(results))
        rows = diff_entries_to_rows(results)
        summary = summarize_diffs(results)
        title = (
            f"Hive Diff — {summary['total']} raw changes; "
            f"{summary['probable_value_renames']} probable value renames"
        )
        dialog = ResultsDialog(self, title, rows)
        dialog.show()
        self.statusBar().showMessage(
            f"Compare complete: {summary['total']} raw changes "
            f"({summary['probable_value_renames']} probable value renames)"
        )

    def _on_compare_error(self, job_id: int, message: str) -> None:
        if job_id != self._hive_generation:
            return
        QtWidgets.QMessageBox.critical(self, "Compare Failed", message)
        self.statusBar().showMessage("Compare failed")

    def _on_compare_finished(self, job_id: int) -> None:
        if job_id != self._hive_generation:
            return
        self._compare_worker = None
        self._compare_thread = None
        self._cleanup_temp_path(self._compare_temp_path)
        self._compare_temp_path = None

    def _on_timeline_results(self, job_id: int, rows: list[dict[str, object]]) -> None:
        if job_id != self._hive_generation:
            return
        dialog = ResultsDialog(self, "Timeline", rows)
        dialog.show()
        self.statusBar().showMessage(f"Timeline ready: {len(rows)} keys")

    def _on_timeline_error(self, job_id: int, message: str) -> None:
        if job_id != self._hive_generation:
            return
        QtWidgets.QMessageBox.critical(self, "Timeline Failed", message)
        self.statusBar().showMessage("Timeline failed")

    def _on_timeline_finished(self, job_id: int) -> None:
        if job_id != self._hive_generation:
            return
        self._timeline_worker = None
        self._timeline_thread = None
        self._cleanup_temp_path(self._timeline_temp_path)
        self._timeline_temp_path = None

    def _run_plugin(self, plugin: Plugin) -> None:
        if self._hive_path is None:
            return
        if self._plugin_thread is not None:
            QtWidgets.QMessageBox.information(self, "Plugin", "A plugin is already running.")
            return
        if not plugin.trusted:
            confirm = QtWidgets.QMessageBox.warning(
                self,
                "Run External Plugin?",
                f"Python plugins can access files and programs as your user. Run this plugin?\n\n"
                f"{plugin.path}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if confirm != QtWidgets.QMessageBox.Yes:
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
        self._plugin_worker = PluginWorker(plugin, snapshot_path, self._hive_generation)
        self._plugin_worker.moveToThread(self._plugin_thread)
        self._plugin_thread.started.connect(self._plugin_worker.run)
        self._plugin_worker.results_ready.connect(self._on_plugin_results)
        self._plugin_worker.error.connect(self._on_plugin_error)
        self._plugin_worker.finished.connect(self._plugin_thread.quit)
        self._plugin_worker.finished.connect(self._plugin_worker.deleteLater)
        self._plugin_worker.finished.connect(self._on_plugin_finished)
        self._plugin_thread.finished.connect(self._plugin_thread.deleteLater)
        self._plugin_thread.start()

    def _on_plugin_results(
        self, job_id: int, name: str, results: list[dict[str, object]]
    ) -> None:
        if job_id != self._hive_generation:
            return
        dialog = ResultsDialog(self, f"Plugin: {name}", results)
        dialog.show()
        self.statusBar().showMessage(f"Plugin complete: {name} ({len(results)} rows)")

    def _on_plugin_error(self, job_id: int, message: str) -> None:
        if job_id != self._hive_generation:
            return
        QtWidgets.QMessageBox.critical(self, "Plugin Failed", message)
        self.statusBar().showMessage("Plugin failed")

    def _on_plugin_finished(self, job_id: int) -> None:
        if job_id != self._hive_generation:
            return
        self._plugin_worker = None
        self._plugin_thread = None
        self._cleanup_temp_path(self._plugin_temp_path)
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
                if existing.startswith(prefix):
                    self._path_to_item.pop(existing, None)
        item.removeRows(0, item.rowCount())
        node = item.data(NODE_ROLE)
        if node is None:
            node = self._hive.get_node(path)
        if node is None:
            item.setData(True, LOADED_ROLE)
            return
        item.setData(node, NODE_ROLE)
        subkeys = sorted(self._hive.iter_subkeys_for_node(node), key=lambda child: child[0].casefold())
        for name, child_node, has_children in subkeys:
            child_item = QtGui.QStandardItem(name)
            child_item.setEditable(False)
            child_path = f"{path}\\{name}" if path else name
            child_item.setData(child_path, PATH_ROLE)
            child_item.setData(False, LOADED_ROLE)
            child_item.setData(child_node, NODE_ROLE)
            self._path_to_item[child_path] = child_item
            if has_children:
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
        self.values_table.setUpdatesEnabled(False)
        try:
            self.values_table.setRowCount(len(values))
            for row, value in enumerate(values):
                name_display = value.name if value.name else "(Default)"
                name_item = QtWidgets.QTableWidgetItem(name_display)
                name_item.setData(VALUE_NAME_ROLE, value.name)
                type_item = QtWidgets.QTableWidgetItem(value.type_name)
                data_text, _truncated = _truncate_text(
                    format_value_data(value), TABLE_VALUE_PREVIEW_CHARS
                )
                data_item = QtWidgets.QTableWidgetItem(data_text)
                self.values_table.setItem(row, 0, name_item)
                self.values_table.setItem(row, 1, type_item)
                self.values_table.setItem(row, 2, data_item)
        finally:
            self.values_table.setUpdatesEnabled(True)
        if len(values) <= 1_000:
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
            decoded_text = "Binary or opaque data; inspect the Hex / ASCII view below."
        elif isinstance(value.decoded, str):
            decoded_text = value.decoded[: DETAIL_VALUE_PREVIEW_CHARS + 1]
        else:
            decoded_text = str(value.decoded)
        decoded_preview, decoded_truncated = _truncate_text(
            decoded_text, DETAIL_VALUE_PREVIEW_CHARS
        )
        raw_preview, raw_truncated = format_hex_ascii(value.data)
        if decoded_truncated or raw_truncated:
            meta += " | Display preview truncated; Copy Value Data retains the complete value"
            self.value_meta.setText(meta)
        self.value_decoded.setPlainText(decoded_preview)
        self.value_raw.setPlainText(raw_preview)

    def _update_key_info(self, path: str | None) -> None:
        path = path or ""
        display_path = path if path else "ROOT"
        timestamp = self._hive.get_key_timestamp_info(path) if self._hive is not None else None
        time_text = timestamp.display if timestamp and timestamp.display else "-"
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

    def edit_selected_bookmark_note(self) -> None:
        item = self.bookmarks_list.currentItem()
        path = item.text() if item is not None else self._current_path()
        annotation = self._annotations.get(path)
        if annotation is None:
            return
        note, accepted = QtWidgets.QInputDialog.getMultiLineText(
            self,
            "Bookmark Note",
            f"Note for {path}",
            annotation.note,
        )
        if not accepted:
            return
        if len(note) > 10_000:
            QtWidgets.QMessageBox.warning(
                self, "Note Too Long", "Bookmark notes are limited to 10,000 characters."
            )
            return
        self._annotations[path] = Annotation(
            path=path,
            note=note,
            created_at=annotation.created_at,
            updated_at=utc_now(),
        )
        self._render_bookmarks()
        self._save_bookmarks()

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
        rename_action = menu.addAction("Rename Key (disabled for metadata safety)")
        delete_action = menu.addAction("Delete Key")
        menu.addSeparator()
        copy_action = menu.addAction("Copy Path")
        bookmark_action = menu.addAction("Add Bookmark" if path not in self._bookmarks else "Remove Bookmark")

        is_root = path == ""
        rename_action.setEnabled(False)
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
            name_item = self.values_table.item(index.row(), 0)
            if name_item is not None:
                value_name = name_item.data(VALUE_NAME_ROLE) or ""
                value = self._hive.get_value(self._current_path(), value_name)
                if value is not None:
                    QtWidgets.QApplication.clipboard().setText(format_value_data(value))

    def _create_key(self, parent_path: str) -> None:
        if self._hive is None:
            return
        if self._read_only:
            QtWidgets.QMessageBox.information(self, "Read-only", "Hive is read-only.")
            return
        while True:
            name, ok = QtWidgets.QInputDialog.getText(self, "New Key", "Key name")
            if not ok:
                return
            try:
                validate_key_name(name)
            except (TypeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, "Invalid Key Name", str(exc))
                continue
            break
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
        QtWidgets.QMessageBox.information(
            self,
            "Rename Key Disabled",
            "Key rename is disabled because the current backend cannot preserve timestamps, "
            "class data, and security metadata safely.",
        )

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
            self._hive.create_value(path, name, value_type, parsed)
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
        if value.type not in EDITABLE_VALUE_TYPES:
            QtWidgets.QMessageBox.information(
                self,
                "Unsupported Registry Type",
                f"{value.type_name} is preserved as raw bytes but cannot be edited safely.",
            )
            return
        data_text = format_value_edit_text(value)
        reg_type = RegistryType(value.type)
        dialog = ValueEditorDialog(self, "Edit Value", value.name, reg_type, data_text)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            name, value_type, parsed = dialog.get_value()
            self._hive.replace_value(path, value.name, name, value_type, parsed)
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
                if child.text().casefold() == part.casefold():
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
        if self._hive is None or self._hive_path is None:
            return
        query = self.search_input.text().strip()
        if not query:
            return
        mode = str(self.search_mode.currentData())
        if mode == "hex":
            try:
                parse_hex_pattern(query)
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "Invalid Hex Search", str(exc))
                return
        self._search_generation += 1
        request_id = self._search_generation
        if not self._stop_search(wait=True):
            self.search_status.setText("Previous search is still stopping")
            return
        try:
            snapshot_path = self._snapshot_path()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Search Failed", str(exc))
            return
        if snapshot_path is None:
            return
        if snapshot_path != self._hive_path:
            self._search_temp_paths[request_id] = snapshot_path
        self.search_results.clear()
        self.export_search_action.setEnabled(False)
        self._search_active = True
        self._search_last_count = 0
        self.search_cancel_button.setEnabled(True)
        self.search_status.setText("Searching...")
        self.statusBar().showMessage("Searching...")
        self._search_thread = QtCore.QThread(self)
        self._search_worker = SearchWorker(snapshot_path, query, request_id, mode=mode)
        self._search_worker.moveToThread(self._search_thread)
        self._search_thread.started.connect(self._search_worker.run)
        self._search_worker.results_batch.connect(self._on_search_batch)
        self._search_worker.completed.connect(self._on_search_completed)
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.error.connect(self._on_search_error)
        self._search_worker.finished.connect(self._search_thread.quit)
        self._search_worker.finished.connect(self._search_worker.deleteLater)
        self._search_thread.finished.connect(self._search_thread.deleteLater)
        self._search_worker.finished.connect(self._on_search_finished)
        self._search_thread.start()

    def _stop_search(self, *, wait: bool = False) -> bool:
        worker = self._search_worker
        thread = self._search_thread
        self._request_worker_stop(worker, thread)
        if wait and thread is not None and thread.isRunning() and not thread.wait(5000):
            return False
        if wait:
            self._search_worker = None
            self._search_thread = None
            if worker is not None:
                request_id = worker.request_id
                self._cleanup_temp_path(self._search_temp_paths.pop(request_id, None))
        return True

    def _on_search_finished(self, request_id: int) -> None:
        self._cleanup_temp_path(self._search_temp_paths.pop(request_id, None))
        if request_id != self._search_generation:
            return
        self._search_worker = None
        self._search_thread = None
        self._search_active = False
        self.search_cancel_button.setEnabled(False)

    def _on_search_progress(self, request_id: int, total: int, matched: int) -> None:
        if request_id != self._search_generation:
            return
        self.statusBar().showMessage(f"Searching... scanned {total} keys, {matched} matches")
        self.search_status.setText(f"Scanned {total} keys, {matched} matches")

    def _on_search_error(self, request_id: int, message: str) -> None:
        if request_id != self._search_generation:
            return
        QtWidgets.QMessageBox.critical(self, "Search Failed", message)
        self.search_status.setText("Search failed")
        self.statusBar().showMessage("Search failed")

    def _on_search_batch(self, request_id: int, results: list[SearchResult]) -> None:
        if request_id != self._search_generation:
            return
        self.search_results.setUpdatesEnabled(False)
        for result in results:
            if result.kind == "key":
                label = f"[Key] {result.path}"
            else:
                value_name = result.value_name or "(Default)"
                label = f"[Value] {result.path} \\ {value_name}"
                if result.match_offset is not None:
                    label += f" @ 0x{result.match_offset:08X}"
                if result.value_data:
                    label += f" = {result.value_data}"
                    if result.value_data_truncated:
                        label += " … [preview truncated]"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, result)
            self.search_results.addItem(item)
        self.search_results.setUpdatesEnabled(True)
        self._search_last_count += len(results)
        self.export_search_action.setEnabled(self._search_last_count > 0)

    def _on_search_completed(
        self,
        request_id: int,
        cancelled: bool,
        truncated: bool,
        total: int,
        matched: int,
    ) -> None:
        if request_id != self._search_generation:
            return
        if cancelled:
            self.search_status.setText(f"Cancelled ({matched} results)")
            self.statusBar().showMessage(f"Search cancelled: {matched} results")
        elif truncated:
            message = f"Result limit reached: {matched} matches in {total} keys; narrow the search"
            self.search_status.setText(message)
            self.statusBar().showMessage(message)
        else:
            self.search_status.setText(f"Results: {matched}")
            self.statusBar().showMessage(f"Search complete: {matched} results")

    def _cancel_search(self) -> None:
        if not self._search_active:
            return
        self._stop_search()
        self.search_status.setText("Cancelling...")
        self.statusBar().showMessage("Cancelling search...")
        self.search_cancel_button.setEnabled(False)

    def _on_search_mode_changed(self) -> None:
        if self.search_mode.currentData() == "hex":
            self.search_input.setPlaceholderText("Exact bytes, for example: DE AD BE EF")
        else:
            self.search_input.setPlaceholderText("Search keys and values...")

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

    def _render_bookmarks(self) -> None:
        self.bookmarks_list.clear()
        for path in self._bookmarks:
            annotation = self._annotations.get(path)
            item = QtWidgets.QListWidgetItem(path)
            if annotation is not None and annotation.note:
                item.setToolTip(annotation.note)
            self.bookmarks_list.addItem(item)

    def _save_bookmarks(self) -> None:
        if self._hive_sha256 is None:
            return
        try:
            self._annotation_store.save(
                self._hive_sha256,
                [self._annotations[path] for path in self._bookmarks],
                source_path=self._hive_path,
            )
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self,
                "Annotation Save Failed",
                f"Bookmarks and notes could not be saved atomically:\n\n{exc}",
            )

    def _add_bookmark(self, path: str) -> None:
        if self._hive_sha256 is None or not path or path in self._bookmarks:
            return
        timestamp = utc_now()
        self._annotations[path] = Annotation(
            path=path, created_at=timestamp, updated_at=timestamp
        )
        self._bookmarks.append(path)
        self._bookmarks.sort(key=str.casefold)
        self._render_bookmarks()
        self._save_bookmarks()

    def _remove_bookmark(self, path: str) -> None:
        if path not in self._bookmarks:
            return
        self._bookmarks.remove(path)
        self._annotations.pop(path, None)
        self._render_bookmarks()
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
            if item and str(item.data(VALUE_NAME_ROLE)).casefold() == value_name.casefold():
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


def _truncate_search_preview(text: str) -> tuple[str, bool]:
    if len(text) <= SEARCH_RESULT_PREVIEW_CHARS:
        return text, False
    return text[:SEARCH_RESULT_PREVIEW_CHARS], True


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return f"{text[:limit]} … [truncated]", True


def format_value_edit_text(value: HiveValue) -> str:
    if value.type == RegistryType.REG_MULTI_SZ:
        if isinstance(value.decoded, list):
            return "\n".join(value.decoded)
        return str(value.decoded)
    if value.type in (RegistryType.REG_BINARY, RegistryType.REG_NONE):
        return value.data.hex(" ")
    return str(value.decoded)


def parse_value_input(value_type: RegistryType, text: str) -> object:
    return parse_value_text(int(value_type), text)

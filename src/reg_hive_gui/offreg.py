"""ctypes adapter for Microsoft's native Windows Offline Registry Library."""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ERROR_SUCCESS = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_MORE_DATA = 234
ERROR_NO_MORE_ITEMS = 259

ORHKEY = ctypes.c_void_p
PORHKEY = ctypes.POINTER(ORHKEY)
PDWORD = ctypes.POINTER(wintypes.DWORD)
LPBYTE = ctypes.POINTER(wintypes.BYTE)
PFILETIME = ctypes.POINTER(wintypes.FILETIME)


@dataclass(frozen=True, slots=True)
class _Node:
    parts: tuple[str, ...]

    @property
    def path(self) -> str:
        return "\\".join(self.parts)


@dataclass(frozen=True, slots=True)
class _Value:
    name: str
    value_type: int
    data: bytes


@dataclass(frozen=True, slots=True)
class _KeyInfo:
    subkey_count: int
    max_subkey_name_length: int
    value_count: int
    max_value_name_length: int
    max_value_data_length: int
    timestamp: int


class _OffregApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("The native Offline Registry backend is available only on Windows")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        dll_path = system_root / "System32" / "offreg.dll"
        if not dll_path.is_file():
            raise FileNotFoundError(
                f"Windows Offline Registry Library was not found at {dll_path}. "
                "Install the Microsoft Offline Registry Library/WDK component."
            )
        self.dll_path = dll_path
        self.dll = ctypes.WinDLL(str(dll_path), use_last_error=True)
        self._configure_functions()

    def _configure_functions(self) -> None:
        self.dll.OROpenHive.argtypes = [wintypes.LPCWSTR, PORHKEY]
        self.dll.OROpenHive.restype = wintypes.DWORD
        self.dll.ORCreateHive.argtypes = [PORHKEY]
        self.dll.ORCreateHive.restype = wintypes.DWORD
        self.dll.ORCloseHive.argtypes = [ORHKEY]
        self.dll.ORCloseHive.restype = wintypes.DWORD
        self.dll.ORSaveHive.argtypes = [
            ORHKEY,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.dll.ORSaveHive.restype = wintypes.DWORD
        self.dll.OROpenKey.argtypes = [ORHKEY, wintypes.LPCWSTR, PORHKEY]
        self.dll.OROpenKey.restype = wintypes.DWORD
        self.dll.ORCloseKey.argtypes = [ORHKEY]
        self.dll.ORCloseKey.restype = wintypes.DWORD
        self.dll.ORCreateKey.argtypes = [
            ORHKEY,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            PORHKEY,
            PDWORD,
        ]
        self.dll.ORCreateKey.restype = wintypes.DWORD
        self.dll.ORDeleteKey.argtypes = [ORHKEY, wintypes.LPCWSTR]
        self.dll.ORDeleteKey.restype = wintypes.DWORD
        self.dll.ORQueryInfoKey.argtypes = [
            ORHKEY,
            wintypes.LPWSTR,
            PDWORD,
            PDWORD,
            PDWORD,
            PDWORD,
            PDWORD,
            PDWORD,
            PDWORD,
            PDWORD,
            PFILETIME,
        ]
        self.dll.ORQueryInfoKey.restype = wintypes.DWORD
        self.dll.OREnumKey.argtypes = [
            ORHKEY,
            wintypes.DWORD,
            wintypes.LPWSTR,
            PDWORD,
            wintypes.LPWSTR,
            PDWORD,
            PFILETIME,
        ]
        self.dll.OREnumKey.restype = wintypes.DWORD
        self.dll.OREnumValue.argtypes = [
            ORHKEY,
            wintypes.DWORD,
            wintypes.LPWSTR,
            PDWORD,
            PDWORD,
            LPBYTE,
            PDWORD,
        ]
        self.dll.OREnumValue.restype = wintypes.DWORD
        self.dll.ORSetValue.argtypes = [
            ORHKEY,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            LPBYTE,
            wintypes.DWORD,
        ]
        self.dll.ORSetValue.restype = wintypes.DWORD
        self.dll.ORDeleteValue.argtypes = [ORHKEY, wintypes.LPCWSTR]
        self.dll.ORDeleteValue.restype = wintypes.DWORD


@lru_cache(maxsize=1)
def _get_api() -> _OffregApi:
    return _OffregApi()


def native_backend_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        _get_api()
    except OSError:
        return False
    return True


def _error_message(code: int) -> str:
    try:
        return ctypes.FormatError(code).strip()
    except (AttributeError, OSError):
        return f"Windows error {code}"


def _check(code: int, operation: str) -> None:
    if code != ERROR_SUCCESS:
        raise OSError(code, f"{operation}: {_error_message(code)}")


class OffregHive:
    """Expose Offreg operations through the subset of the hivex interface the app uses."""

    backend_name = "Windows Offline Registry Library"
    save_os_version: tuple[int, int] | None = None

    def __init__(self, path: Path | str, *, write: bool = False) -> None:
        del write  # Offreg always edits an in-memory copy; Hive enforces write permissions.
        self._api = _get_api()
        self._root = ORHKEY()
        self._closed = False
        hive_path = str(Path(path).resolve())
        _check(
            self._api.dll.OROpenHive(hive_path, ctypes.byref(self._root)),
            f"Open registry hive {hive_path}",
        )

    @classmethod
    def create(cls) -> OffregHive:
        instance = cls.__new__(cls)
        instance._api = _get_api()
        instance._root = ORHKEY()
        instance._closed = False
        _check(
            instance._api.dll.ORCreateHive(ctypes.byref(instance._root)),
            "Create registry hive",
        )
        return instance

    def root(self) -> _Node:
        self._ensure_open()
        return _Node(())

    def _ensure_open(self) -> None:
        if self._closed or not self._root:
            raise OSError("Registry hive is closed")

    @contextmanager
    def _open_node(self, node: _Node) -> Iterator[ORHKEY]:
        self._ensure_open()
        if not node.parts:
            yield self._root
            return
        handle = ORHKEY()
        _check(
            self._api.dll.OROpenKey(self._root, node.path, ctypes.byref(handle)),
            f"Open registry key {node.path}",
        )
        try:
            yield handle
        finally:
            _check(self._api.dll.ORCloseKey(handle), f"Close registry key {node.path}")

    def _query_info(self, node: _Node) -> _KeyInfo:
        subkeys = wintypes.DWORD()
        max_subkey = wintypes.DWORD()
        values = wintypes.DWORD()
        max_value_name = wintypes.DWORD()
        max_value_data = wintypes.DWORD()
        timestamp = wintypes.FILETIME()
        with self._open_node(node) as handle:
            _check(
                self._api.dll.ORQueryInfoKey(
                    handle,
                    None,
                    None,
                    ctypes.byref(subkeys),
                    ctypes.byref(max_subkey),
                    None,
                    ctypes.byref(values),
                    ctypes.byref(max_value_name),
                    ctypes.byref(max_value_data),
                    None,
                    ctypes.byref(timestamp),
                ),
                f"Query registry key {node.path or 'ROOT'}",
            )
        raw_timestamp = (int(timestamp.dwHighDateTime) << 32) | int(timestamp.dwLowDateTime)
        return _KeyInfo(
            subkey_count=int(subkeys.value),
            max_subkey_name_length=int(max_subkey.value),
            value_count=int(values.value),
            max_value_name_length=int(max_value_name.value),
            max_value_data_length=int(max_value_data.value),
            timestamp=raw_timestamp,
        )

    def node_get_child(self, node: _Node, name: str) -> _Node | None:
        child = _Node((*node.parts, name))
        try:
            with self._open_node(child):
                pass
        except OSError as exc:
            if exc.errno in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
                return None
            raise
        return child

    def node_children(self, node: _Node) -> list[_Node]:
        info = self._query_info(node)
        children: list[_Node] = []
        with self._open_node(node) as handle:
            for index in range(info.subkey_count):
                capacity = max(info.max_subkey_name_length + 1, 1)
                while True:
                    name = ctypes.create_unicode_buffer(capacity)
                    name_length = wintypes.DWORD(capacity)
                    code = int(
                        self._api.dll.OREnumKey(
                            handle,
                            index,
                            name,
                            ctypes.byref(name_length),
                            None,
                            None,
                            None,
                        )
                    )
                    if code == ERROR_MORE_DATA:
                        capacity = max(capacity * 2, int(name_length.value) + 1)
                        continue
                    if code == ERROR_NO_MORE_ITEMS:
                        return children
                    _check(code, f"Enumerate subkeys of {node.path or 'ROOT'}")
                    children.append(_Node((*node.parts, name.value)))
                    break
        return children

    def node_nr_children(self, node: _Node) -> int:
        return self._query_info(node).subkey_count

    def node_name(self, node: _Node) -> str:
        return node.parts[-1] if node.parts else "ROOT"

    def node_timestamp(self, node: _Node) -> int:
        return self._query_info(node).timestamp

    def node_values(self, node: _Node) -> list[_Value]:
        info = self._query_info(node)
        values: list[_Value] = []
        with self._open_node(node) as handle:
            for index in range(info.value_count):
                name_capacity = max(info.max_value_name_length + 1, 1)
                data_capacity = max(info.max_value_data_length, 1)
                while True:
                    name = ctypes.create_unicode_buffer(name_capacity)
                    name_length = wintypes.DWORD(name_capacity)
                    value_type = wintypes.DWORD()
                    data = ctypes.create_string_buffer(data_capacity)
                    data_length = wintypes.DWORD(data_capacity)
                    code = int(
                        self._api.dll.OREnumValue(
                            handle,
                            index,
                            name,
                            ctypes.byref(name_length),
                            ctypes.byref(value_type),
                            ctypes.cast(data, LPBYTE),
                            ctypes.byref(data_length),
                        )
                    )
                    if code == ERROR_MORE_DATA:
                        name_capacity = max(name_capacity * 2, int(name_length.value) + 1)
                        data_capacity = max(data_capacity * 2, int(data_length.value), 1)
                        continue
                    if code == ERROR_NO_MORE_ITEMS:
                        return values
                    _check(code, f"Enumerate values of {node.path or 'ROOT'}")
                    values.append(
                        _Value(
                            name=name.value,
                            value_type=int(value_type.value),
                            data=bytes(data.raw[: data_length.value]),
                        )
                    )
                    break
        return values

    node_get_values = node_values

    @staticmethod
    def value_key(value: _Value) -> str:
        return value.name

    @staticmethod
    def value_type(value: _Value) -> int:
        return value.value_type

    @staticmethod
    def value_value(value: _Value) -> bytes:
        return value.data

    def node_add_child(self, node: _Node, name: str) -> _Node:
        result = ORHKEY()
        disposition = wintypes.DWORD()
        with self._open_node(node) as handle:
            _check(
                self._api.dll.ORCreateKey(
                    handle,
                    name,
                    None,
                    0,
                    None,
                    ctypes.byref(result),
                    ctypes.byref(disposition),
                ),
                f"Create registry key {name}",
            )
        try:
            return _Node((*node.parts, name))
        finally:
            if result:
                _check(self._api.dll.ORCloseKey(result), f"Close registry key {name}")

    def _set_value(self, node: _Node, name: str, value_type: int, data: bytes) -> None:
        buffer = None
        pointer = None
        if data:
            buffer = (wintypes.BYTE * len(data)).from_buffer_copy(data)
            pointer = ctypes.cast(buffer, LPBYTE)
        with self._open_node(node) as handle:
            _check(
                self._api.dll.ORSetValue(
                    handle,
                    name or None,
                    int(value_type),
                    pointer,
                    len(data),
                ),
                f"Set registry value {name or '(Default)'}",
            )

    def node_set_value(self, node: _Node, payload: dict[str, object]) -> None:
        name = payload.get("key")
        value_type = payload.get("t")
        data = payload.get("value")
        if not isinstance(name, str) or not isinstance(value_type, int):
            raise TypeError("Invalid registry value payload")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Registry value data must be bytes")
        self._set_value(node, name, value_type, bytes(data))

    def _delete_value(self, node: _Node, name: str) -> None:
        with self._open_node(node) as handle:
            _check(
                self._api.dll.ORDeleteValue(handle, name or None),
                f"Delete registry value {name or '(Default)'}",
            )

    def _replace_values(self, node: _Node, payloads: Sequence[dict[str, object]]) -> None:
        for value in self.node_values(node):
            self._delete_value(node, value.name)
        for payload in payloads:
            self.node_set_value(node, payload)

    def node_set_values(self, node: _Node, payloads: Sequence[dict[str, object]]) -> None:
        original = [
            {"key": value.name, "t": value.value_type, "value": value.data}
            for value in self.node_values(node)
        ]
        try:
            self._replace_values(node, payloads)
        except Exception:
            try:
                self._replace_values(node, original)
            except Exception:
                pass
            raise

    def _target_os_version(self) -> tuple[int, int]:
        if self.save_os_version is not None:
            return self.save_os_version
        windows_version = sys.getwindowsversion()
        return windows_version.major, windows_version.minor

    def _save_to_new_file(self, output: Path) -> None:
        if output.exists():
            raise FileExistsError(f"Offline Registry save target already exists: {output}")
        major, minor = self._target_os_version()
        _check(
            self._api.dll.ORSaveHive(self._root, str(output.resolve()), major, minor),
            f"Save registry hive {output}",
        )

    def _rollback_snapshot(self) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix="reg_hive_rollback_", suffix=".hive")
        os.close(descriptor)
        snapshot = Path(raw_path)
        snapshot.unlink()
        try:
            self._save_to_new_file(snapshot)
        except Exception:
            try:
                snapshot.unlink()
            except FileNotFoundError:
                pass
            raise
        return snapshot

    def _restore_snapshot(self, snapshot: Path) -> None:
        restored_root = ORHKEY()
        _check(
            self._api.dll.OROpenHive(str(snapshot.resolve()), ctypes.byref(restored_root)),
            "Open registry rollback snapshot",
        )
        close_code = int(self._api.dll.ORCloseHive(self._root))
        if close_code != ERROR_SUCCESS:
            self._api.dll.ORCloseHive(restored_root)
            _check(close_code, "Close failed registry working copy")
        self._root = restored_root

    def _delete_key_leaf(self, node: _Node) -> None:
        parent = _Node(node.parts[:-1])
        with self._open_node(parent) as handle:
            _check(
                self._api.dll.ORDeleteKey(handle, node.parts[-1]),
                f"Delete registry key {node.path}",
            )

    def node_delete_child(self, node: _Node) -> None:
        if not node.parts:
            raise ValueError("Cannot delete the hive root")
        snapshot = self._rollback_snapshot()
        postorder: list[_Node] = []
        try:
            stack: list[tuple[_Node, bool]] = [(node, False)]
            while stack:
                current, visited = stack.pop()
                if visited:
                    postorder.append(current)
                    continue
                stack.append((current, True))
                for child in self.node_children(current):
                    stack.append((child, False))
            for current in postorder:
                self._delete_key_leaf(current)
        except Exception:
            try:
                self._restore_snapshot(snapshot)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Registry key deletion failed and the working-copy rollback also failed"
                ) from rollback_error
            raise
        finally:
            try:
                snapshot.unlink()
            except FileNotFoundError:
                pass

    def commit(self, output_path: Path | str) -> None:
        self._ensure_open()
        output = Path(output_path)
        self._save_to_new_file(output)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        root = self._root
        self._root = ORHKEY()
        if root:
            _check(self._api.dll.ORCloseHive(root), "Close registry hive")

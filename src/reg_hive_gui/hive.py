"""Core hive parsing/editing utilities backed by hivex."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Iterator, Sequence

from ._hivex import hivex


class RegistryType(IntEnum):
    REG_NONE = 0
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_BINARY = 3
    REG_DWORD = 4
    REG_DWORD_BIG_ENDIAN = 5
    REG_LINK = 6
    REG_MULTI_SZ = 7
    REG_RESOURCE_LIST = 8
    REG_FULL_RESOURCE_DESCRIPTOR = 9
    REG_RESOURCE_REQUIREMENTS_LIST = 10
    REG_QWORD = 11

    @classmethod
    def from_value(cls, value: int) -> RegistryType | None:
        try:
            return cls(int(value))
        except ValueError:
            return None


def _split_path(path: str | None) -> list[str]:
    if not path:
        return []
    normalized = path.replace("/", "\\").strip("\\")
    if not normalized:
        return []
    return [part for part in normalized.split("\\") if part]


_KNOWN_TYPES = {item.value for item in RegistryType}


def _normalize_value_type(raw: object) -> int:
    if isinstance(raw, tuple) and raw:
        first = raw[0]
        if isinstance(first, int) and first in _KNOWN_TYPES:
            return int(first)
        if len(raw) > 1 and isinstance(raw[1], int) and raw[1] in _KNOWN_TYPES:
            return int(raw[1])
        return int(first)
    return int(raw)


def _normalize_value_data(raw: object) -> bytes:
    if isinstance(raw, tuple):
        for item in reversed(raw):
            if isinstance(item, (bytes, bytearray)):
                return bytes(item)
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, bytes):
        return raw
    raise TypeError(f"Unexpected value payload type: {type(raw)!r}")


def decode_value(value_type: int, data: bytes) -> object:
    if value_type in (RegistryType.REG_SZ, RegistryType.REG_EXPAND_SZ):
        text = data.decode("utf-16le", errors="replace")
        return text.rstrip("\x00")
    if value_type == RegistryType.REG_MULTI_SZ:
        text = data.decode("utf-16le", errors="replace")
        return [item for item in text.rstrip("\x00").split("\x00") if item]
    if value_type == RegistryType.REG_DWORD and len(data) >= 4:
        return int.from_bytes(data[:4], "little", signed=False)
    if value_type == RegistryType.REG_QWORD and len(data) >= 8:
        return int.from_bytes(data[:8], "little", signed=False)
    return data


def encode_value(value_type: int, value: object) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if value_type in (RegistryType.REG_SZ, RegistryType.REG_EXPAND_SZ):
        return (str(value) + "\x00").encode("utf-16le")
    if value_type == RegistryType.REG_MULTI_SZ:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            joined = "\x00".join(str(item) for item in value)
        else:
            joined = str(value)
        return (joined + "\x00\x00").encode("utf-16le")
    if value_type == RegistryType.REG_DWORD:
        return int(value).to_bytes(4, "little", signed=False)
    if value_type == RegistryType.REG_QWORD:
        return int(value).to_bytes(8, "little", signed=False)
    return bytes(value)


def _type_name(value_type: int) -> str:
    known = RegistryType.from_value(value_type)
    return known.name if known else f"REG_UNKNOWN_{value_type}"


def filetime_to_datetime(filetime: int) -> datetime | None:
    if not filetime:
        return None
    # FILETIME: 100-ns intervals since 1601-01-01 UTC
    unix_epoch = 116444736000000000
    try:
        unix_time = (int(filetime) - unix_epoch) / 10_000_000
    except (OverflowError, ValueError):
        return None
    return datetime.fromtimestamp(unix_time, tz=timezone.utc)


@dataclass(frozen=True)
class HiveValue:
    name: str
    type: int
    data: bytes
    decoded: object

    @property
    def type_name(self) -> str:
        return _type_name(self.type)


class Hive:
    def __init__(self, path: Path | str, *, write: bool = True) -> None:
        self._path = Path(path)
        self._handle = hivex.Hivex(str(self._path), write=write)
        self._write = write

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        if hasattr(self._handle, "close"):
            self._handle.close()

    def __enter__(self) -> "Hive":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _node_values(self, node: int) -> list[int]:
        if hasattr(self._handle, "node_values"):
            return list(self._handle.node_values(node))
        if hasattr(self._handle, "node_get_values"):
            return list(self._handle.node_get_values(node))
        return []

    def _get_child(self, node: int, name: str) -> int | None:
        child = self._handle.node_get_child(node, name)
        return child or None

    def get_node(self, path: str | None) -> int | None:
        node = self._handle.root()
        for part in _split_path(path):
            child = self._get_child(node, part)
            if child is None:
                return None
            node = child
        return node

    def ensure_path(self, path: str) -> int:
        node = self._handle.root()
        for part in _split_path(path):
            child = self._get_child(node, part)
            if child is None:
                child = self._handle.node_add_child(node, part)
            node = child
        return node

    def list_subkeys(self, path: str | None = None) -> list[str]:
        node = self.get_node(path)
        if node is None:
            return []
        return [self._handle.node_name(child) for child in self._handle.node_children(node)]

    def iter_key_nodes(self, start_path: str | None = None) -> Iterator[tuple[str, int]]:
        start_node = self.get_node(start_path)
        if start_node is None:
            return
        normalized = "" if not start_path else "\\".join(_split_path(start_path))
        stack: list[tuple[str, int]] = [(normalized, start_node)]
        while stack:
            path, node = stack.pop()
            yield path, node
            for child in self._handle.node_children(node):
                name = self._handle.node_name(child)
                child_path = f"{path}\\{name}" if path else name
                stack.append((child_path, child))

    def iter_keys(self, start_path: str | None = None) -> Iterator[str]:
        for path, _node in self.iter_key_nodes(start_path):
            yield path

    def iter_values(self, path: str | None = None) -> Iterator[HiveValue]:
        node = self.get_node(path)
        if node is None:
            return
        yield from self.iter_values_for_node(node)

    def iter_values_for_node(self, node: int) -> Iterator[HiveValue]:
        for value in self._node_values(node):
            name = self._handle.value_key(value)
            value_type = _normalize_value_type(self._handle.value_type(value))
            data_raw = _normalize_value_data(self._handle.value_value(value))
            decoded = decode_value(value_type, data_raw)
            yield HiveValue(name=name, type=value_type, data=data_raw, decoded=decoded)

    def get_node_timestamp(self, node: int) -> datetime | None:
        try:
            timestamp = self._handle.node_timestamp(node)
        except Exception:
            return None
        return filetime_to_datetime(timestamp)

    def get_key_timestamp(self, path: str) -> datetime | None:
        node = self.get_node(path)
        if node is None:
            return None
        return self.get_node_timestamp(node)

    def list_values(self, path: str | None = None) -> list[HiveValue]:
        node = self.get_node(path)
        if node is None:
            return []
        values: list[HiveValue] = []
        for value in self._node_values(node):
            name = self._handle.value_key(value)
            value_type = _normalize_value_type(self._handle.value_type(value))
            data_raw = _normalize_value_data(self._handle.value_value(value))
            decoded = decode_value(value_type, data_raw)
            values.append(HiveValue(name=name, type=value_type, data=data_raw, decoded=decoded))
        return values

    def get_value(self, path: str, name: str) -> HiveValue | None:
        node = self.get_node(path)
        if node is None:
            return None
        value = None
        if hasattr(self._handle, "node_get_value"):
            value = self._handle.node_get_value(node, name)
        if value is None:
            for candidate in self._node_values(node):
                if self._handle.value_key(candidate) == name:
                    value = candidate
                    break
        if value is None:
            return None
        value_type = _normalize_value_type(self._handle.value_type(value))
        data_raw = _normalize_value_data(self._handle.value_value(value))
        decoded = decode_value(value_type, data_raw)
        return HiveValue(name=name, type=value_type, data=data_raw, decoded=decoded)

    def set_value(self, path: str, name: str, value_type: int, value: object) -> None:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        node = self.ensure_path(path)
        payload = {
            "key": name,
            "t": int(value_type),
            "value": encode_value(value_type, value),
        }
        self._handle.node_set_value(node, payload)

    def delete_value(self, path: str, name: str) -> bool:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        node = self.get_node(path)
        if node is None:
            return False
        values = []
        removed = False
        for value in self._node_values(node):
            value_name = self._handle.value_key(value)
            value_type = _normalize_value_type(self._handle.value_type(value))
            data_raw = _normalize_value_data(self._handle.value_value(value))
            if value_name == name:
                removed = True
                continue
            values.append({"key": value_name, "t": value_type, "value": data_raw})
        if removed:
            self._handle.node_set_values(node, values)
        return removed

    def create_key(self, path: str) -> int:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        return self.ensure_path(path)

    def delete_key(self, path: str) -> bool:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        parts = _split_path(path)
        if not parts:
            raise ValueError("Cannot delete the hive root")
        parent_path = "\\".join(parts[:-1])
        parent = self.get_node(parent_path)
        if parent is None:
            return False
        child = self._get_child(parent, parts[-1])
        if child is None:
            return False
        self._handle.node_delete_child(child)
        return True

    def rename_key(self, path: str, new_name: str) -> bool:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        parts = _split_path(path)
        if not parts:
            raise ValueError("Cannot rename the hive root")
        parent_path = "\\".join(parts[:-1])
        parent = self.get_node(parent_path)
        if parent is None:
            return False
        node = self._get_child(parent, parts[-1])
        if node is None:
            return False
        new_node = self._copy_subtree(node, parent, new_name)
        self._handle.node_delete_child(node)
        return new_node is not None

    def _copy_subtree(self, node: int, parent: int, name: str) -> int:
        new_node = self._handle.node_add_child(parent, name)
        values = []
        for value in self._node_values(node):
            value_name = self._handle.value_key(value)
            value_type = _normalize_value_type(self._handle.value_type(value))
            data_raw = _normalize_value_data(self._handle.value_value(value))
            values.append({"key": value_name, "t": value_type, "value": data_raw})
        if values:
            self._handle.node_set_values(new_node, values)
        for child in self._handle.node_children(node):
            child_name = self._handle.node_name(child)
            self._copy_subtree(child, new_node, child_name)
        return new_node

    def export(self, output_path: Path | str) -> Path:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        output = Path(output_path)
        if output.resolve() == self._path.resolve():
            raise ValueError("Refusing to overwrite the input hive")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        self._handle.commit(str(output))
        return output

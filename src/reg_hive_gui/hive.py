"""Core hive parsing/editing utilities backed by hivex."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Iterator

from ._backend import load_backend
from .fileio import staged_output
from .validation import (
    validate_key_name,
    validate_multi_string,
    validate_unsigned_integer,
    validate_value_name,
)


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
    if value_type == RegistryType.REG_DWORD_BIG_ENDIAN and len(data) >= 4:
        return int.from_bytes(data[:4], "big", signed=False)
    if value_type == RegistryType.REG_QWORD and len(data) >= 8:
        return int.from_bytes(data[:8], "little", signed=False)
    return data


def encode_value(value_type: int, value: object) -> bytes:
    value_type = int(value_type)
    if value_type in (RegistryType.REG_SZ, RegistryType.REG_EXPAND_SZ):
        if not isinstance(value, str):
            raise TypeError("String registry values require text")
        if "\x00" in value:
            raise ValueError("String registry values cannot contain NUL characters")
        return (value + "\x00").encode("utf-16le")
    if value_type == RegistryType.REG_MULTI_SZ:
        joined = "\x00".join(validate_multi_string(value))
        return (joined + "\x00\x00").encode("utf-16le")
    if value_type == RegistryType.REG_DWORD:
        return validate_unsigned_integer(value, 32).to_bytes(4, "little", signed=False)
    if value_type == RegistryType.REG_QWORD:
        return validate_unsigned_integer(value, 64).to_bytes(8, "little", signed=False)
    if not isinstance(value, (bytes, bytearray)):
        raise TypeError("Opaque and binary registry values require raw bytes")
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
        return datetime.fromtimestamp(unix_time, tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class HiveValue:
    name: str
    type: int
    data: bytes
    decoded: object

    @property
    def type_name(self) -> str:
        return _type_name(self.type)


@dataclass(frozen=True)
class HiveTimestamp:
    raw: int | None
    value: datetime | None

    @property
    def display(self) -> str:
        if self.value is not None:
            return self.value.isoformat()
        if self.raw:
            return f"Invalid FILETIME ({self.raw})"
        return ""


class Hive:
    supports_key_rename = False

    def __init__(self, path: Path | str, *, write: bool = False) -> None:
        self._path = Path(path)
        backend = load_backend()
        self._handle = backend.Hivex(str(self._path), write=write)
        self._backend_name = getattr(self._handle, "backend_name", "hivex")
        self._write = write

    @property
    def path(self) -> Path:
        return self._path

    @property
    def backend_name(self) -> str:
        return self._backend_name

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

    def _raw_values(self, node: int) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for value in self._node_values(node):
            values.append(
                {
                    "key": self._handle.value_key(value),
                    "t": _normalize_value_type(self._handle.value_type(value)),
                    "value": _normalize_value_data(self._handle.value_value(value)),
                }
            )
        return values

    def _find_value_handle(self, node: int, name: str) -> int | None:
        wanted = name.casefold()
        for candidate in self._node_values(node):
            if self._handle.value_key(candidate).casefold() == wanted:
                return candidate
        return None

    def _get_child(self, node: int, name: str) -> int | None:
        child = self._handle.node_get_child(node, name)
        if child:
            return child
        wanted = name.casefold()
        for candidate in self._handle.node_children(node):
            if self._handle.node_name(candidate).casefold() == wanted:
                return candidate
        return None

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

    def iter_subkeys_for_node(self, node: int) -> Iterator[tuple[str, int, bool]]:
        """Yield child metadata without repeatedly resolving the parent path."""
        for child in self._handle.node_children(node):
            name = self._handle.node_name(child)
            if hasattr(self._handle, "node_nr_children"):
                has_children = bool(self._handle.node_nr_children(child))
            else:
                has_children = bool(self._handle.node_children(child))
            yield name, child, has_children

    def has_subkeys(self, path: str | None = None) -> bool:
        node = self.get_node(path)
        if node is None:
            return False
        if hasattr(self._handle, "node_nr_children"):
            return bool(self._handle.node_nr_children(node))
        return bool(self._handle.node_children(node))

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

    def get_node_timestamp_info(self, node: int) -> HiveTimestamp:
        try:
            raw = int(self._handle.node_timestamp(node))
        except Exception:
            return HiveTimestamp(raw=None, value=None)
        return HiveTimestamp(raw=raw, value=filetime_to_datetime(raw))

    def get_node_timestamp(self, node: int) -> datetime | None:
        return self.get_node_timestamp_info(node).value

    def get_key_timestamp(self, path: str) -> datetime | None:
        node = self.get_node(path)
        if node is None:
            return None
        return self.get_node_timestamp(node)

    def get_key_timestamp_info(self, path: str) -> HiveTimestamp:
        node = self.get_node(path)
        if node is None:
            return HiveTimestamp(raw=None, value=None)
        return self.get_node_timestamp_info(node)

    def list_values(self, path: str | None = None) -> list[HiveValue]:
        return list(self.iter_values(path))

    def get_value(self, path: str, name: str) -> HiveValue | None:
        node = self.get_node(path)
        if node is None:
            return None
        value = self._find_value_handle(node, name)
        if value is None:
            return None
        actual_name = self._handle.value_key(value)
        value_type = _normalize_value_type(self._handle.value_type(value))
        data_raw = _normalize_value_data(self._handle.value_value(value))
        decoded = decode_value(value_type, data_raw)
        return HiveValue(name=actual_name, type=value_type, data=data_raw, decoded=decoded)

    def value_name_exists(self, path: str, name: str) -> bool:
        node = self.get_node(path)
        return node is not None and self._find_value_handle(node, name) is not None

    def set_value(self, path: str, name: str, value_type: int, value: object) -> None:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        validate_value_name(name)
        self.set_raw_value(path, name, value_type, encode_value(value_type, value))

    def set_raw_value(self, path: str, name: str, value_type: int, data: bytes) -> None:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        validate_value_name(name)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Raw registry value data must be bytes")
        node = self.ensure_path(path)
        payload = {
            "key": name,
            "t": int(value_type),
            "value": bytes(data),
        }
        self._handle.node_set_value(node, payload)

    def create_value(self, path: str, name: str, value_type: int, value: object) -> None:
        if self.value_name_exists(path, name):
            raise ValueError(f"A value named {name or '(Default)'} already exists")
        self.set_value(path, name, value_type, value)

    def replace_value(
        self,
        path: str,
        old_name: str,
        new_name: str,
        value_type: int,
        value: object,
    ) -> None:
        """Replace or rename a value with verification and best-effort rollback."""
        if not self._write:
            raise PermissionError("Hive opened read-only")
        validate_value_name(new_name)
        original = self.get_value(path, old_name)
        if original is None:
            raise KeyError(f"Registry value not found: {old_name or '(Default)'}")
        if original.name.casefold() == new_name.casefold() and original.name != new_name:
            raise ValueError("Case-only value renames are not supported safely")
        existing = self.get_value(path, new_name)
        if existing is not None and existing.name.casefold() != original.name.casefold():
            raise ValueError(f"A value named {new_name or '(Default)'} already exists")

        new_data = encode_value(value_type, value)
        try:
            self.set_raw_value(path, new_name, value_type, new_data)
            written = self.get_value(path, new_name)
            if written is None or written.type != int(value_type) or written.data != new_data:
                raise OSError("Registry backend did not verify the replacement value")
            if new_name != original.name and not self.delete_value(path, original.name):
                raise OSError("Original value could not be removed after replacement")
        except Exception:
            try:
                if new_name.casefold() != original.name.casefold():
                    self.delete_value(path, new_name)
                self.set_raw_value(path, original.name, original.type, original.data)
            except Exception:
                pass
            raise

    def delete_value(self, path: str, name: str) -> bool:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        node = self.get_node(path)
        if node is None:
            return False
        original_values = self._raw_values(node)
        values = []
        removed = False
        wanted = name.casefold()
        for value in original_values:
            if str(value["key"]).casefold() == wanted:
                removed = True
                continue
            values.append(value)
        if removed:
            try:
                self._handle.node_set_values(node, values)
            except Exception:
                try:
                    self._handle.node_set_values(node, original_values)
                except Exception:
                    pass
                raise
        return removed

    def create_key(self, path: str) -> int:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        parts = _split_path(path)
        if not parts:
            raise ValueError("Cannot create the hive root")
        for part in parts:
            validate_key_name(part)
        if self.get_node(path) is not None:
            raise ValueError(f"A key named {parts[-1]} already exists")
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
        raise NotImplementedError(
            "Key rename is disabled because the current backend cannot preserve all key metadata"
        )

    def export(self, output_path: Path | str) -> Path:
        if not self._write:
            raise PermissionError("Hive opened read-only")
        output = Path(output_path)
        with staged_output(output, source_path=self._path, precreate=False) as temporary:
            self._handle.commit(str(temporary))
            try:
                signature = temporary.read_bytes()[:4]
            except OSError as exc:
                raise OSError("Registry backend did not produce a readable hive") from exc
            if signature != b"regf":
                raise OSError("Registry backend produced an invalid hive signature")
        return output

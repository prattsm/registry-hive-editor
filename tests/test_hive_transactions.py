from __future__ import annotations

from pathlib import Path

import pytest

from reg_hive_gui.hive import Hive, RegistryType, encode_value


class FakeHandle:
    def __init__(self) -> None:
        self._next_value = 10
        self.values: dict[int, dict[str, object]] = {}
        self.fail_next_set_values = False
        self.corrupt_next_write = False

    def root(self) -> int:
        return 1

    def node_get_child(self, _node: int, _name: str) -> int:
        return 0

    def node_values(self, _node: int) -> list[int]:
        return list(self.values)

    def value_key(self, value: int) -> str:
        return str(self.values[value]["key"])

    def value_type(self, value: int) -> int:
        return int(self.values[value]["t"])

    def value_value(self, value: int) -> bytes:
        return bytes(self.values[value]["value"])

    def node_set_value(self, _node: int, payload: dict[str, object]) -> None:
        name = str(payload["key"])
        existing = next(
            (item for item in self.values if self.value_key(item).casefold() == name.casefold()),
            None,
        )
        handle = existing if existing is not None else self._next_value
        if existing is None:
            self._next_value += 1
        stored = dict(payload)
        if self.corrupt_next_write:
            self.corrupt_next_write = False
            stored["value"] = b"corrupt"
        self.values[handle] = stored

    def node_set_values(self, _node: int, payloads: list[dict[str, object]]) -> None:
        if self.fail_next_set_values:
            self.fail_next_set_values = False
            raise OSError("simulated value-list failure")
        self.values.clear()
        for payload in payloads:
            self.node_set_value(1, payload)

    def add_value(self, name: str, value_type: int, data: bytes) -> None:
        self.node_set_value(1, {"key": name, "t": value_type, "value": data})


def make_hive(*, write: bool = True) -> tuple[Hive, FakeHandle]:
    handle = FakeHandle()
    hive = Hive.__new__(Hive)
    hive._path = Path("fake-hive")
    hive._write = write
    hive._handle = handle
    return hive, handle


def test_value_lookup_and_collision_checks_are_case_insensitive() -> None:
    hive, handle = make_hive()
    handle.add_value("MixedCase", RegistryType.REG_SZ, encode_value(RegistryType.REG_SZ, "old"))

    assert hive.get_value("", "mixedcase").name == "MixedCase"
    with pytest.raises(ValueError, match="already exists"):
        hive.create_value("", "MIXEDCASE", RegistryType.REG_SZ, "new")


def test_value_rename_writes_verifies_then_removes_original() -> None:
    hive, handle = make_hive()
    handle.add_value("Old", RegistryType.REG_SZ, encode_value(RegistryType.REG_SZ, "before"))

    hive.replace_value("", "old", "New", RegistryType.REG_DWORD, 42)

    assert hive.get_value("", "Old") is None
    replacement = hive.get_value("", "new")
    assert replacement is not None
    assert replacement.decoded == 42


def test_case_only_value_rename_is_rejected_without_mutation() -> None:
    hive, handle = make_hive()
    original = encode_value(RegistryType.REG_SZ, "before")
    handle.add_value("Original", RegistryType.REG_SZ, original)

    with pytest.raises(ValueError, match="Case-only"):
        hive.replace_value("", "original", "ORIGINAL", RegistryType.REG_SZ, "after")

    assert hive.get_value("", "Original").data == original


def test_failed_remove_during_rename_rolls_back_original_and_new_value() -> None:
    hive, handle = make_hive()
    original = encode_value(RegistryType.REG_SZ, "before")
    handle.add_value("Old", RegistryType.REG_SZ, original)
    handle.fail_next_set_values = True

    with pytest.raises(OSError, match="simulated"):
        hive.replace_value("", "Old", "New", RegistryType.REG_SZ, "after")

    restored = hive.get_value("", "Old")
    assert restored is not None and restored.data == original
    assert hive.get_value("", "New") is None


def test_backend_corruption_is_detected_and_original_is_restored() -> None:
    hive, handle = make_hive()
    original = encode_value(RegistryType.REG_SZ, "before")
    handle.add_value("Stable", RegistryType.REG_SZ, original)
    handle.corrupt_next_write = True

    with pytest.raises(OSError, match="did not verify"):
        hive.replace_value("", "Stable", "Stable", RegistryType.REG_SZ, "after")

    restored = hive.get_value("", "Stable")
    assert restored is not None and restored.data == original


def test_failed_delete_restores_complete_value_list() -> None:
    hive, handle = make_hive()
    handle.add_value("Keep", RegistryType.REG_DWORD, encode_value(RegistryType.REG_DWORD, 1))
    handle.add_value("Delete", RegistryType.REG_DWORD, encode_value(RegistryType.REG_DWORD, 2))
    handle.fail_next_set_values = True

    with pytest.raises(OSError, match="simulated"):
        hive.delete_value("", "delete")

    assert hive.get_value("", "Keep") is not None
    assert hive.get_value("", "Delete") is not None


def test_mutations_are_denied_in_read_only_mode() -> None:
    hive, _handle = make_hive(write=False)
    with pytest.raises(PermissionError):
        hive.set_value("", "Test", RegistryType.REG_SZ, "data")
    with pytest.raises(PermissionError):
        hive.rename_key("Anything", "Else")


def test_unsafe_key_rename_is_explicitly_disabled() -> None:
    hive, _handle = make_hive()
    with pytest.raises(NotImplementedError, match="cannot preserve"):
        hive.rename_key("Anything", "Else")


class CaseInsensitiveTreeHandle:
    def __init__(self) -> None:
        self.added = False

    def root(self) -> int:
        return 1

    def node_get_child(self, _node: int, name: str) -> int:
        return 2 if name == "MixedCase" else 0

    def node_children(self, node: int) -> list[int]:
        return [2] if node == 1 else []

    def node_name(self, node: int) -> str:
        assert node == 2
        return "MixedCase"

    def node_add_child(self, _node: int, _name: str) -> int:
        self.added = True
        return 3


def test_key_lookup_and_create_collisions_are_case_insensitive() -> None:
    hive = Hive.__new__(Hive)
    hive._path = Path("fake-hive")
    hive._write = True
    hive._handle = CaseInsensitiveTreeHandle()

    assert hive.get_node("mixedcase") == 2
    with pytest.raises(ValueError, match="already exists"):
        hive.create_key("MIXEDCASE")
    assert not hive._handle.added

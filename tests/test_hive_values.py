from datetime import datetime, timezone

import pytest

from reg_hive_gui.hive import (
    HiveTimestamp,
    RegistryType,
    decode_value,
    encode_value,
    filetime_to_datetime,
)


@pytest.mark.parametrize(
    ("value_type", "value", "expected"),
    [
        (RegistryType.REG_SZ, "hello", "hello"),
        (RegistryType.REG_EXPAND_SZ, "%TEMP%", "%TEMP%"),
        (RegistryType.REG_MULTI_SZ, ["one", "two"], ["one", "two"]),
        (RegistryType.REG_DWORD, 0xFFFFFFFF, 0xFFFFFFFF),
        (RegistryType.REG_QWORD, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF),
        (RegistryType.REG_BINARY, b"\x00\xff", b"\x00\xff"),
        (RegistryType.REG_NONE, b"\x01\x02", b"\x01\x02"),
        (0x1234, b"opaque", b"opaque"),
    ],
)
def test_supported_and_opaque_values_round_trip(value_type: int, value: object, expected: object) -> None:
    encoded = encode_value(value_type, value)
    assert decode_value(value_type, encoded) == expected


def test_filetime_epoch_conversion() -> None:
    value = filetime_to_datetime(116444736000000000)
    assert value == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_big_endian_dword_decoding() -> None:
    assert decode_value(RegistryType.REG_DWORD_BIG_ENDIAN, b"\x12\x34\x56\x78") == 0x12345678


@pytest.mark.parametrize("value", [None, 0, "bad", 10**100])
def test_invalid_filetime_is_safe(value: object) -> None:
    assert filetime_to_datetime(value) is None


def test_invalid_filetime_retains_raw_evidence_for_display() -> None:
    timestamp = HiveTimestamp(raw=10**30, value=None)
    assert timestamp.display == f"Invalid FILETIME ({10**30})"


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        (RegistryType.REG_DWORD, -1),
        (RegistryType.REG_DWORD, 1 << 32),
        (RegistryType.REG_DWORD, 1.5),
        (RegistryType.REG_DWORD, True),
        (RegistryType.REG_QWORD, -1),
        (RegistryType.REG_QWORD, 1 << 64),
        (RegistryType.REG_MULTI_SZ, ["one", "", "two"]),
        (RegistryType.REG_MULTI_SZ, "not a sequence of entries"),
        (RegistryType.REG_SZ, "embedded\x00nul"),
        (RegistryType.REG_NONE, "not raw bytes"),
        (0x1234, "not raw bytes"),
    ],
)
def test_invalid_encodings_are_rejected(value_type: int, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_value(value_type, value)


def test_importing_package_does_not_require_hivex() -> None:
    import reg_hive_gui

    assert reg_hive_gui.RegistryType.REG_SZ == 1

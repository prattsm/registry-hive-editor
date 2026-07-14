from __future__ import annotations

import pytest

from reg_hive_gui.validation import (
    MAX_KEY_NAME_LENGTH,
    MAX_VALUE_NAME_LENGTH,
    REG_BINARY,
    REG_DWORD,
    REG_MULTI_SZ,
    REG_QWORD,
    REG_SZ,
    parse_binary_text,
    parse_value_text,
    validate_key_name,
    validate_value_name,
)


@pytest.mark.parametrize("name", ["", "bad\\name", "bad/name", "bad\x00name"])
def test_invalid_key_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        validate_key_name(name)


def test_key_name_length_limit() -> None:
    assert validate_key_name("x" * MAX_KEY_NAME_LENGTH)
    with pytest.raises(ValueError):
        validate_key_name("x" * (MAX_KEY_NAME_LENGTH + 1))


def test_default_value_name_is_valid_but_nul_and_oversize_are_not() -> None:
    assert validate_value_name("") == ""
    with pytest.raises(ValueError):
        validate_value_name("bad\x00name")
    with pytest.raises(ValueError):
        validate_value_name("x" * (MAX_VALUE_NAME_LENGTH + 1))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", b""),
        ("001aFF", b"\x00\x1a\xff"),
        ("00 1a ff", b"\x00\x1a\xff"),
        ("00:1a:ff", b"\x00\x1a\xff"),
        ("00, 1a, ff", b"\x00\x1a\xff"),
        ("00-1a-ff", b"\x00\x1a\xff"),
    ],
)
def test_binary_parser_accepts_explicit_hex_formats(text: str, expected: bytes) -> None:
    assert parse_binary_text(text) == expected


@pytest.mark.parametrize("text", ["0", "0xz1", "00 gg", "001", "not hex"])
def test_binary_parser_rejects_malformed_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_binary_text(text)


def test_integer_parsing_enforces_unsigned_width() -> None:
    assert parse_value_text(REG_DWORD, "08") == 8
    assert parse_value_text(REG_DWORD, "0xffffffff") == (1 << 32) - 1
    assert parse_value_text(REG_QWORD, str((1 << 64) - 1)) == (1 << 64) - 1
    for value_type, text in (
        (REG_DWORD, "-1"),
        (REG_DWORD, str(1 << 32)),
        (REG_QWORD, str(1 << 64)),
        (REG_QWORD, "1.5"),
    ):
        with pytest.raises(ValueError):
            parse_value_text(value_type, text)


def test_multi_string_does_not_silently_drop_entries() -> None:
    assert parse_value_text(REG_MULTI_SZ, "one\ntwo") == ["one", "two"]
    assert parse_value_text(REG_MULTI_SZ, "") == []
    with pytest.raises(ValueError):
        parse_value_text(REG_MULTI_SZ, "one\n\ntwo")


def test_string_nul_and_unsupported_types_are_rejected() -> None:
    with pytest.raises(ValueError):
        parse_value_text(REG_SZ, "bad\x00text")
    with pytest.raises(ValueError, match="not safely editable"):
        parse_value_text(99, "anything")


def test_binary_value_parser_uses_strict_binary_parser() -> None:
    assert parse_value_text(REG_BINARY, "de ad be ef") == b"\xde\xad\xbe\xef"
    with pytest.raises(ValueError):
        parse_value_text(REG_BINARY, "de ad nope")

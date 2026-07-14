"""Validation and parsing for registry edits."""

from __future__ import annotations

import re
from collections.abc import Sequence
from numbers import Integral

MAX_KEY_NAME_LENGTH = 255
MAX_VALUE_NAME_LENGTH = 16_383

REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_MULTI_SZ = 7
REG_QWORD = 11

EDITABLE_VALUE_TYPES = frozenset(
    {REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ, REG_QWORD}
)


def validate_key_name(name: str) -> str:
    """Validate one key segment, never an entire path."""
    if not isinstance(name, str):
        raise TypeError("Key name must be text")
    if not name:
        raise ValueError("Key name cannot be empty")
    if len(name) > MAX_KEY_NAME_LENGTH:
        raise ValueError(f"Key name cannot exceed {MAX_KEY_NAME_LENGTH} characters")
    if "\x00" in name:
        raise ValueError("Key name cannot contain a NUL character")
    if "\\" in name or "/" in name:
        raise ValueError("Key name cannot contain a slash or backslash")
    return name


def validate_value_name(name: str) -> str:
    """Validate a value name; an empty name is the default value."""
    if not isinstance(name, str):
        raise TypeError("Value name must be text")
    if len(name) > MAX_VALUE_NAME_LENGTH:
        raise ValueError(f"Value name cannot exceed {MAX_VALUE_NAME_LENGTH} characters")
    if "\x00" in name:
        raise ValueError("Value name cannot contain a NUL character")
    return name


def parse_binary_text(text: str) -> bytes:
    """Parse continuous hex or separated byte pairs without discarding input."""
    stripped = text.strip()
    if not stripped:
        return b""
    if re.fullmatch(r"[0-9A-Fa-f]+", stripped):
        if len(stripped) % 2:
            raise ValueError("Binary hex must contain an even number of digits")
        return bytes.fromhex(stripped)
    if not re.fullmatch(
        r"[0-9A-Fa-f]{2}(?:[\s,:-]+[0-9A-Fa-f]{2})*", stripped
    ):
        raise ValueError("Binary data must contain hexadecimal byte pairs only")
    compact = re.sub(r"[\s,:-]+", "", stripped)
    return bytes.fromhex(compact)


def _parse_unsigned_integer(text: str, bits: int) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    try:
        base = 16 if stripped.lower().startswith("0x") else 10
        value = int(stripped, base)
    except ValueError as exc:
        raise ValueError(f"Expected a {bits}-bit unsigned integer") from exc
    maximum = (1 << bits) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"Value must be between 0 and {maximum}")
    return value


def parse_value_text(value_type: int, text: str) -> object:
    """Parse user-entered text for a supported registry type."""
    value_type = int(value_type)
    if value_type not in EDITABLE_VALUE_TYPES:
        raise ValueError(f"Registry type {value_type} is not safely editable")
    if value_type == REG_MULTI_SZ:
        if not text:
            return []
        values = text.splitlines()
        if any(value == "" for value in values):
            raise ValueError("REG_MULTI_SZ entries cannot be empty")
        if any("\x00" in value for value in values):
            raise ValueError("REG_MULTI_SZ entries cannot contain NUL characters")
        return values
    if value_type == REG_DWORD:
        return _parse_unsigned_integer(text, 32)
    if value_type == REG_QWORD:
        return _parse_unsigned_integer(text, 64)
    if value_type == REG_BINARY:
        return parse_binary_text(text)
    if "\x00" in text:
        raise ValueError("String values cannot contain NUL characters")
    return text


def validate_multi_string(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("REG_MULTI_SZ requires a sequence of strings")
    items = list(value)
    if any(not isinstance(item, str) for item in items):
        raise TypeError("REG_MULTI_SZ entries must be strings")
    if any(not item for item in items):
        raise ValueError("REG_MULTI_SZ entries cannot be empty")
    if any("\x00" in item for item in items):
        raise ValueError("REG_MULTI_SZ entries cannot contain NUL characters")
    return items


def validate_unsigned_integer(value: object, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"REG_{'DWORD' if bits == 32 else 'QWORD'} requires an integer")
    integer = int(value)
    maximum = (1 << bits) - 1
    if not 0 <= integer <= maximum:
        raise ValueError(f"Value must be between 0 and {maximum}")
    return integer

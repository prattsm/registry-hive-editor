from __future__ import annotations

import pytest

from reg_hive_gui.binary_view import format_hex_ascii, format_match_context, parse_hex_pattern


@pytest.mark.parametrize(
    "text",
    ["DE AD BE EF", "deadbeef", "0xDE,0xAD,0xBE,0xEF", "de-ad-be-ef"],
)
def test_parse_hex_pattern_accepts_common_exact_byte_notation(text: str) -> None:
    assert parse_hex_pattern(text) == b"\xde\xad\xbe\xef"


@pytest.mark.parametrize("text", ["", "A", "GG", "12 345"])
def test_parse_hex_pattern_rejects_ambiguous_or_invalid_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_hex_pattern(text)


def test_hex_ascii_view_has_offsets_and_is_bounded() -> None:
    text, truncated = format_hex_ascii(b"ABC\x00" + bytes(range(32)), limit=16, width=8)

    assert text.splitlines()[0] == "00000000  41 42 43 00 00 01 02 03  |ABC.....|"
    assert text.splitlines()[1].startswith("00000008")
    assert truncated


def test_match_context_is_small_and_centered_on_hit() -> None:
    data = bytes(range(100))
    context = format_match_context(data, 50, 2)

    assert context == data[42:60].hex(" ")

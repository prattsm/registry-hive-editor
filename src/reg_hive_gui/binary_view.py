"""Bounded binary display and exact byte-pattern parsing helpers."""
from __future__ import annotations

import re

HEX_PREVIEW_BYTES = 16 * 1024
HEX_LINE_BYTES = 16


def parse_hex_pattern(text: str) -> bytes:
    """Parse an exact byte sequence written as compact or separated hex."""
    normalized = re.sub(r"(?i)0x", "", text.strip())
    normalized = re.sub(r"[\s,:;_-]+", "", normalized)
    if not normalized:
        raise ValueError("Enter at least one hexadecimal byte.")
    if len(normalized) % 2:
        raise ValueError("Hex byte searches require two digits per byte.")
    if re.fullmatch(r"[0-9a-fA-F]+", normalized) is None:
        raise ValueError("Hex byte searches may contain only hexadecimal digits and separators.")
    return bytes.fromhex(normalized)


def format_hex_ascii(
    data: bytes,
    *,
    limit: int = HEX_PREVIEW_BYTES,
    width: int = HEX_LINE_BYTES,
) -> tuple[str, bool]:
    """Return an offset/hex/ASCII view without rendering more than ``limit`` bytes."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    if width <= 0:
        raise ValueError("width must be positive")
    preview = data[:limit]
    lines: list[str] = []
    hex_width = width * 3 - 1
    for offset in range(0, len(preview), width):
        chunk = preview[offset : offset + width]
        hex_text = chunk.hex(" ").ljust(hex_width)
        ascii_text = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)
        lines.append(f"{offset:08X}  {hex_text}  |{ascii_text:<{width}}|")
    if not lines:
        lines.append("(empty)")
    return "\n".join(lines), len(preview) < len(data)


def format_match_context(data: bytes, offset: int, pattern_size: int) -> str:
    """Return a small byte context around a search hit."""
    start = max(0, offset - 8)
    end = min(len(data), offset + pattern_size + 8)
    return data[start:end].hex(" ")

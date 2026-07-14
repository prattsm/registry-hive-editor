"""Bounded triage of binary and opaque registry values."""

from __future__ import annotations

import math
import re
from collections import Counter

from reg_hive_gui.hive import Hive, RegistryType

PLUGIN_NAME = "Binary Triage"
PLUGIN_DESCRIPTION = "Summarize binary values, entropy, signatures, and bounded embedded strings."
PLUGIN_VERSION = "1.0"
PLUGIN_TARGET_HIVES = ("ANY",)
PLUGIN_REQUIRED_PATHS: tuple[str, ...] = ()

MAX_ROWS = 10_000
MAX_SCAN_BYTES = 1_048_576
MAX_STRINGS = 20
MAX_STRING_LENGTH = 512
BINARY_TYPES = {
    RegistryType.REG_NONE,
    RegistryType.REG_BINARY,
    RegistryType.REG_RESOURCE_LIST,
    RegistryType.REG_FULL_RESOURCE_DESCRIPTOR,
    RegistryType.REG_RESOURCE_REQUIREMENTS_LIST,
}


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _signature(data: bytes) -> str:
    if data.startswith(b"MZ"):
        return "Windows executable (MZ)"
    if data.startswith(b"regf"):
        return "Registry hive (regf)"
    if data.startswith(b"PK\x03\x04"):
        return "ZIP archive"
    if data.startswith(b"\x7fELF"):
        return "ELF executable"
    return ""


def _embedded_strings(data: bytes) -> str:
    sample = data[:MAX_SCAN_BYTES]
    found: list[str] = []
    for match in re.finditer(rb"[\x20-\x7e]{4,}", sample):
        found.append(match.group()[:MAX_STRING_LENGTH].decode("ascii"))
        if len(found) >= MAX_STRINGS:
            break
    if len(found) < MAX_STRINGS:
        for match in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", sample):
            encoded_limit = MAX_STRING_LENGTH * 2
            found.append(
                match.group()[:encoded_limit].decode("utf-16le").rstrip("\x00")
            )
            if len(found) >= MAX_STRINGS:
                break
    return " | ".join(found)


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path, node in hive.iter_key_nodes():
        for value in hive.iter_values_for_node(node):
            if value.type not in BINARY_TYPES:
                continue
            sample = value.data[:MAX_SCAN_BYTES]
            rows.append(
                {
                    "path": path,
                    "name": value.name,
                    "type": value.type_name,
                    "size": len(value.data),
                    "entropy": round(_entropy(sample), 4),
                    "signature": _signature(value.data),
                    "strings": _embedded_strings(value.data),
                    "scan_truncated": len(sample) < len(value.data),
                }
            )
            if len(rows) >= MAX_ROWS:
                return rows
    return rows

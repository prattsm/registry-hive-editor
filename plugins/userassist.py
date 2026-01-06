"""Example plugin: extract UserAssist entries from NTUSER.DAT."""
from __future__ import annotations

import codecs

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "UserAssist"
PLUGIN_DESCRIPTION = "Extract UserAssist entries (ROT13 names) from NTUSER.DAT."


BASE_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\UserAssist"


def _rot13(text: str) -> str:
    return codecs.decode(text, "rot_13")


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    subkeys = hive.list_subkeys(BASE_PATH)
    if not subkeys:
        return rows
    for subkey in subkeys:
        count_path = f"{BASE_PATH}\\{subkey}\\Count"
        values = hive.list_values(count_path)
        for value in values:
            rows.append(
                {
                    "path": count_path,
                    "name": value.name,
                    "decoded_name": _rot13(value.name),
                    "data_hex": value.data.hex(" "),
                }
            )
    return rows

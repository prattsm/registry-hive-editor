"""Extract UserAssist values from an NTUSER.DAT hive."""

from __future__ import annotations

import codecs

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "UserAssist"
PLUGIN_DESCRIPTION = "Extract UserAssist entries and decode ROT13 value names."

BASE_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\UserAssist"


def _rot13(text: str) -> str:
    return codecs.decode(text, "rot_13")


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for subkey in hive.list_subkeys(BASE_PATH):
        count_path = f"{BASE_PATH}\\{subkey}\\Count"
        for value in hive.list_values(count_path):
            rows.append(
                {
                    "path": count_path,
                    "name": value.name,
                    "decoded_name": _rot13(value.name),
                    "data_hex": value.data.hex(" "),
                }
            )
    return rows

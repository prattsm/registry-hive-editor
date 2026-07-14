"""Extract UserAssist values from an NTUSER.DAT hive."""

from __future__ import annotations

import codecs
import struct

from reg_hive_gui.hive import Hive, filetime_to_datetime

PLUGIN_NAME = "UserAssist"
PLUGIN_DESCRIPTION = "Extract UserAssist entries and decode ROT13 value names."
PLUGIN_VERSION = "1.1"
PLUGIN_TARGET_HIVES = ("NTUSER.DAT",)

BASE_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\UserAssist"
PLUGIN_REQUIRED_PATHS = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\UserAssist",
)


def _rot13(text: str) -> str:
    return codecs.decode(text, "rot_13")


def _parse_userassist(data: bytes) -> dict[str, object]:
    parsed: dict[str, object] = {
        "session_id": "",
        "run_count": "",
        "focus_count": "",
        "focus_time_ms": "",
        "last_execution": "",
        "structure": "unrecognized",
    }
    if len(data) >= 72:
        session_id, run_count, focus_count, focus_time = struct.unpack_from("<IIII", data)
        last_execution_raw = struct.unpack_from("<Q", data, 60)[0]
        last_execution = filetime_to_datetime(last_execution_raw)
        parsed.update(
            {
                "session_id": session_id,
                "run_count": run_count,
                "focus_count": focus_count,
                "focus_time_ms": focus_time,
                "last_execution": last_execution.isoformat() if last_execution else "",
                "structure": "Windows 7+",
            }
        )
    elif len(data) >= 16:
        run_count = struct.unpack_from("<I", data, 4)[0]
        last_execution_raw = struct.unpack_from("<Q", data, 8)[0]
        last_execution = filetime_to_datetime(last_execution_raw)
        parsed.update(
            {
                "run_count": run_count,
                "last_execution": last_execution.isoformat() if last_execution else "",
                "structure": "legacy",
            }
        )
    return parsed


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for subkey in hive.list_subkeys(BASE_PATH):
        count_path = f"{BASE_PATH}\\{subkey}\\Count"
        for value in hive.list_values(count_path):
            parsed = _parse_userassist(value.data)
            rows.append(
                {
                    "path": count_path,
                    "name": value.name,
                    "decoded_name": _rot13(value.name),
                    "data_hex": value.data.hex(" "),
                    **parsed,
                }
            )
    return rows

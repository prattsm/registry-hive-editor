"""Extract Run and RunOnce values from a SOFTWARE hive."""

from __future__ import annotations

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Run Keys"
PLUGIN_DESCRIPTION = "Extract Run and RunOnce entries from common locations."
PLUGIN_VERSION = "1.1"
PLUGIN_TARGET_HIVES = ("SOFTWARE",)

RUN_PATHS = [
    "Microsoft\\Windows\\CurrentVersion\\Run",
    "Microsoft\\Windows\\CurrentVersion\\RunOnce",
    "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run",
    "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\RunOnce",
]
PLUGIN_REQUIRED_PATHS = (
    "Microsoft\\Windows\\CurrentVersion\\Run",
    "Microsoft\\Windows\\CurrentVersion\\RunOnce",
    "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run",
    "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\RunOnce",
)


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in RUN_PATHS:
        for value in hive.list_values(path):
            architecture = "32-bit" if path.startswith("WOW6432Node\\") else "64-bit/native"
            location = "RunOnce" if path.endswith("\\RunOnce") else "Run"
            rows.append(
                {
                    "path": path,
                    "name": value.name,
                    "location": location,
                    "architecture": architecture,
                    "type": value.type_name,
                    "data": value.decoded,
                    "raw_data_hex": value.data.hex(" "),
                }
            )
    return rows

"""Example plugin: extract Run/RunOnce keys."""
from __future__ import annotations

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Run Keys"
PLUGIN_DESCRIPTION = "Extract Run and RunOnce entries from common locations."


RUN_PATHS = [
    "Microsoft\\Windows\\CurrentVersion\\Run",
    "Microsoft\\Windows\\CurrentVersion\\RunOnce",
    "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run",
    "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\RunOnce",
]


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in RUN_PATHS:
        values = hive.list_values(path)
        if not values:
            continue
        for value in values:
            rows.append(
                {
                    "path": path,
                    "name": value.name,
                    "type": value.type_name,
                    "data": value.decoded,
                }
            )
    return rows

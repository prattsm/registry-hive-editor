"""Extract services from a SYSTEM hive."""

from __future__ import annotations

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Services"
PLUGIN_DESCRIPTION = "List services from available ControlSet service keys."

SERVICE_PATHS = [
    "ControlSet001\\Services",
    "ControlSet002\\Services",
    "CurrentControlSet\\Services",
]


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for base in SERVICE_PATHS:
        for service in hive.list_subkeys(base):
            path = f"{base}\\{service}"
            display_name = hive.get_value(path, "DisplayName")
            start_value = hive.get_value(path, "Start")
            rows.append(
                {
                    "path": path,
                    "service": service,
                    "display_name": display_name.decoded if display_name else "",
                    "start": start_value.decoded if start_value else "",
                }
            )
    return rows

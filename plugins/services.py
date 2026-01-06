"""Example plugin: extract services from SYSTEM hive."""
from __future__ import annotations

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Services"
PLUGIN_DESCRIPTION = "List services from CurrentControlSet\\Services (SYSTEM hive)."


SERVICE_PATHS = [
    "ControlSet001\\Services",
    "ControlSet002\\Services",
    "CurrentControlSet\\Services",
]


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for base in SERVICE_PATHS:
        subkeys = hive.list_subkeys(base)
        if not subkeys:
            continue
        for service in subkeys:
            display_name = hive.get_value(f"{base}\\{service}", "DisplayName")
            start_value = hive.get_value(f"{base}\\{service}", "Start")
            rows.append(
                {
                    "path": f"{base}\\{service}",
                    "service": service,
                    "display_name": display_name.decoded if display_name else "",
                    "start": start_value.decoded if start_value else "",
                }
            )
    return rows

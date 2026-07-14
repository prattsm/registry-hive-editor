"""Extract services from a SYSTEM hive."""

from __future__ import annotations

from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Services"
PLUGIN_DESCRIPTION = "List services from available ControlSet service keys."
PLUGIN_VERSION = "1.1"
PLUGIN_TARGET_HIVES = ("SYSTEM",)

SERVICE_PATHS = [
    "ControlSet001\\Services",
    "ControlSet002\\Services",
    "CurrentControlSet\\Services",
]
PLUGIN_REQUIRED_PATHS = (
    "Select",
    "ControlSet001\\Services",
    "ControlSet002\\Services",
    "CurrentControlSet\\Services",
)

START_LABELS = {
    0: "Boot",
    1: "System",
    2: "Automatic",
    3: "Manual",
    4: "Disabled",
}
TYPE_LABELS = {
    1: "Kernel driver",
    2: "File-system driver",
    16: "Own-process service",
    32: "Shared-process service",
}


def _decoded(hive: Hive, path: str, name: str) -> object:
    value = hive.get_value(path, name)
    return value.decoded if value is not None else ""


def _service_bases(hive: Hive) -> list[str]:
    bases: list[str] = []
    current = _decoded(hive, "Select", "Current")
    if isinstance(current, int):
        bases.append(f"ControlSet{current:03d}\\Services")
    bases.extend(SERVICE_PATHS)
    result: list[str] = []
    for path in bases:
        if path.casefold() not in {item.casefold() for item in result} and hive.get_node(path) is not None:
            result.append(path)
    return result


def analyze(hive: Hive) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for base in _service_bases(hive):
        for service in hive.list_subkeys(base):
            path = f"{base}\\{service}"
            start = _decoded(hive, path, "Start")
            service_type = _decoded(hive, path, "Type")
            parameters_path = f"{path}\\Parameters"
            rows.append(
                {
                    "path": path,
                    "control_set": base.split("\\", 1)[0],
                    "service": service,
                    "display_name": _decoded(hive, path, "DisplayName"),
                    "start": start,
                    "start_label": START_LABELS.get(start, "Unknown") if isinstance(start, int) else "",
                    "type": service_type,
                    "type_label": (
                        TYPE_LABELS.get(service_type, "Other")
                        if isinstance(service_type, int)
                        else ""
                    ),
                    "image_path": _decoded(hive, path, "ImagePath"),
                    "object_name": _decoded(hive, path, "ObjectName"),
                    "service_dll": _decoded(hive, parameters_path, "ServiceDll"),
                }
            )
    return rows

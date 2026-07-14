# Plugin API

A plugin is a UTF-8 Python file with literal metadata and a synchronous `analyze` function:

```python
from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Example"
PLUGIN_DESCRIPTION = "Describe what the plugin extracts."


def analyze(hive: Hive) -> list[dict[str, object]]:
    return [{"path": path} for path in hive.iter_keys()]
```

`PLUGIN_NAME` and `PLUGIN_DESCRIPTION` must be literal strings so discovery can read them without
executing the file. `analyze(hive)` receives an independently opened, read-only `Hive` and must
return a list of row dictionaries with text column names.

Supported cell values are strings, numbers, booleans, `None`, bytes, dates, datetimes, mappings,
and sequences. Bytes become spaced hexadecimal text; nested mappings and sequences become JSON.
Unsupported objects are rejected instead of invoking arbitrary display conversions in the GUI.

Limits protect the GUI and report exporter: 100,000 rows, 256 columns per row, 1,000,000 characters
per text cell, and 120 seconds per run.

External plugins are untrusted code. The confirmation prompt identifies their source path. The child
process timeout limits hangs and crashes, but it does not restrict the plugin's account permissions.

The package ships three examples in `reg_hive_gui/builtin_plugins`: Run Keys, Services, and
UserAssist.

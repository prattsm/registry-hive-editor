# Plugin API

A plugin is a UTF-8 Python file with literal metadata and a synchronous `analyze` function:

```python
from reg_hive_gui.hive import Hive

PLUGIN_NAME = "Example"
PLUGIN_DESCRIPTION = "Describe what the plugin extracts."
PLUGIN_VERSION = "1.0"
PLUGIN_TARGET_HIVES = ("SOFTWARE",)
PLUGIN_REQUIRED_PATHS = ("Microsoft\\Windows\\CurrentVersion",)


def analyze(hive: Hive) -> list[dict[str, object]]:
    return [{"path": path} for path in hive.iter_keys()]
```

All `PLUGIN_*` metadata must be literal strings or literal string sequences so discovery can read it
without executing the file. Required paths use any-match semantics: the plugin is enabled when at
least one is present. An empty required-path tuple applies to every open hive. `analyze(hive)`
receives an independently opened, read-only `Hive` and must
return a list of row dictionaries with text column names.

Supported cell values are strings, numbers, booleans, `None`, bytes, dates, datetimes, mappings,
and sequences. Bytes become spaced hexadecimal text; nested mappings and sequences become JSON.
Unsupported objects are rejected instead of invoking arbitrary display conversions in the GUI.

Limits protect the GUI and report exporter: 100,000 rows, 256 columns per row, 1,000,000 characters
per text cell, and 120 seconds per run.

External plugins are untrusted code. The confirmation prompt identifies their source path. The child
process timeout limits hangs and crashes, but it does not restrict the plugin's account permissions.

The package ships four analyzers in `reg_hive_gui/builtin_plugins`: Binary Triage, Run Keys,
Services, and UserAssist. Binary Triage caps rows, scanned bytes, extracted string count, and each
string's length.

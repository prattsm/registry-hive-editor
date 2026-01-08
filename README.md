# Registry Hive GUI

Offline Windows registry hive viewer/editor with "hive in / hive out" safety. Designed for DFIR workflows and large hives.

## Features
- Load offline hives (SYSTEM, SOFTWARE, SAM, SECURITY, NTUSER.DAT, etc.)
- Tree + values view with fast search
- Create/rename/delete keys and values
- Decoded and hex/raw value views
- Bookmarks, jump-to-path, copy path
- Export reports (search results, subtree) as JSON/CSV
- Compare two hives and export diff report
- Timeline view (key last-write times)
- Plugin hooks with sample plugins
- Read-only mode

## Requirements
- Python 3.11+
- PySide6
- hivex bindings (system package recommended)

### Install dependencies
Recommended on Ubuntu/WSL:
```
sudo apt-get update
sudo apt-get install -y python3-hivex libhivex0
python -m pip install PySide6
```

Note: `pip install hivex` may fail on some systems. The app uses the system `python3-hivex` package via a shim in `src/reg_hive_gui/_hivex.py`.

## Run
From the repo root:
```
python scripts/run_gui.py
```
Optionally pass a hive path:
```
python scripts/run_gui.py sample_hives/SOFTWARE
```
## Create a sample SOFTWARE hive
To test with a real `SOFTWARE` hive from your own Windows machine, use `reg save` to create a hive-format file (not a `.reg` text export).

1. Create a folder to write the export (example):
   - `C:\Temp\hives`
2. Open **Command Prompt** or **PowerShell** as **Administrator**.
3. Run:
   ```
   reg save HKLM\SOFTWARE C:\Temp\hives\SOFTWARE /y
   ```

## Tests
```
python -m pip install pytest
python -m pytest -q
```

## Plugins
Plugins are Python files exposing:
- `PLUGIN_NAME` (optional)
- `PLUGIN_DESCRIPTION` (optional)
- `analyze(hive: Hive) -> list[dict]`

Search paths:
- `./plugins/*.py`
- `~/.config/reg_hive_gui/plugins/*.py`

Sample plugins are included in `plugins/`.

## Notes
- The input hive is never overwritten. Export always writes to a new path.
- Read-only mode disables editing (safe for triage/review).

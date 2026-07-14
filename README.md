# Registry Hive Editor

A safety-focused desktop viewer and editor for offline Windows registry hives. It runs natively on
Windows through Microsoft's Offline Registry Library—WSL, Linux, and `hivex` are not required on
Windows.

## Capabilities

- Open SYSTEM, SOFTWARE, SAM, SECURITY, NTUSER.DAT, and other offline hives.
- Browse keys lazily and inspect decoded data plus a bounded offset/hex/ASCII view.
- Search text or exact hex bytes in a cancellable background job with incremental results.
- Create/delete keys and create/edit/delete supported values in an in-memory working copy.
- Export edited hives to a separate, atomically replaced output file.
- Export subtree, search, timeline, plugin, and comparison reports as JSON or CSV.
- Compare large hives with a bounded-memory, case-insensitive SQLite index, probable value-rename
  correlation, and changed-byte ranges.
- Inspect hive header health, SHA-256 identity, and adjacent transaction-log sidecars.
- Keep per-evidence bookmarks and notes in atomic sidecars keyed by the hive SHA-256.
- Run four packaged, hive-aware analysis plugins or explicitly approved external plugins.

Every session starts read-only. Key rename and editing of opaque registry types are deliberately
disabled where metadata-preserving behavior cannot be guaranteed. See [the safety model](docs/SAFETY.md).

## Native Windows setup

Requirements:

- 64-bit Windows 10/11 or a supported Windows Server release
- Python 3.11 through 3.14
- `%SystemRoot%\System32\offreg.dll` (present on current supported Windows images)

In PowerShell from the repository folder:

```powershell
py -3.14 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
& .\.venv\Scripts\registry-hive-editor-gui.exe
```

You can also run:

```powershell
& .\.venv\Scripts\python.exe -m reg_hive_gui
& .\.venv\Scripts\python.exe -m reg_hive_gui C:\Temp\hives\SOFTWARE
```

For PyCharm, open the repository folder and select
`.venv\Scripts\python.exe` as the project interpreter. Run the `reg_hive_gui` module or
`scripts\run_gui.py`.

The Windows backend loads `offreg.dll` only from the trusted System32 location. Microsoft's library
opens and validates the input into memory; changes do not persist until the app saves a new hive.
Microsoft documents a less-than-4-GB input limit for `OROpenHive`.

## Linux setup

Non-Windows systems continue to use the `hivex` Python binding:

```bash
sudo apt-get update
sudo apt-get install -y python3-hivex libhivex0
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e .
.venv/bin/registry-hive-editor
```

## Safe edit workflow

1. Preserve and hash the original hive with your approved evidence tool. Use **Hive Information**
   to record the app's SHA-256 and header-health view as a second reference.
2. Open it; the app starts read-only.
3. If editing is required, uncheck **Read-only Mode** and accept the warning.
4. Make changes in the in-memory working copy.
5. Choose **Export Hive As** and select a different output path.
6. Record the SHA-256 displayed after export and independently validate/hash the exported hive.

Exports reject the original path and hard links to it. A staging file is flushed and validated
before it atomically replaces the selected destination, so a failed write does not destroy an
existing output file.

To make a test SOFTWARE hive from the active registry, open an elevated PowerShell or Command
Prompt and run:

```powershell
New-Item -ItemType Directory -Force C:\Temp\hives
reg save HKLM\SOFTWARE C:\Temp\hives\SOFTWARE /y
```

This produces a binary hive. A `.reg` text export is not a hive and cannot be opened.

## Plugins

Built-ins are packaged under `reg_hive_gui/builtin_plugins`. External plugins are discovered at:

- Windows: `%LOCALAPPDATA%\RegistryHiveEditor\plugins`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/reg_hive_gui/plugins`

Discovery parses source without executing it. Plugins can declare versions, intended hive types,
and identifying paths; nonmatching analyzers are disabled for the open hive. The app asks before
every external plugin run and executes it in a time-limited child process. That process is not an
OS sandbox; only run trusted
code. See [the plugin API](docs/PLUGINS.md).

Bookmark sidecars are stored under `%LOCALAPPDATA%\RegistryHiveEditor\annotations` on Windows (or
`${XDG_CONFIG_HOME:-~/.config}/reg_hive_gui/annotations` on Linux). They never modify the evidence
hive and are loaded only when the full SHA-256 matches.

## Development and tests

```text
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -p no:cacheprovider
python -m compileall -q src scripts tests
python -m build
```

Windows test runs create synthetic hives through Offreg and verify create/open/edit/export/reopen,
recursive deletion, corrupt-input rejection, source immutability, and plugin subprocess access. No
real registry evidence is stored in the repository. The CI matrix covers Windows and Ubuntu on
Python 3.11–3.14. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Backend references

- [Microsoft: About the Offline Registry Library](https://learn.microsoft.com/en-us/windows/win32/devnotes/about-the-offline-registry-library)
- [Microsoft: Offline Registry Library functions](https://learn.microsoft.com/en-us/windows/win32/devnotes/offline-registry-library-functions)
- [Microsoft: ORSaveHive](https://learn.microsoft.com/en-us/windows/win32/devnotes/orsavehive)

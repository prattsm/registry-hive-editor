# Safety model

Registry Hive Editor uses a separate-input/separate-output workflow. It does not modify the hive
passed to it. Editing occurs in a backend-managed working copy, and an edited hive must be exported
to a different path.

## Defaults and write controls

- Every application session starts in read-only mode, regardless of the previous session.
- Enabling editing requires an explicit warning confirmation.
- The input path, aliases of that path, and hard links to that file cannot be export targets.
- Hive and report exports use a same-directory staging file and atomically move it into place only
  after a successful write. Edited hive exports are structurally reopened before replacement and
  their SHA-256 is reported. A failed export leaves an existing destination intact.
- Closing, opening another hive, or changing mode prompts when the latest edit revision has not
  been exported.

The app verifies the `regf` signature and structurally reopens edited output with an independent
read-only backend handle, but this is not a substitute for
organizational evidence-handling procedures. Preserve the original evidence, record hashes with an
approved forensic tool, and validate exported artifacts before operational use.

## Editing guarantees and limits

The editor strictly validates key names, value names, integers, hexadecimal bytes, strings, and
multi-strings. Value replacement is write-then-verify with best-effort rollback if verification or
removal fails. Registry names are checked case-insensitively.

The safely editable value types are `REG_SZ`, `REG_EXPAND_SZ`, `REG_MULTI_SZ`, `REG_DWORD`,
`REG_QWORD`, and `REG_BINARY`. Other types remain viewable and exportable as raw bytes but cannot be
edited through the GUI.

Key rename is deliberately disabled because the supported backends do not expose a rename that can
guarantee preservation of all timestamps, class data, and security metadata. Key creation and
recursive deletion remain available in edit mode.

On Windows, Offreg saves using the current Windows major/minor target format, as specified by the
`ORSaveHive` API. If an output must boot on an older Windows release, validate format compatibility
in an isolated test environment before deploying it.

## Background operations

Search, comparison, timeline analysis, and plugins use independent read-only hive handles. Dirty
working data is first exported to a temporary snapshot. Jobs support cancellation and are stopped
before a hive is changed or the application exits. Old job generations cannot publish results into
a newly loaded hive.

Search results are emitted incrementally and capped at 50,000 entries. Exact byte search reports the
matching offset. The UI reports when the cap is reached. Large displayed values and binary triage
use explicit byte/string/row bounds; deliberate copy and report operations
retain the complete formatted data.

Bookmarks and notes are separate JSON files under the user's application-data directory. Each is
named and internally identified by the complete SHA-256 of the source hive, validated on load, and
atomically replaced. They are analysis aids rather than evidence and are not written beside or into
the source hive.

## Plugins

Built-in plugins are trusted package content. External Python plugins are discovered by parsing
their source without importing it, and the app asks before each execution. Execution occurs in a
time-limited child process so a normal plugin exception or hang does not take down the GUI.

This child process is a reliability boundary, not an operating-system security sandbox. An external
plugin runs with the same account permissions as the user and can access files or launch programs.
Only run code you have reviewed and trust.

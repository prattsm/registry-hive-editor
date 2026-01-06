# Registry Hive GUI Editor (offline hive in / hive out)

## Core requirements
- Python GUI application.
- Load an offline Windows registry hive file (e.g., SYSTEM/SOFTWARE/SAM/SECURITY/NTUSER.DAT, etc).
- Display keys/values in a tree structure.
- Provide fast search across:
  - key paths
  - value names
  - value data (with sensible decoding for common types)
- Allow edits:
  - create/rename/delete keys
  - add/edit/delete values
  - change value types/data
- Never modify the original input hive on disk.
- Export/save the modified hive as a NEW registry hive file (“hive in, hive out”).

## UX expectations
- Large hives should remain usable (avoid freezing; background loading/search if needed).
- Clear status/progress for long operations.
- An “unsafe” action (delete key, etc.) should have confirmation.

## Reverse engineering / DFIR-friendly nice-to-haves
- Hex/raw view + decoded view for value data
- Bookmarks / favorites for interesting keys
- Registry path copy, jump-to-path
- Diff/compare two hives (or export a changeset report)
- Timeline-ish view: last write times for keys, filtering/sorting
- Plugin hooks for common artifact parsers (e.g., userassist, run keys, services)
- Export search results / key subtree to a report (JSON/CSV) in addition to hive export
- Read-only mode toggle

## Constraints
- Must run on Windows via WSL (GUI display through WSLg/X) and normal terminal.
- Prefer a clean, testable core library (hive parsing/editing) with GUI thin layer on top.
- Include tests for core parsing/editing/export logic.

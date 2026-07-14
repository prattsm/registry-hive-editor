from pathlib import Path

from reg_hive_gui.app import resolve_hive_argument


def test_resolve_hive_argument_accepts_no_path() -> None:
    assert resolve_hive_argument([]) == (None, None)


def test_resolve_hive_argument_rejects_missing_path(workspace_tmp_path: Path) -> None:
    path, error = resolve_hive_argument([str(workspace_tmp_path / "missing")])
    assert path is None
    assert error is not None and "does not exist" in error


def test_resolve_hive_argument_rejects_directory(workspace_tmp_path: Path) -> None:
    path, error = resolve_hive_argument([str(workspace_tmp_path)])
    assert path is None
    assert error is not None and "not a file" in error


def test_resolve_hive_argument_rejects_extra_arguments() -> None:
    path, error = resolve_hive_argument(["one", "two"])
    assert path is None
    assert error == "Expected at most one hive path."


def test_resolve_hive_argument_returns_absolute_file(workspace_tmp_path: Path) -> None:
    hive = workspace_tmp_path / "SOFTWARE"
    hive.write_bytes(b"regf")
    path, error = resolve_hive_argument([str(hive)])
    assert error is None
    assert path == hive.resolve()

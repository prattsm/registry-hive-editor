from __future__ import annotations

import os
from pathlib import Path

import pytest

import reg_hive_gui.fileio as fileio
from reg_hive_gui.fileio import atomic_copy_file, paths_refer_to_same_file, staged_output
from reg_hive_gui.hive import Hive
from reg_hive_gui.reporting import export_rows


def test_staged_output_replaces_destination_only_after_success(workspace_tmp_path: Path) -> None:
    output = workspace_tmp_path / "result.bin"
    output.write_bytes(b"old")

    with staged_output(output) as temporary:
        temporary.write_bytes(b"new")
        assert output.read_bytes() == b"old"

    assert output.read_bytes() == b"new"


def test_staged_output_preserves_destination_on_failure(workspace_tmp_path: Path) -> None:
    output = workspace_tmp_path / "result.bin"
    output.write_bytes(b"old")

    with pytest.raises(RuntimeError):
        with staged_output(output) as temporary:
            temporary.write_bytes(b"partial")
            raise RuntimeError("simulated failure")

    assert output.read_bytes() == b"old"
    assert not list(workspace_tmp_path.glob(".result.bin.*.tmp"))


def test_replace_failure_preserves_destination_and_removes_staging_file(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = workspace_tmp_path / "result.bin"
    output.write_bytes(b"old")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(fileio.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="simulated"):
        with staged_output(output) as temporary:
            temporary.write_bytes(b"new")

    assert output.read_bytes() == b"old"
    assert not list(workspace_tmp_path.glob(".result.bin.*.tmp"))


def test_atomic_copy_rejects_same_path_and_hard_link(workspace_tmp_path: Path) -> None:
    source = workspace_tmp_path / "source"
    source.write_bytes(b"regfdata")
    with pytest.raises(ValueError, match="overwrite"):
        atomic_copy_file(source, source)

    hard_link = workspace_tmp_path / "hard-link"
    os.link(source, hard_link)
    assert paths_refer_to_same_file(source, hard_link)
    with pytest.raises(ValueError, match="overwrite"):
        atomic_copy_file(source, hard_link)


def test_atomic_copy_preserves_bytes_and_metadata(workspace_tmp_path: Path) -> None:
    source = workspace_tmp_path / "source"
    destination = workspace_tmp_path / "nested" / "copy"
    source.write_bytes(b"regfdata")

    assert atomic_copy_file(source, destination) == destination
    assert destination.read_bytes() == source.read_bytes()


class CommitHandle:
    def __init__(self, payload: bytes = b"regfvalid", error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    def commit(self, path: str) -> None:
        Path(path).write_bytes(self.payload)
        if self.error is not None:
            raise self.error


def make_export_hive(source: Path, handle: CommitHandle, *, write: bool = True) -> Hive:
    hive = Hive.__new__(Hive)
    hive._path = source
    hive._write = write
    hive._handle = handle
    return hive


def test_hive_export_is_atomic_and_validates_signature(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = workspace_tmp_path / "source"
    source.write_bytes(b"regforiginal")
    output = workspace_tmp_path / "output"
    output.write_bytes(b"existing")
    hive = make_export_hive(source, CommitHandle())
    monkeypatch.setattr(Hive, "_validate_exported_hive", staticmethod(lambda _path: None))

    assert hive.export(output) == output
    assert output.read_bytes() == b"regfvalid"
    assert hive.last_export_sha256 == "c0fd15815def383f157178f2d882b0b3a47d3ca8575c289edbb45732ea90275b"


@pytest.mark.parametrize(
    "handle",
    [CommitHandle(error=OSError("commit failed")), CommitHandle(payload=b"not-a-hive")],
)
def test_failed_hive_export_preserves_existing_destination(
    workspace_tmp_path: Path, handle: CommitHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = workspace_tmp_path / "source"
    source.write_bytes(b"regforiginal")
    output = workspace_tmp_path / "output"
    output.write_bytes(b"existing")
    hive = make_export_hive(source, handle)
    monkeypatch.setattr(Hive, "_validate_exported_hive", staticmethod(lambda _path: None))

    with pytest.raises(OSError):
        hive.export(output)

    assert output.read_bytes() == b"existing"


def test_reopen_validation_failure_preserves_existing_destination(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = workspace_tmp_path / "source"
    output = workspace_tmp_path / "output"
    source.write_bytes(b"regforiginal")
    output.write_bytes(b"existing")
    hive = make_export_hive(source, CommitHandle())

    def fail_validation(_path: Path) -> None:
        raise OSError("simulated structural failure")

    monkeypatch.setattr(Hive, "_validate_exported_hive", staticmethod(fail_validation))

    with pytest.raises(OSError, match="simulated structural failure"):
        hive.export(output)

    assert output.read_bytes() == b"existing"
    assert not list(workspace_tmp_path.glob(".output.*.tmp"))


def test_hive_export_rejects_input_and_read_only_mode(workspace_tmp_path: Path) -> None:
    source = workspace_tmp_path / "source"
    source.write_bytes(b"regforiginal")
    with pytest.raises(ValueError, match="overwrite"):
        make_export_hive(source, CommitHandle()).export(source)
    with pytest.raises(PermissionError):
        make_export_hive(source, CommitHandle(), write=False).export(workspace_tmp_path / "out")


def test_report_exports_replace_existing_content(workspace_tmp_path: Path) -> None:
    rows = [{"path": "A", "value": 1}]
    json_path = workspace_tmp_path / "report.json"
    csv_path = workspace_tmp_path / "report.csv"
    json_path.write_text("old", encoding="utf-8")
    csv_path.write_text("old", encoding="utf-8")

    export_rows(rows, json_path)
    export_rows(rows, csv_path)

    assert '"path": "A"' in json_path.read_text(encoding="utf-8")
    assert "path,value" in csv_path.read_text(encoding="utf-8")


def test_failed_csv_export_preserves_existing_report(workspace_tmp_path: Path) -> None:
    class BrokenText:
        def __str__(self) -> str:
            raise RuntimeError("cannot format")

    output = workspace_tmp_path / "report.csv"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot format"):
        export_rows([{"value": BrokenText()}], output)

    assert output.read_text(encoding="utf-8") == "existing"


def test_failed_streaming_json_export_preserves_existing_report(
    workspace_tmp_path: Path,
) -> None:
    output = workspace_tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")

    def rows():
        yield {"first": 1}
        raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError, match="stream failed"):
        export_rows(rows(), output)

    assert output.read_text(encoding="utf-8") == "existing"

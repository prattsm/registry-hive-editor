"""Atomic filesystem operations used for evidence and report exports."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def paths_refer_to_same_file(source: Path | str, destination: Path | str) -> bool:
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        if source_path.exists() and destination_path.exists():
            return os.path.samefile(source_path, destination_path)
    except OSError:
        pass
    return source_path.resolve() == destination_path.resolve()


def _sync_file(path: Path) -> None:
    # Windows' _commit rejects a read-only descriptor, so open without truncation.
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def staged_output(
    output_path: Path | str,
    *,
    source_path: Path | str | None = None,
    precreate: bool = True,
) -> Iterator[Path]:
    """Yield a same-directory staging path and atomically replace output on success."""
    output = Path(output_path)
    if source_path is not None and paths_refer_to_same_file(source_path, output):
        raise ValueError("Refusing to overwrite the input file")
    if output.exists() and not output.is_file():
        raise IsADirectoryError(f"Output path is not a file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(raw_temp)
    if not precreate:
        temporary.unlink()
    try:
        yield temporary
        if not temporary.is_file():
            raise OSError("Export backend did not create an output file")
        _sync_file(temporary)
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_copy_file(source_path: Path | str, output_path: Path | str) -> Path:
    source = Path(source_path)
    output = Path(output_path)
    if not source.is_file():
        raise FileNotFoundError(f"Input file does not exist: {source}")
    with staged_output(output, source_path=source) as temporary:
        shutil.copy2(source, temporary)
    return output

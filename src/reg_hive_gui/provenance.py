"""Read-only hive provenance, header, and hashing helpers."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_BLOCK_SIZE = 4096
CHECKSUM_OFFSET = 0x1FC
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class HiveHeader:
    primary_sequence: int
    secondary_sequence: int
    last_write_raw: int
    last_write: datetime | None
    major_version: int
    minor_version: int
    file_type: int
    file_format: int
    root_cell_offset: int
    hive_bins_size: int
    clustering_factor: int
    embedded_name: str
    stored_checksum: int
    calculated_checksum: int

    @property
    def sequence_consistent(self) -> bool:
        return self.primary_sequence == self.secondary_sequence

    @property
    def checksum_valid(self) -> bool:
        return self.stored_checksum == self.calculated_checksum


@dataclass(frozen=True)
class HiveProvenance:
    path: Path
    size: int
    modified_at: datetime
    header: HiveHeader
    transaction_logs: tuple[Path, ...]


def _filetime_to_datetime(raw: int) -> datetime | None:
    if raw <= 0:
        return None
    try:
        return FILETIME_EPOCH + timedelta(microseconds=raw // 10)
    except (OverflowError, ValueError):
        return None


def calculate_base_block_checksum(block: bytes) -> int:
    if len(block) < CHECKSUM_OFFSET:
        raise ValueError("Registry hive base block is truncated")
    checksum = 0
    for offset in range(0, CHECKSUM_OFFSET, 4):
        checksum ^= struct.unpack_from("<I", block, offset)[0]
    if checksum == 0xFFFFFFFF:
        return 0xFFFFFFFE
    if checksum == 0:
        return 1
    return checksum


def parse_hive_header(block: bytes) -> HiveHeader:
    if len(block) < CHECKSUM_OFFSET + 4:
        raise ValueError("Registry hive base block is truncated")
    if block[:4] != b"regf":
        raise ValueError("File does not have a registry hive regf signature")
    embedded_name = block[0x30:0x70].decode("utf-16-le", errors="replace").split("\0", 1)[0]
    last_write_raw = struct.unpack_from("<Q", block, 0x0C)[0]
    return HiveHeader(
        primary_sequence=struct.unpack_from("<I", block, 0x04)[0],
        secondary_sequence=struct.unpack_from("<I", block, 0x08)[0],
        last_write_raw=last_write_raw,
        last_write=_filetime_to_datetime(last_write_raw),
        major_version=struct.unpack_from("<I", block, 0x14)[0],
        minor_version=struct.unpack_from("<I", block, 0x18)[0],
        file_type=struct.unpack_from("<I", block, 0x1C)[0],
        file_format=struct.unpack_from("<I", block, 0x20)[0],
        root_cell_offset=struct.unpack_from("<I", block, 0x24)[0],
        hive_bins_size=struct.unpack_from("<I", block, 0x28)[0],
        clustering_factor=struct.unpack_from("<I", block, 0x2C)[0],
        embedded_name=embedded_name,
        stored_checksum=struct.unpack_from("<I", block, CHECKSUM_OFFSET)[0],
        calculated_checksum=calculate_base_block_checksum(block),
    )


def find_transaction_logs(path: Path | str) -> tuple[Path, ...]:
    hive_path = Path(path)
    wanted = {
        f"{hive_path.name}.log".casefold(),
        f"{hive_path.name}.log1".casefold(),
        f"{hive_path.name}.log2".casefold(),
    }
    try:
        matches = [child for child in hive_path.parent.iterdir() if child.name.casefold() in wanted]
    except OSError:
        return ()
    return tuple(sorted((item.resolve() for item in matches if item.is_file()), key=str))


def inspect_hive(path: Path | str) -> HiveProvenance:
    hive_path = Path(path).resolve()
    with hive_path.open("rb") as handle:
        block = handle.read(BASE_BLOCK_SIZE)
    stat = hive_path.stat()
    return HiveProvenance(
        path=hive_path,
        size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        header=parse_hive_header(block),
        transaction_logs=find_transaction_logs(hive_path),
    )


def hash_file(
    path: Path | str,
    *,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
    chunk_size: int = 1024 * 1024,
) -> str:
    if chunk_size <= 0:
        raise ValueError("Hash chunk size must be positive")
    input_path = Path(path)
    total = input_path.stat().st_size
    processed = 0
    digest = hashlib.sha256()
    with input_path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            if cancelled is not None and cancelled():
                raise CancelledError("Hive hashing cancelled")
            digest.update(chunk)
            processed += len(chunk)
            if progress is not None:
                progress(processed, total)
    return digest.hexdigest()

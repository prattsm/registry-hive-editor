from __future__ import annotations

import struct
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reg_hive_gui.provenance import (
    BASE_BLOCK_SIZE,
    CHECKSUM_OFFSET,
    calculate_base_block_checksum,
    find_transaction_logs,
    hash_file,
    inspect_hive,
    parse_hive_header,
)


def _base_block(*, primary: int = 7, secondary: int = 7) -> bytearray:
    block = bytearray(BASE_BLOCK_SIZE)
    block[:4] = b"regf"
    struct.pack_into("<IIQIIIIIII", block, 4, primary, secondary, 116444736000000000, 1, 5, 0, 1, 0x20, 0x2000, 1)
    name = "SAM".encode("utf-16-le")
    block[0x30 : 0x30 + len(name)] = name
    struct.pack_into("<I", block, CHECKSUM_OFFSET, calculate_base_block_checksum(block))
    return block


def test_parse_hive_header_preserves_raw_metadata() -> None:
    block = _base_block()

    header = parse_hive_header(block)

    assert header.primary_sequence == 7
    assert header.secondary_sequence == 7
    assert header.sequence_consistent
    assert header.last_write == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert (header.major_version, header.minor_version) == (1, 5)
    assert header.root_cell_offset == 0x20
    assert header.hive_bins_size == 0x2000
    assert header.embedded_name == "SAM"
    assert header.checksum_valid


def test_inconsistent_sequence_and_checksum_are_reported() -> None:
    block = _base_block(primary=8, secondary=7)
    block[0x40] ^= 0xFF

    header = parse_hive_header(block)

    assert not header.sequence_consistent
    assert not header.checksum_valid


@pytest.mark.parametrize("block", [b"", b"regf", b"nope" + bytes(512)])
def test_invalid_base_blocks_are_rejected(block: bytes) -> None:
    with pytest.raises(ValueError):
        parse_hive_header(block)


def test_inspection_and_transaction_log_detection(workspace_tmp_path: Path) -> None:
    hive = workspace_tmp_path / "SAM"
    hive.write_bytes(_base_block())
    log1 = workspace_tmp_path / "SAM.LOG1"
    log2 = workspace_tmp_path / "sam.log2"
    unrelated = workspace_tmp_path / "OTHER.LOG1"
    log1.write_bytes(b"one")
    log2.write_bytes(b"two")
    unrelated.write_bytes(b"other")

    result = inspect_hive(hive)

    assert result.path == hive.resolve()
    assert result.size == BASE_BLOCK_SIZE
    assert result.header.embedded_name == "SAM"
    assert set(result.transaction_logs) == {log1.resolve(), log2.resolve()}
    assert find_transaction_logs(hive) == result.transaction_logs


def test_streaming_hash_reports_progress_and_cancellation(workspace_tmp_path: Path) -> None:
    path = workspace_tmp_path / "data"
    path.write_bytes(b"abcdefgh")
    progress: list[tuple[int, int]] = []

    digest = hash_file(path, chunk_size=3, progress=lambda done, total: progress.append((done, total)))

    assert digest == "9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab"
    assert progress == [(3, 8), (6, 8), (8, 8)]
    with pytest.raises(CancelledError):
        hash_file(path, chunk_size=1, cancelled=lambda: True)

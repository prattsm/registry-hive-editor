from __future__ import annotations

import json
from pathlib import Path

import pytest

from reg_hive_gui.annotations import Annotation, AnnotationStore

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def test_annotations_round_trip_by_hive_sha256(workspace_tmp_path: Path) -> None:
    store = AnnotationStore(workspace_tmp_path / "annotations")
    annotations = [
        Annotation(
            path="Software\\Example",
            note="Investigate this persistence entry",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-02T00:00:00+00:00",
        )
    ]

    output = store.save(DIGEST_A, annotations, source_path="evidence.hive")

    assert output.name == f"{DIGEST_A}.json"
    assert store.load(DIGEST_A) == annotations
    assert store.load(DIGEST_B) == []


def test_annotation_write_replaces_existing_json_atomically(workspace_tmp_path: Path) -> None:
    store = AnnotationStore(workspace_tmp_path)
    store.save(DIGEST_A, [Annotation("Old")])

    store.save(DIGEST_A, [Annotation("New", "note")])

    assert store.load(DIGEST_A) == [Annotation("New", "note")]
    assert not list(workspace_tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64, "a" * 63])
def test_invalid_annotation_identity_is_rejected(
    workspace_tmp_path: Path, digest: str
) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        AnnotationStore(workspace_tmp_path).path_for(digest)


def test_mismatched_or_duplicate_annotation_file_is_rejected(workspace_tmp_path: Path) -> None:
    store = AnnotationStore(workspace_tmp_path)
    output = store.path_for(DIGEST_A)
    output.write_text(
        json.dumps(
            {
                "hive_sha256": DIGEST_A,
                "annotations": [{"path": "Key"}, {"path": "key"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        store.load(DIGEST_A)

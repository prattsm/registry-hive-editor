"""Per-evidence bookmarks and notes stored outside the source hive."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .fileio import staged_output

MAX_ANNOTATIONS = 10_000
MAX_NOTE_LENGTH = 10_000
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def annotation_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RegistryHiveEditor" / "annotations"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reg_hive_gui" / "annotations"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Annotation:
    path: str
    note: str = ""
    created_at: str = ""
    updated_at: str = ""


class AnnotationStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or annotation_directory()

    def path_for(self, sha256: str) -> Path:
        digest = sha256.casefold()
        if SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("Annotation identity must be a complete SHA-256 digest")
        return self.directory / f"{digest}.json"

    def load(self, sha256: str) -> list[Annotation]:
        path = self.path_for(sha256)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("hive_sha256") != sha256.casefold():
            raise ValueError("Annotation file does not match this hive SHA-256")
        raw_annotations = payload.get("annotations")
        if not isinstance(raw_annotations, list) or len(raw_annotations) > MAX_ANNOTATIONS:
            raise ValueError("Annotation file has an invalid annotation list")
        annotations: list[Annotation] = []
        seen: set[str] = set()
        for raw in raw_annotations:
            if not isinstance(raw, dict):
                raise ValueError("Annotation file contains an invalid entry")
            values = {name: raw.get(name, "") for name in Annotation.__dataclass_fields__}
            if not all(isinstance(value, str) for value in values.values()):
                raise ValueError("Annotation fields must be text")
            annotation = Annotation(**values)
            if not annotation.path or len(annotation.note) > MAX_NOTE_LENGTH:
                raise ValueError("Annotation contains an invalid path or oversized note")
            if annotation.path.casefold() in seen:
                raise ValueError("Annotation file contains duplicate paths")
            seen.add(annotation.path.casefold())
            annotations.append(annotation)
        return annotations

    def save(
        self,
        sha256: str,
        annotations: list[Annotation],
        *,
        source_path: Path | str | None = None,
    ) -> Path:
        if len(annotations) > MAX_ANNOTATIONS:
            raise ValueError(f"Cannot store more than {MAX_ANNOTATIONS} annotations")
        if any(not item.path or len(item.note) > MAX_NOTE_LENGTH for item in annotations):
            raise ValueError("Cannot store an invalid annotation")
        if len({item.path.casefold() for item in annotations}) != len(annotations):
            raise ValueError("Cannot store duplicate annotation paths")
        output = self.path_for(sha256)
        payload = {
            "schema_version": 1,
            "hive_sha256": sha256.casefold(),
            "source_path": str(source_path) if source_path is not None else "",
            "annotations": [asdict(annotation) for annotation in annotations],
        }
        with staged_output(output) as temporary:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return output

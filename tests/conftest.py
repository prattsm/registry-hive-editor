"""Shared test configuration."""

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def workspace_tmp_path():
    """Provide a writable temp directory in restricted desktop environments."""
    root = Path(__file__).parent / "_tmp"
    path = root / uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

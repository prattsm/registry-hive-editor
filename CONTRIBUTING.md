# Contributing

Use Python 3.11 or newer and create a virtual environment. From the repository root:

```text
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -p no:cacheprovider
python -m compileall -q src scripts tests
python -m build
```

The GitHub Actions matrix runs these checks on Windows and Ubuntu with Python 3.11 through 3.14.

Tests use small in-memory backend doubles for malformed data and injected write failures. Real-hive
tests belong under the `integration` marker and must use redistributable synthetic fixtures or
create temporary hives through a documented platform API. Never commit customer, user, SAM,
SECURITY, SYSTEM, SOFTWARE, or NTUSER evidence.

Performance regressions should use the `performance` marker and assert bounded behavior rather than
fragile wall-clock thresholds. Preserve incremental search batches, one-pass tree child enumeration,
bounded comparison indexing, and streaming report output.

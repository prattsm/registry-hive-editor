from __future__ import annotations

import subprocess
from concurrent.futures import CancelledError
from pathlib import Path

import pytest

from reg_hive_gui.plugins import (
    MAX_PLUGIN_COLUMNS,
    MAX_PLUGIN_ROWS,
    Plugin,
    discover_plugins,
    execute_plugin,
    normalize_plugin_rows,
    run_plugin_subprocess,
)


class DummyHive:
    pass


def test_discovery_does_not_execute_plugin_code(workspace_tmp_path: Path) -> None:
    marker = workspace_tmp_path / "executed"
    plugin_path = workspace_tmp_path / "example.py"
    plugin_path.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('yes')",
                "PLUGIN_NAME = 'Deferred Example'",
                "PLUGIN_DESCRIPTION = 'Does not run during discovery'",
                "def analyze(hive):",
                "    return [{'result': 'ok'}]",
            ]
        ),
        encoding="utf-8",
    )

    plugins, errors = discover_plugins([(workspace_tmp_path, False)])

    assert not errors
    assert plugins == [
        Plugin(
            name="Deferred Example",
            description="Does not run during discovery",
            path=plugin_path.resolve(),
            trusted=False,
        )
    ]
    assert not marker.exists()

    assert execute_plugin(plugins[0], DummyHive()) == [{"result": "ok"}]
    assert marker.read_text(encoding="utf-8") == "yes"


def test_builtin_plugins_are_packaged_and_discoverable() -> None:
    import reg_hive_gui

    plugin_dir = Path(reg_hive_gui.__file__).with_name("builtin_plugins")
    plugins, errors = discover_plugins([(plugin_dir, True)])

    assert not errors
    assert {plugin.name for plugin in plugins} == {"Run Keys", "Services", "UserAssist"}
    assert all(plugin.trusted for plugin in plugins)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("this is not valid Python !!!", "invalid syntax"),
        ("PLUGIN_NAME = 'Missing analyze'", "must define analyze"),
        ("PLUGIN_NAME = 123\ndef analyze(hive): return []", "literal string"),
        ("PLUGIN_NAME = '  '\ndef analyze(hive): return []", "cannot be empty"),
    ],
)
def test_invalid_plugins_are_reported_without_importing(
    workspace_tmp_path: Path, source: str, message: str
) -> None:
    plugin_path = workspace_tmp_path / "invalid.py"
    plugin_path.write_text(source, encoding="utf-8")

    plugins, errors = discover_plugins([workspace_tmp_path])

    assert not plugins
    assert len(errors) == 1
    assert message in errors[0].message


def test_plugin_rows_are_normalized_to_safe_report_values() -> None:
    assert normalize_plugin_rows(
        [{"bytes": b"\xde\xad", "items": [1, "two"], "mapping": {"a": 1}}]
    ) == [{"bytes": "de ad", "items": '[1, "two"]', "mapping": '{"a": 1}'}]


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        ["not a row"],
        [{1: "non-text key"}],
        [{"unsupported": object()}],
        [{}] * (MAX_PLUGIN_ROWS + 1),
        [{str(index): index for index in range(MAX_PLUGIN_COLUMNS + 1)}],
    ],
)
def test_invalid_plugin_results_are_rejected(result: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_plugin_rows(result)


def test_plugin_subprocess_response_is_validated(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = Plugin("Example", "", workspace_tmp_path / "plugin.py")
    hive_path = workspace_tmp_path / "hive"

    class FakeProcess:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return self.stdout, ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess('[1, 2]'))
    with pytest.raises(RuntimeError, match="invalid response"):
        run_plugin_subprocess(plugin, hive_path)

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess('{"ok": false, "error": "failed safely"}', 1),
    )
    with pytest.raises(RuntimeError, match="failed safely"):
        run_plugin_subprocess(plugin, hive_path)

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess('{"ok": true, "rows": [{"value": 1}]}'),
    )
    assert run_plugin_subprocess(plugin, hive_path) == [{"value": 1}]


def test_plugin_subprocess_timeout_is_reported(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = Plugin("Example", "", workspace_tmp_path / "plugin.py")

    class TimedOutProcess:
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if self.returncode is not None:
                return "", ""
            raise subprocess.TimeoutExpired("plugin", timeout)

        def terminate(self) -> None:
            self.returncode = -1

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: TimedOutProcess())
    with pytest.raises(TimeoutError, match="0-second"):
        run_plugin_subprocess(plugin, workspace_tmp_path / "hive", timeout_seconds=0)


def test_plugin_subprocess_can_be_cancelled(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = Plugin("Example", "", workspace_tmp_path / "plugin.py")

    class RunningProcess:
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if self.returncode is None:
                raise subprocess.TimeoutExpired("plugin", timeout)
            return "", ""

        def terminate(self) -> None:
            self.returncode = -1

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: RunningProcess())
    with pytest.raises(CancelledError):
        run_plugin_subprocess(
            plugin,
            workspace_tmp_path / "hive",
            timeout_seconds=10,
            cancelled=lambda: True,
        )

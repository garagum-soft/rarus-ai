"""The frozen program stands in for ``python``, and every process it starts sees the components."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from rarus_app import __main__ as entry
from rarus_app import component_paths


@pytest.mark.parametrize(
    ("args", "rest"),
    [
        (["C:/agents/raven-code/run.py", "--acp"], ["C:/agents/raven-code/run.py", "--acp"]),
        (["-m", "raven", "acp", "--config", "x.json"], ["-m", "raven", "acp", "--config", "x.json"]),
        (["-P", "-m", "raven", "serve"], ["-m", "raven", "serve"]),
        (["-X", "utf8", "-u", "-c", "print(1)"], ["-c", "print(1)"]),
        (["tool.PY"], ["tool.PY"]),
    ],
)
def test_python_command_lines_are_recognised(args, rest) -> None:
    assert entry.python_call(args) == rest


@pytest.mark.parametrize("args", [[], ["--hidden"], ["--engine", "serve"], ["-m"], ["-P"], ["notes.txt"]])
def test_the_window_keeps_its_own_arguments(args) -> None:
    assert entry.python_call(args) is None


def test_a_script_runs_as_main_with_its_folder_importable(tmp_path, monkeypatch) -> None:
    (tmp_path / "helper.py").write_text("VALUE = 7\n", encoding="utf-8")
    out = tmp_path / "out.json"
    script = tmp_path / "run.py"
    script.write_text(
        "import sys, json, helper\n"
        f"json.dump({{'name': __name__, 'argv': sys.argv[1:], 'v': helper.VALUE}}, open({str(out)!r}, 'w'))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "helper", raising=False)

    entry.run_python([str(script), "--acp"])

    assert json.loads(out.read_text(encoding="utf-8")) == {"name": "__main__", "argv": ["--acp"], "v": 7}


def test_a_module_and_code_run_like_the_interpreter(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "probe_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    out = tmp_path / "out.txt"
    (pkg / "__main__.py").write_text(
        f"import sys\nopen({str(out)!r}, 'w').write(' '.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])

    entry.run_python(["-m", "probe_pkg", "acp", "--config", "c.json"])
    assert out.read_text(encoding="utf-8") == "acp --config c.json"

    entry.run_python(["-c", f"open({str(out)!r}, 'w').write(__name__)"])
    assert out.read_text(encoding="utf-8") == "__main__"


def _install(root: Path, cid: str, *, enabled: bool = True, env: dict | None = None) -> Path:
    folder = root / cid / "1.0"
    (folder / "site").mkdir(parents=True)
    (folder / component_paths.MARK_FILE).write_text(json.dumps({"id": cid, "env": env or {}}), encoding="utf-8")
    state = component_paths.read_state(root)
    state.setdefault("components", {})[cid] = {"dir": f"{cid}/1.0", "enabled": enabled}
    (root / component_paths.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    return folder


def test_active_components_go_first_on_the_path_and_set_their_environment(monkeypatch) -> None:
    root = component_paths.components_dir()
    browser = _install(root, "browser", env={"PLAYWRIGHT_BROWSERS_PATH": "{dir}/browsers", "RAVEN_BROWSER_CHANNEL": "chromium"})
    _install(root, "messengers", enabled=False)
    (browser / "site" / "extra").mkdir()
    (browser / "site" / "extra.pth").write_text("# note\nimport os\nextra\nmissing\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", ["bundle"])
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("RAVEN_BROWSER_CHANNEL", raising=False)

    folders = component_paths.activate()

    browser = browser.resolve()
    assert folders == [browser]
    assert sys.path == [str(browser / "site"), str(browser / "site" / "extra"), "bundle"]
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == f"{browser}/browsers"
    assert os.environ["RAVEN_BROWSER_CHANNEL"] == "chromium"


def test_a_folder_outside_the_components_or_without_its_mark_is_ignored(tmp_path, monkeypatch) -> None:
    root = component_paths.components_dir()
    root.mkdir(parents=True)
    (tmp_path / "evil" / "site").mkdir(parents=True)
    (tmp_path / "evil" / component_paths.MARK_FILE).write_text("{}", encoding="utf-8")
    (root / "half" / "site").mkdir(parents=True)
    state = {"components": {"evil": {"dir": "../evil"}, "half": {"dir": "half"}}}
    (root / component_paths.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(sys, "path", ["bundle"])

    assert component_paths.activate() == []
    assert sys.path == ["bundle"]


def test_no_components_folder_changes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", ["bundle"])

    assert component_paths.activate() == []
    assert sys.path == ["bundle"]


def test_a_frozen_program_execing_itself_continues_in_the_same_process(tmp_path, monkeypatch) -> None:
    """On Windows exec loses the windowed program's pipes, so an agent's re-exec runs in place."""
    out = tmp_path / "out.txt"
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(os, "execv", lambda path, args: calls.append((path, args)))
    monkeypatch.setattr(os, "execve", lambda path, args, env: calls.append((path, args)))
    monkeypatch.setattr(os, "_rarus_in_place", False, raising=False)
    monkeypatch.setattr(entry, "_activate_components", lambda: None)
    script = tmp_path / "run.py"
    code = f"open({str(out)!r}, 'w').write('in place')"
    script.write_text(
        "import os, sys\n"
        "os.execv('C:/Windows/notepad.exe', ['notepad'])\n"
        f"os.execv(sys.executable, [sys.executable, '-c', {code!r}])\n"
        "raise AssertionError('exec returned')\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as stop:
        entry.run_python([str(script)])

    assert stop.value.code == 0
    assert out.read_text(encoding="utf-8") == "in place"
    assert calls == [("C:/Windows/notepad.exe", ["notepad"])]


def test_on_windows_the_streams_speak_utf8(monkeypatch) -> None:
    """The code page could not print what EverOS prints, nor read a Russian prompt."""
    import io

    streams = {name: io.TextIOWrapper(io.BytesIO(), encoding="cp1251") for name in ("stdin", "stdout", "stderr")}
    monkeypatch.setattr(sys, "platform", "win32")
    for name, stream in streams.items():
        monkeypatch.setattr(sys, name, stream)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    entry._utf8_streams()

    assert {s.encoding for s in streams.values()} == {"utf-8"}
    assert os.environ["PYTHONIOENCODING"] == "utf-8"

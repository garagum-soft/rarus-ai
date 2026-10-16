"""Stopping a command or an agent ends everything it started, on Windows too.

Windows has no process group to signal. Ending the launcher alone -- cmd.exe
under ``shell=True``, an npm ``.cmd`` shim -- left the program it started
running: a stopped CLI agent raised ``AttributeError`` (``os.killpg``) in place
of its cancellation and went on spending its quota, and a closed ACP agent, a
reaped background command and a timed-out ``exec`` all outlived their stop.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from raven.utils import proc_tree

PY = sys.executable

#: A launcher that starts a long sleeper, writes its pid, and sleeps itself.
LAUNCHER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(120)\n"
)


def _write_launcher(tmp_path: Path) -> Path:
    script = tmp_path / "launcher.py"
    script.write_text(LAUNCHER, encoding="utf-8")
    return script


def _wait_for_pid(path: Path, within: float = 20.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


def _running(pid: int) -> bool:
    row = proc_tree.by_pid(proc_tree.read_table() or []).get(pid)
    return row is not None and "defunct" not in row.name


def _gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def leftovers():
    pids: list[int] = []
    yield pids
    for pid in pids:
        if _running(pid):
            proc_tree.signal_pid(pid, hard=True)


def test_kill_tree_ends_what_the_launcher_started(tmp_path: Path, leftovers) -> None:
    pidfile = tmp_path / "child.pid"
    launcher = subprocess.Popen([PY, str(_write_launcher(tmp_path)), str(pidfile)])
    child = _wait_for_pid(pidfile)
    leftovers.extend([launcher.pid, child])

    proc_tree.kill_tree(launcher.pid)
    launcher.wait(timeout=10)

    assert _gone(child), "the program the launcher started outlived the stop"


async def test_stopping_a_cli_agent_ends_its_tree_without_raising(tmp_path: Path, leftovers) -> None:
    from raven.agent.subagent.backends.cli_agent import CliAgentBackend

    pidfile = tmp_path / "child.pid"
    proc = await asyncio.create_subprocess_exec(
        PY, str(_write_launcher(tmp_path)), str(pidfile), start_new_session=True
    )
    child = await asyncio.to_thread(_wait_for_pid, pidfile)
    leftovers.extend([proc.pid, child])

    await CliAgentBackend._kill_process_group(proc, proc.pid)

    assert proc.returncode is not None
    assert await asyncio.to_thread(_gone, child)


def test_reaping_a_background_command_ends_its_tree(tmp_path: Path, leftovers, monkeypatch) -> None:
    from raven.agent.tools import background_exec

    pidfile = tmp_path / "child.pid"
    launcher = subprocess.Popen(
        [PY, str(_write_launcher(tmp_path)), str(pidfile)], **({"start_new_session": True} if os.name != "nt" else {})
    )
    child = _wait_for_pid(pidfile)
    leftovers.extend([launcher.pid, child])
    monkeypatch.setattr(background_exec, "status", lambda task_id: {"task_id": task_id, "pid": launcher.pid})
    monkeypatch.setattr(background_exec, "_alive", lambda task_id, pid: launcher.poll() is None)

    assert background_exec.reap("t1")
    launcher.wait(timeout=10)
    assert _gone(child)


async def test_a_timed_out_exec_ends_what_its_shell_started(tmp_path: Path, leftovers) -> None:
    from raven.sandbox.direct_executor import DirectExecutor

    pidfile = tmp_path / "child.pid"
    command = f'"{PY}" "{_write_launcher(tmp_path)}" "{pidfile}"'

    result = await DirectExecutor().exec(command, cwd=str(tmp_path), timeout=3)
    child = _wait_for_pid(pidfile)
    leftovers.append(child)

    assert "Timed out" in result.stderr
    assert await asyncio.to_thread(_gone, child)


async def test_closing_an_acp_agent_ends_what_its_shim_started(tmp_path: Path, leftovers) -> None:
    from raven.acp_client.client import AcpClient

    pidfile = tmp_path / "child.pid"
    script = _write_launcher(tmp_path)
    if os.name == "nt":
        shim = tmp_path / "agent.cmd"
        shim.write_text(f'@echo off\r\n"{PY}" "{script}" "{pidfile}"\r\n', encoding="utf-8")
        command = str(shim)
    else:
        command = f'"{PY}" "{script}" "{pidfile}"'
    client = await AcpClient.launch(name="shim", command=command, cwd=str(tmp_path))
    child = await asyncio.to_thread(_wait_for_pid, pidfile)
    leftovers.extend([client._proc.pid, child])

    await client.close()

    assert await asyncio.to_thread(_gone, child)

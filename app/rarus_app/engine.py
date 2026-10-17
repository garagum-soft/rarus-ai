"""Starting and stopping the engine the window shows.

The engine is ``rarus serve`` run as a child process. It binds 127.0.0.1 only, keeps its
session credentials in ``serve.json`` (port, token, pid; mode 0600), and mints a one-time
nonce on request: the window opens ``/auth#<nonce>`` and the page trades the nonce for an
HttpOnly cookie, so the long-lived token never reaches the page. This module only drives
those documented pieces; it does not reimplement them.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from rarus_app.paths import app_home, logs_dir

DEFAULT_PORT = 18792
START_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 10.0
_HEALTH_SERVICE = "raven-serve"


class EngineError(RuntimeError):
    """The engine did not start, or stopped answering."""


@dataclass(frozen=True)
class EngineState:
    port: int
    token: str
    pid: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def free_port() -> int:
    """A port nobody holds right now (the engine probes forward if it is taken meanwhile)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def engine_command(port: int, *, python: str | None = None) -> list[str]:
    """The argv that runs the engine.

    A frozen build has no ``python``: the executable itself takes ``--engine`` and runs the
    command line behind it (see ``rarus_app.__main__``).
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--engine", "serve", "--port", str(port)]
    return [python or sys.executable, "-m", "rarus_app", "--engine", "serve", "--port", str(port)]


def serve_state_path() -> Path:
    return app_home() / "serve.json"


def read_state(path: Path | None = None) -> EngineState | None:
    """The recorded gateway, or None when the file is absent or unreadable."""
    try:
        data = json.loads((path or serve_state_path()).read_text(encoding="utf-8"))
        state = EngineState(port=int(data["port"]), token=str(data["token"]), pid=int(data["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return state if state.port > 0 and state.token else None


class Engine:
    """One engine child process and the way into its page."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        port: int | None = None,
        env: dict[str, str] | None = None,
        start_timeout_s: float = START_TIMEOUT_S,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.port = port or free_port()
        self._command = list(command) if command is not None else engine_command(self.port)
        self._env = {**os.environ, **(env or {})}
        self._start_timeout_s = start_timeout_s
        self._popen = popen
        self._proc: subprocess.Popen | None = None
        self._log: Any = None
        self.state: EngineState | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> EngineState:
        if self._proc is not None:
            raise EngineError("the engine was already started")
        stale = serve_state_path()
        stale.unlink(missing_ok=True)  # a leftover file would name a dead process's port
        self._log = open(logs_dir() / "engine.log", "ab")  # noqa: SIM115 - closed in stop()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self._proc = self._popen(
            self._command,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=self._env,
            creationflags=flags,
        )
        self.state = self._wait_ready(self._proc)
        return self.state

    def _wait_ready(self, proc: subprocess.Popen) -> EngineState:
        deadline = time.monotonic() + self._start_timeout_s
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                raise EngineError(f"the engine exited with code {code} before it was ready (see engine.log)")
            state = read_state()
            # Not ``state.pid == proc.pid``: a venv's python.exe on Windows is a launcher that
            # starts the real interpreter, so the pid in the file is the grandchild's. The file
            # was deleted before the start, so any file here was written by this launch.
            if state is not None and self._healthy(state):
                return state
            time.sleep(0.2)
        self.stop()
        raise EngineError("the engine did not become ready in time (see engine.log)")

    @staticmethod
    def _healthy(state: EngineState) -> bool:
        try:
            reply = httpx.get(f"{state.base_url}/health", timeout=2.0)
            return reply.status_code == 200 and reply.json().get("service") == _HEALTH_SERVICE
        except (httpx.HTTPError, ValueError):
            return False

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        recorded = read_state()
        child = (self.state or recorded).pid if (self.state or recorded) else None
        if proc is not None:
            _end_tree(proc, extra_pid=child)
        if self._log is not None:
            self._log.close()
            self._log = None
        if recorded is not None and child == recorded.pid:
            serve_state_path().unlink(missing_ok=True)
        self.state = None

    # -- the way in ------------------------------------------------------------

    def auth_url(self) -> str:
        """A one-time address that signs the window in; each call mints a fresh nonce."""
        if self.state is None:
            raise EngineError("the engine is not running")
        reply = httpx.post(
            f"{self.state.base_url}/auth/nonce",
            headers={"X-Raven-Token": self.state.token},
            timeout=5.0,
        )
        if reply.status_code != 200:
            raise EngineError(f"the engine refused to mint a sign-in nonce ({reply.status_code})")
        return f"{self.state.base_url}/auth#{reply.json()['nonce']}"


def _end_tree(proc: subprocess.Popen, *, extra_pid: int | None) -> None:
    """End the engine and anything it started.

    On Windows ``terminate`` stops only the process it is given, and the engine may be that
    process's child (see ``_wait_ready``), so the whole tree is asked to go; elsewhere a
    terminate is enough, with a kill after the grace period.
    """
    pids = {proc.pid, *([extra_pid] if extra_pid else [])}
    if sys.platform == "win32":
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        for pid in pids:
            subprocess.run(
                [taskkill, "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
    elif proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(STOP_TIMEOUT_S)

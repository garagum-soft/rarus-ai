"""Where the shell keeps its own files: the same home the engine uses."""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "RARUS_HOME"


def app_home() -> Path:
    """``$RARUS_HOME`` or ``~/.rarus`` -- the engine's home, so one folder holds everything."""
    override = os.environ.get(HOME_ENV, "").strip()
    return Path(override) if override else Path.home() / ".rarus"


def logs_dir() -> Path:
    path = app_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def shell_state_path() -> Path:
    return app_home() / "shell.json"

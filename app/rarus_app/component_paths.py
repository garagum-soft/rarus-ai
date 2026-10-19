"""Put the installed components where every process of the program finds them.

A component (rarus_app/components.py) is unpacked into ``<home>/components/<id>/<version>/``:
``site/`` holds its Python packages, and ``component.json`` names the environment variables it
needs, ``{dir}`` standing for that folder (the browser points ``PLAYWRIGHT_BROWSERS_PATH`` at
``{dir}/browsers``). ``state.json`` beside them says which ones are installed and switched on.

``activate`` runs first in every process the frozen program starts -- the engine, an agent, the
memory server -- before anything else is imported, so they all see the same packages. Component
folders go in front of the program's own: a build only ever puts a package into a component when
the program does not carry it whole (packaging/components/build.py), at the version the program was
built with. Standard library only: nothing may be imported before the paths are set.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rarus_app.paths import app_home

STATE_FILE = "state.json"
MARK_FILE = "component.json"


def components_dir(home: Path | None = None) -> Path:
    return (home or app_home()) / "components"


def read_state(root: Path) -> dict:
    try:
        data = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def active_folders(root: Path) -> list[Path]:
    """The folders of the components that are installed, switched on and still on disk, in catalogue order."""
    entries = read_state(root).get("components")
    if not isinstance(entries, dict):
        return []
    found = []
    for entry in entries.values():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        rel = str(entry.get("dir") or "")
        folder = (root / rel).resolve()
        if not rel or root.resolve() not in folder.parents or not (folder / MARK_FILE).is_file():
            continue
        found.append(folder)
    return found


def _pth_paths(site: Path) -> list[str]:
    """Plain path lines of the ``.pth`` files in a component's site folder; ``import`` lines are not run."""
    paths = []
    for pth in sorted(site.glob("*.pth")):
        try:
            lines = pth.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", "import ", "import\t")):
                continue
            candidate = (site / line).resolve()
            if candidate.is_dir():
                paths.append(str(candidate))
    return paths


def activate(home: Path | None = None) -> list[Path]:
    """Prepend the active components' packages to ``sys.path`` and set their environment; answers the folders."""
    root = components_dir(home)
    folders = active_folders(root)
    front: list[str] = []
    for folder in folders:
        site = folder / "site"
        if site.is_dir():
            front.append(str(site))
            front.extend(_pth_paths(site))
        try:
            mark = json.loads((folder / MARK_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mark = {}
        env = mark.get("env") if isinstance(mark, dict) else None
        for key, value in (env or {}).items():
            if isinstance(key, str) and isinstance(value, str) and key.isupper():
                os.environ[key] = value.replace("{dir}", str(folder))
    sys.path[:0] = [p for p in front if p not in sys.path]
    return folders

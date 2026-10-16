"""The record a scenario run leaves: what it ran, what came back, what it sent and received.

One directory per run under ``<RAVEN_HOME>/scenario_runs/<run id>/``: an
append-only ``journal.jsonl`` of events, ``meta.json`` (name, arguments,
status, timing) rewritten as the run moves, and ``result.md``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from raven.utils.atomic_io import atomic_replace, locked_append


def default_runs_dir() -> Path:
    from raven.home import raven_home

    return raven_home() / "scenario_runs"


class Journal:
    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta: dict[str, Any] = {"id": run_id}

    def event(self, name: str, /, **data: Any) -> None:
        locked_append(
            self.dir / "journal.jsonl", [json.dumps({"at": time.time(), "event": name, **data}, ensure_ascii=False)]
        )

    def meta(self, **fields: Any) -> None:
        self._meta.update(fields)
        atomic_replace(self.dir / "meta.json", json.dumps(self._meta, ensure_ascii=False, indent=2))

    def result(self, text: str) -> None:
        atomic_replace(self.dir / "result.md", text)


def recent(root: Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Run metadata, newest first."""
    base = root or default_runs_dir()
    if not base.is_dir():
        return []
    out = []
    for meta in base.glob("*/meta.json"):
        try:
            out.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda m: m.get("started_at", 0), reverse=True)
    return out[:limit]


def events(run_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    path = (root or default_runs_dir()) / run_id / "journal.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


__all__ = ["Journal", "default_runs_dir", "events", "recent"]

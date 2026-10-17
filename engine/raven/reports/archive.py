"""The run archive: one line per finished run, kept after the run's own record is gone.

``<RARUS_HOME>/reports/runs.jsonl``, append-only. A run is written once, the
first time a report or a listing sees it finished; a run still in flight is not
archived until it ends. The line is the run's summary (status, agents, nodes,
failures, cost-relevant counts, where its record was), so a conversation that
retention later deleted still has its runs listed here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from raven.reports.runs import RunEntry
from raven.utils.atomic_io import locked_append, write_transaction


def default_root() -> Path:
    from raven.home import raven_home

    return raven_home() / "reports"


class Archive:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.path = self.root / "runs.jsonl"

    def rows(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def update(self, entries: Iterable[RunEntry]) -> int:
        """Archive the finished entries not archived yet; the number added."""
        self.root.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            known = {(r.get("kind"), r.get("id"), r.get("session_key")) for r in self.rows()}
            new = [
                json.dumps(e.to_dict(), ensure_ascii=False)
                for e in entries
                if e.finished and (e.kind, e.id, e.session_key) not in known
            ]
            if new:
                locked_append(self.path, new)
        return len(new)

    def find(self, run_id: str) -> dict[str, Any] | None:
        for row in reversed(self.rows()):
            if row.get("id") == run_id:
                return row
        return None


__all__ = ["Archive", "default_root"]

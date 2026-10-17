"""Read and write the reporting structure (``subagents.org``) on the on-disk config.

The agent canvas edits the chart as a whole: it sends every row, the rows are checked as a
forest (no cycle, no manager outside the chart, no one reporting to themselves, at most
``MAX_DEPTH`` levels) and only then replace what the file holds. A refused chart changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.org import OrgChart, OrgChartError
from raven.config.loader import get_config_path, read_raw_or_raise
from raven.utils.atomic_io import atomic_update

#: Levels from a head down to its deepest report, a head being level 1 (spec FR-161).
MAX_DEPTH = 6


class OrgRefusedError(ValueError):
    """The rows cannot be saved; the message says why in plain words."""


def _row(raw: Mapping[str, Any]) -> dict[str, Any]:
    manager = raw.get("reports_to", raw.get("reportsTo"))
    return {
        "name": str(raw.get("name") or "").strip(),
        "reports_to": str(manager).strip() or None if manager else None,
        "department": str(raw.get("department") or ""),
        "title": str(raw.get("title") or ""),
    }


def read_org(*, config_path: Path | None = None) -> list[dict[str, Any]]:
    """The rows as the file has them, in the snake_case the wire uses."""
    data = read_raw_or_raise(config_path or get_config_path())
    subagents = data.get("subagents")
    rows = subagents.get("org") if isinstance(subagents, dict) else None
    return [_row(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def check(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The rows cleaned and verified, or :class:`OrgRefusedError`."""
    clean = [_row(r) for r in rows]
    if any(not r["name"] for r in clean):
        raise OrgRefusedError("every row needs a name")
    try:
        chart = OrgChart.from_rows(clean)
    except OrgChartError as exc:
        raise OrgRefusedError(str(exc)) from exc
    for member in chart.members():
        levels = len(chart.chain_of_command(member.name)) + 1
        if levels > MAX_DEPTH:
            raise OrgRefusedError(f"{member.name!r} would sit {levels} levels deep; the chart allows {MAX_DEPTH}")
    return clean


def write_org(rows: Iterable[Mapping[str, Any]], *, config_path: Path | None = None) -> list[dict[str, Any]]:
    """Replace ``subagents.org`` with the verified rows. Returns the rows as written."""
    clean = check(rows)
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        section = data.setdefault("subagents", {})
        section["org"] = [
            {"name": r["name"], "reportsTo": r["reports_to"], "department": r["department"], "title": r["title"]}
            for r in clean
        ]
        return json.dumps(data, indent=2, ensure_ascii=False), None

    atomic_update(path, _apply)
    logger.info("org_store: org chart written ({} rows)", len(clean))
    return clean

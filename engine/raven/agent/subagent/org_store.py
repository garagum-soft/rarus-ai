"""Read and write the reporting structure (``subagents.org``) on the on-disk config.

The agent canvas edits the chart as a whole: it sends every row, the rows are checked as a
forest (no cycle, no manager outside the chart, no one reporting to themselves, at most
``MAX_DEPTH`` levels) and only then replace what the file holds. A refused chart changes nothing.

The companies and departments the rows are grouped into (``subagents.companies``) travel with
the rows and are checked with them: a member's company and department must exist, a head must
work where it heads, and a company's logo is a small picture kept in the config itself.
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
#: A logo is a data URL kept in the config file; the canvas shrinks pictures to 128 px before sending.
MAX_LOGO_CHARS = 400_000
MAX_COMPANY_NAME = 80


class OrgRefusedError(ValueError):
    """The rows cannot be saved; the message says why in plain words."""


def _row(raw: Mapping[str, Any]) -> dict[str, Any]:
    manager = raw.get("reports_to", raw.get("reportsTo"))
    return {
        "name": str(raw.get("name") or "").strip(),
        "reports_to": str(manager).strip() or None if manager else None,
        "department": str(raw.get("department") or ""),
        "title": str(raw.get("title") or ""),
        "company": str(raw.get("company") or ""),
        "avatar": str(raw.get("avatar") or ""),
    }


def _company(raw: Mapping[str, Any]) -> dict[str, Any]:
    departments = raw.get("departments") if isinstance(raw.get("departments"), list) else []
    return {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "logo": str(raw.get("logo") or ""),
        "head": str(raw.get("head") or ""),
        "frozen": bool(raw.get("frozen")),
        "background": str(raw.get("background") or ""),
        "departments": [
            {
                "name": str(d.get("name") or "").strip(),
                "head": str(d.get("head") or ""),
                "background": str(d.get("background") or ""),
            }
            for d in departments
            if isinstance(d, Mapping)
        ],
    }


def _subagents(path: Path) -> dict[str, Any]:
    data = read_raw_or_raise(path)
    section = data.get("subagents")
    return section if isinstance(section, dict) else {}


def read_org(*, config_path: Path | None = None) -> list[dict[str, Any]]:
    """The rows as the file has them, in the snake_case the wire uses."""
    rows = _subagents(config_path or get_config_path()).get("org")
    return [_row(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def read_companies(*, config_path: Path | None = None) -> list[dict[str, Any]]:
    """The companies with their departments, as the file has them."""
    rows = _subagents(config_path or get_config_path()).get("companies")
    return [_company(c) for c in rows if isinstance(c, dict)] if isinstance(rows, list) else []


def check_companies(companies: Iterable[Mapping[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The companies cleaned and verified against the member rows, or :class:`OrgRefusedError`."""
    clean = [_company(c) for c in companies]
    by_id: dict[str, dict[str, Any]] = {}
    for company in clean:
        if not company["id"]:
            raise OrgRefusedError("every company needs an id")
        if company["id"] in by_id:
            raise OrgRefusedError(f"two companies share the id {company['id']!r}")
        if not company["name"] or len(company["name"]) > MAX_COMPANY_NAME:
            raise OrgRefusedError(f"a company's name is 1 to {MAX_COMPANY_NAME} characters")
        logo = company["logo"]
        if logo and (not logo.startswith("data:image/") or len(logo) > MAX_LOGO_CHARS):
            raise OrgRefusedError(f"the logo of {company['name']!r} must be a small picture (data:image/..., under 300 KB)")
        names = [d["name"] for d in company["departments"]]
        if any(not n for n in names):
            raise OrgRefusedError(f"every department of {company['name']!r} needs a name")
        if len(set(names)) != len(names):
            raise OrgRefusedError(f"{company['name']!r} has two departments with one name")
        by_id[company["id"]] = company
    member = {r["name"]: r for r in rows}
    for row in rows:
        if not row["company"]:
            continue
        company = by_id.get(row["company"])
        if company is None:
            raise OrgRefusedError(f"{row['name']!r} is in a company that is not there")
        if row["department"] and row["department"] not in {d["name"] for d in company["departments"]}:
            raise OrgRefusedError(f"{row['name']!r} is in department {row['department']!r}, which {company['name']!r} has not got")
    for company in by_id.values():
        head = company["head"]
        if head and member.get(head, {}).get("company") != company["id"]:
            raise OrgRefusedError(f"the head of {company['name']!r} must work in it")
        for department in company["departments"]:
            head = department["head"]
            row = member.get(head, {}) if head else {}
            if head and (row.get("company") != company["id"] or row.get("department") != department["name"]):
                raise OrgRefusedError(f"the head of {department['name']!r} must work in that department")
    return clean


def check(rows: Iterable[Mapping[str, Any]], companies: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
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
    check_companies(companies, clean)
    return clean


def write_org(
    rows: Iterable[Mapping[str, Any]],
    companies: Iterable[Mapping[str, Any]] | None = None,
    *,
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Replace ``subagents.org`` (and ``subagents.companies`` when given) with the verified rows.

    Returns the rows as written. Leaving ``companies`` out keeps the ones the file has, so a
    caller that only knows the reporting lines does not wipe the companies.
    """
    path = config_path or get_config_path()
    kept = read_companies(config_path=path) if companies is None else list(companies)
    clean = check(rows, kept)
    groups = check_companies(kept, clean)

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        section = data.setdefault("subagents", {})
        section["org"] = [
            {
                "name": r["name"],
                "reportsTo": r["reports_to"],
                "department": r["department"],
                "title": r["title"],
                "company": r["company"],
                "avatar": r["avatar"],
            }
            for r in clean
        ]
        section["companies"] = [
            {
                "id": c["id"],
                "name": c["name"],
                "logo": c["logo"],
                "head": c["head"],
                "frozen": c["frozen"],
                "background": c["background"],
                "departments": c["departments"],
            }
            for c in groups
        ]
        return json.dumps(data, indent=2, ensure_ascii=False), None

    atomic_update(path, _apply)
    logger.info("org_store: org chart written ({} rows, {} companies)", len(clean), len(groups))
    return clean

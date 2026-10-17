"""Areas of responsibility: a roster agent that owns a part of a project, not a single task.

An area names an owner (a roster agent), what it covers -- path patterns in a
project and topic words -- and a charter: the standing instructions for what
the owner looks after and what "good" means there. Areas live in
``<RARUS_HOME>/areas.json`` and outlive every conversation.

What makes an owner more than an agent called on demand:

- **who** -- a path or a piece of text finds its owner (the most specific path
  pattern first, then topic words), so work can be routed to whoever owns it;
- **a journal** -- every run in an area is appended to
  ``<RARUS_HOME>/areas/<name>.jsonl``, and the next run is handed the charter
  and the latest entries, so the owner picks up where it left off in any
  conversation, on any day;
- **a duty** -- an area with ``every_s`` set is looked after on its own: a
  resident pass in the gateway runs the owner's standing duty when it is due,
  and a line of its report that starts with ``ATTENTION:`` is raised on the
  operator's attention queue.

Nothing runs until an area is assigned (``raven areas assign``); an area
without ``every_s`` runs only when asked.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from raven.utils.atomic_io import atomic_replace, write_transaction

logger = logging.getLogger(__name__)

MIN_EVERY_S = 600
TICK_S = 60.0
JOURNAL_IN_BRIEF = 5
_EXCERPT = 2000
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ATTENTION = re.compile(r"^\s*ATTENTION:\s*(.+)$", re.M)

DUTY = (
    "Do your standing duty for this area: look over what it covers for anything that changed or needs "
    "care since your last look, fix what is yours to fix within your charter, and leave alone what is "
    "not. Finish with a short report of what you checked and did. Start a line with 'ATTENTION:' for "
    "anything a person has to look at; write no such line when nothing needs them."
)


class AreaError(ValueError):
    """An area that cannot be assigned as given."""


@dataclass
class Area:
    name: str
    agent: str
    project: str = ""
    """The checkout the owner works in; empty is the workspace a run is given."""
    paths: list[str] = field(default_factory=list)
    """Glob patterns, relative to ``project`` (``raven/routing/**``)."""
    topics: list[str] = field(default_factory=list)
    charter: str = ""
    every_s: int = 0
    """Seconds between standing duties; 0 runs the area only when asked."""
    last_run_at: float = 0.0
    last_status: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Area:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def due(self, now: float) -> bool:
        return self.every_s > 0 and now - self.last_run_at >= max(self.every_s, MIN_EVERY_S)


def check_area(area: Area) -> Area:
    """The area as it will be stored, or why it cannot be."""
    area.name = area.name.strip().lower()
    if not _NAME.match(area.name):
        raise AreaError(f"{area.name!r}: an area name is lowercase letters, digits, '-' or '_', up to 48")
    if not area.agent.strip():
        raise AreaError("an area needs an owner (a roster agent)")
    area.paths = [p.strip().replace("\\", "/") for p in area.paths if p.strip()]
    area.topics = [t.strip().lower() for t in area.topics if t.strip()]
    if not area.paths and not area.topics and not area.charter.strip():
        raise AreaError("an area needs something to cover: a path pattern, a topic or a charter")
    if area.every_s and area.every_s < MIN_EVERY_S:
        raise AreaError(f"a standing duty runs at most every {MIN_EVERY_S}s")
    return area


# -- the store -------------------------------------------------------------------------------


def default_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "areas.json"


class AreaStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    @property
    def journal_dir(self) -> Path:
        return self.path.parent / "areas"

    def areas(self) -> list[Area]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Area.from_dict(a) for a in raw.get("areas") or [] if isinstance(a, dict)]

    def get(self, name: str) -> Area | None:
        return next((a for a in self.areas() if a.name == name.strip().lower()), None)

    def _save(self, areas: list[Area]) -> None:
        atomic_replace(self.path, json.dumps({"areas": [asdict(a) for a in areas]}, ensure_ascii=False, indent=2))

    def put(self, area: Area) -> None:
        check_area(area)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            rest = [a for a in self.areas() if a.name != area.name]
            self._save([*rest, area])

    def remove(self, name: str) -> bool:
        if not self.path.exists():
            return False
        with write_transaction(self.path):
            areas = self.areas()
            rest = [a for a in areas if a.name != name.strip().lower()]
            if len(rest) == len(areas):
                return False
            self._save(rest)
        return True

    def stamp(self, name: str, *, at: float, status: str) -> None:
        """Record a run on the stored area, unless it was removed meanwhile."""
        with write_transaction(self.path):
            areas = self.areas()
            for area in areas:
                if area.name == name:
                    area.last_run_at, area.last_status = at, status
                    self._save(areas)
                    return

    # -- the journal --

    def journal(self, name: str, limit: int = 0) -> list[dict[str, Any]]:
        try:
            lines = (self.journal_dir / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[-limit:] if limit else out

    def note(self, name: str, entry: dict[str, Any]) -> None:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        with (self.journal_dir / f"{name}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -- who owns what ---------------------------------------------------------------------------


def _specificity(pattern: str) -> int:
    """How much of a pattern is literal: the longer, the narrower the area."""
    return len(re.split(r"[*?\[]", pattern, maxsplit=1)[0])


def _relative(path: str, project: str) -> str:
    p = path.replace("\\", "/")
    if project:
        root = project.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(root.lower()):
            p = p[len(root) :]
    return p.lstrip("./") if p.startswith("./") else p


def owners_of_path(areas: list[Area], path: str) -> list[Area]:
    """The areas whose path patterns match ``path``, the narrowest first."""
    hits: list[tuple[int, Area]] = []
    for area in areas:
        rel = _relative(path, area.project)
        best = max(
            (
                _specificity(p)
                for p in area.paths
                if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/*") + "/*")
            ),
            default=-1,
        )
        if best >= 0:
            hits.append((best, area))
    return [a for _, a in sorted(hits, key=lambda h: (-h[0], h[1].name))]


def owners_of_text(areas: list[Area], text: str) -> list[Area]:
    """The areas a piece of text is about: named paths first, then topic words and the area's own name."""
    by_path: list[Area] = []
    for token in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]+|[\w-]+/[\w./-]+", text or ""):
        for area in owners_of_path(areas, token):
            if area not in by_path:
                by_path.append(area)
    words = set(re.findall(r"[\w-]+", (text or "").lower()))
    scored = []
    for area in areas:
        if area in by_path:
            continue
        hits = sum(1 for t in area.topics if (t in words if " " not in t else t in (text or "").lower()))
        hits += area.name in words
        if hits:
            scored.append((hits, area))
    return by_path + [a for _, a in sorted(scored, key=lambda s: (-s[0], s[1].name))]


# -- a run in an area ------------------------------------------------------------------------


def brief(area: Area, journal: list[dict[str, Any]]) -> str:
    """What an owner is handed before any task in its area: the area, the charter, what it did lately."""
    lines = [f"You own the area '{area.name}'" + (f" in {area.project}" if area.project else "") + "."]
    if area.paths:
        lines.append("It covers: " + ", ".join(area.paths))
    if area.topics:
        lines.append("Topics: " + ", ".join(area.topics))
    if area.charter.strip():
        lines.append(f"Your standing charter:\n{area.charter.strip()}")
    if journal:
        lines.append("What you did lately in this area (newest last):")
        for entry in journal[-JOURNAL_IN_BRIEF:]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("at") or 0))
            said = (entry.get("report") or entry.get("error") or "").strip().splitlines()
            lines.append(
                f"- {when} {entry.get('kind', 'task')}: {entry.get('task', '')[:120]} -> {said[0][:200] if said else '(nothing)'}"
            )
    else:
        lines.append("This is your first run in this area.")
    return "\n".join(lines)


_AREA_HEAD = re.compile(r"^You own the area '([a-z0-9][a-z0-9_-]*)'", re.M)


def spawn_brief(agent: str, text: str, store: AreaStore | None = None) -> str:
    """The brief to put before a task handed to ``agent``, when it is work in an area that agent owns.

    Empty when the agent owns no area or the task is not about one -- a spawn
    to an owner is unchanged unless it lands in what the owner looks after.
    Never raises: a store that cannot be read costs the brief, not the spawn.
    """
    try:
        store = store or AreaStore()
        mine = [a for a in store.areas() if a.agent == agent]
        hits = owners_of_text(mine, text) if mine else []
        if not hits:
            return ""
        return f"{brief(hits[0], store.journal(hits[0].name))}\n\nThe task below is work in this area.\n\n"
    except Exception:  # noqa: BLE001
        logger.debug("areas: no brief for %s", agent, exc_info=True)
        return ""


def note_spawn_result(task: str, summary: str, result: str, status: str, store: AreaStore | None = None) -> bool:
    """Journal a spawned task that carried an area's brief; False when it carried none."""
    found = _AREA_HEAD.search(task or "")
    if not found:
        return False
    try:
        store = store or AreaStore()
        area = store.get(found.group(1))
        if area is None:
            return False
        ok = status == "ok"
        entry = {"at": time.time(), "kind": "assigned", "task": summary or "(untitled)", "agent": area.agent, "ok": ok}
        entry["report" if ok else "error"] = str(result or "")[:_EXCERPT]
        store.note(area.name, entry)
        store.stamp(area.name, at=entry["at"], status="ok" if ok else "failed")
        return True
    except Exception:  # noqa: BLE001 -- journaling never costs the announcement
        logger.debug("areas: could not journal a spawn", exc_info=True)
        return False


def attention_lines(report: str) -> list[str]:
    return [m.strip() for m in _ATTENTION.findall(report or "") if m.strip()]


RunIn = Callable[[str, str, Path], Awaitable[str]]


async def run_area(
    store: AreaStore,
    area: Area,
    run_in: RunIn,
    *,
    task: str = "",
    workspace: Path | None = None,
    queue: Any = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One run of the owner in its area -- ``task``, or its standing duty -- journaled and reported.

    ``run_in(agent, prompt, workspace)`` is the manager's own; a failure is
    journaled and returned, never raised, so a resident pass keeps going.
    """
    kind = "task" if task.strip() else "duty"
    where = Path(area.project) if area.project else (workspace or Path.cwd())
    prompt = f"{brief(area, store.journal(area.name))}\n\n{'Your task now' if task.strip() else 'Now'}:\n{task.strip() or DUTY}"
    began = now()
    entry: dict[str, Any] = {"at": began, "kind": kind, "task": task.strip() or "standing duty", "agent": area.agent}
    try:
        report = str(await run_in(area.agent, prompt, where) or "")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a failed run is a journal entry, not a crash
        entry.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:_EXCERPT], seconds=round(now() - began, 1))
        store.note(area.name, entry)
        store.stamp(area.name, at=began, status="failed")
        return entry
    flags = attention_lines(report)
    entry.update(ok=True, report=report[:_EXCERPT], attention=flags, seconds=round(now() - began, 1))
    store.note(area.name, entry)
    store.stamp(area.name, at=began, status="attention" if flags else "ok")
    if flags:
        _raise(area, flags, queue)
    return entry


def _raise(area: Area, flags: list[str], queue: Any) -> None:
    from raven.proactive_engine import attention

    q = queue or attention.queue()
    for flag in flags:
        digest = hashlib.sha1(flag.encode("utf-8")).hexdigest()[:10]
        try:
            q.raise_item(
                f"area:{area.name}:{digest}",
                kind="area_report",
                title=f"{area.name}: {flag[:160]}",
                detail=f"Reported by {area.agent}, owner of the area '{area.name}'.",
                source="areas",
                data={"area": area.name, "agent": area.agent},
            )
        except Exception:  # noqa: BLE001 -- the report is journaled either way
            logger.warning("areas: could not raise %s on the attention queue", area.name)


# -- the resident duty -----------------------------------------------------------------------


class AreaWatch:
    """The gateway's pass over the areas with a standing duty: one due area at a time, every minute."""

    def __init__(self, store: AreaStore, run_in: Callable[[], RunIn | None], *, tick_s: float = TICK_S) -> None:
        self.store = store
        self._run_in = run_in
        self.tick_s = tick_s
        self._task: asyncio.Task | None = None

    async def once(self, now: float | None = None) -> list[dict[str, Any]]:
        when = time.time() if now is None else now
        run_in = self._run_in()
        if run_in is None:
            return []
        done = []
        for area in self.store.areas():
            if area.due(when):
                done.append(await run_area(self.store, area, run_in))
        return done

    async def _loop(self) -> None:
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a bad pass is logged; the next one runs
                logger.exception("area duty pass failed")
            await asyncio.sleep(self.tick_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="area-watch")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def parse_every(text: str) -> int:
    """``90m``, ``6h``, ``1d`` or plain seconds; ``0`` / ``off`` for none."""
    raw = (text or "").strip().lower()
    if raw in ("", "0", "off", "none"):
        return 0
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", raw)
    if not match:
        raise AreaError(f"{text!r}: give the interval as 90m, 6h, 1d or seconds")
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


__all__ = [
    "DUTY",
    "MIN_EVERY_S",
    "Area",
    "AreaError",
    "AreaStore",
    "AreaWatch",
    "attention_lines",
    "brief",
    "check_area",
    "owners_of_path",
    "owners_of_text",
    "note_spawn_result",
    "parse_every",
    "run_area",
    "spawn_brief",
]

"""Ideation and a roadmap: agents propose ideas per category, and the ideas become phases.

Each category -- features, quality, performance, security, documentation by
default -- is put to an agent on its own, concurrently, with the project in
front of it, and comes back as a list of ideas, each with why it matters, an
effort (S, M, L) and an impact (1 to 5). The roadmap is then laid out from the
ideas themselves: ranked by impact over effort, cut into phases (Now, Next,
Later), and every category keeps its own slice of every phase. Progress is an
event stream with a phase name and a percentage, so a caller can show it as it
goes.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from json_repair import repair_json

DEFAULT_CATEGORIES = ("features", "quality", "performance", "security", "documentation")
PHASES = ("Now", "Next", "Later")
EFFORT_WEIGHT = {"S": 1, "M": 2, "L": 3}

Ask = Callable[[str, str], Awaitable[str]]
OnEvent = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class Idea:
    category: str
    title: str
    why: str = ""
    effort: str = "M"
    impact: int = 3

    @property
    def score(self) -> float:
        return self.impact / EFFORT_WEIGHT.get(self.effort, 2)


@dataclass
class Roadmap:
    ideas: list[Idea] = field(default_factory=list)
    phases: dict[str, list[Idea]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def by_category(self, category: str) -> dict[str, list[str]]:
        return {phase: [i.title for i in items if i.category == category] for phase, items in self.phases.items()}

    def render(self) -> str:
        lines = ["# Roadmap", ""]
        for phase in PHASES:
            lines += [f"## {phase}", ""]
            for idea in self.phases.get(phase, []):
                lines.append(
                    f"- [{idea.category}] {idea.title} (effort {idea.effort}, impact {idea.impact}) -- {idea.why}"
                )
            lines.append("")
        for category, why in self.failed.items():
            lines.append(f"> {category}: no ideas ({why})")
        return "\n".join(lines).rstrip() + "\n"


def _prompt(category: str, brief: str) -> str:
    return (
        f"You are planning the next work on this project, looking only at {category}.\n"
        f"What the project is:\n{brief}\n\n"
        "Propose up to five ideas. Answer with JSON only: a list of objects with "
        '"title" (short), "why" (one sentence), "effort" ("S", "M" or "L") and "impact" (1 to 5).'
    )


def parse_ideas(category: str, text: str) -> list[Idea]:
    """The ideas in an agent's answer, tolerating prose around the JSON and loose JSON."""
    found = re.search(r"\[.*\]", text, re.S)
    if not found:
        raise ValueError("the answer holds no JSON list of ideas")
    raw = json.loads(repair_json(found.group(0)))
    ideas = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict) or not str(row.get("title") or "").strip():
            continue
        effort = str(row.get("effort") or "M").upper()[:1]
        try:
            impact = max(1, min(5, int(row.get("impact") or 3)))
        except (TypeError, ValueError):
            impact = 3
        ideas.append(
            Idea(
                category=category,
                title=str(row["title"]).strip(),
                why=str(row.get("why") or "").strip(),
                effort=effort if effort in EFFORT_WEIGHT else "M",
                impact=impact,
            )
        )
    if not ideas:
        raise ValueError("the answer holds no usable idea")
    return ideas


def lay_out(ideas: Sequence[Idea]) -> dict[str, list[Idea]]:
    """Cut the ranked ideas into phases, a third each, every category ranked within itself."""
    phases: dict[str, list[Idea]] = {phase: [] for phase in PHASES}
    for category in dict.fromkeys(i.category for i in ideas):
        ranked = sorted((i for i in ideas if i.category == category), key=lambda i: -i.score)
        cut = max(1, -(-len(ranked) // len(PHASES)))
        for index, idea in enumerate(ranked):
            phases[PHASES[min(index // cut, len(PHASES) - 1)]].append(idea)
    for phase in PHASES:
        phases[phase].sort(key=lambda i: -i.score)
    return phases


async def _emit(on_event: OnEvent | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        outcome = on_event(event)
        if asyncio.iscoroutine(outcome):
            await outcome


async def ideate(
    brief: str,
    ask: Ask,
    *,
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    on_event: OnEvent | None = None,
) -> Roadmap:
    """Ask for ideas per category concurrently, then lay the roadmap out from them."""
    roadmap = Roadmap()
    total = len(categories)
    done = 0
    await _emit(
        on_event, {"event": "ideation_started", "phase": "ideas", "progress": 0, "categories": list(categories)}
    )

    async def one(category: str) -> None:
        nonlocal done
        await _emit(on_event, {"event": "category_started", "category": category})
        try:
            ideas = parse_ideas(category, await ask(category, _prompt(category, brief)))
            roadmap.ideas.extend(ideas)
            outcome = {"event": "category_finished", "category": category, "ideas": len(ideas)}
        except Exception as exc:  # noqa: BLE001 - one category's failure is reported, the rest go on
            roadmap.failed[category] = f"{type(exc).__name__}: {exc}"
            outcome = {"event": "category_failed", "category": category, "error": roadmap.failed[category]}
        done += 1
        await _emit(on_event, {**outcome, "phase": "ideas", "progress": round(90 * done / max(total, 1))})

    await asyncio.gather(*(one(c) for c in categories))
    roadmap.ideas.sort(key=lambda i: (list(categories).index(i.category), -i.score))
    roadmap.phases = lay_out(roadmap.ideas)
    await _emit(
        on_event,
        {
            "event": "roadmap_finished",
            "phase": "roadmap",
            "progress": 100,
            "phases": {p: len(v) for p, v in roadmap.phases.items()},
        },
    )
    return roadmap


def save(roadmap: Roadmap, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    ideas = out / "ideas.json"
    ideas.write_text(json.dumps([asdict(i) for i in roadmap.ideas], indent=2, ensure_ascii=False), encoding="utf-8")
    page = out / "roadmap.md"
    page.write_text(roadmap.render(), encoding="utf-8")
    return ideas, page


__all__ = ["DEFAULT_CATEGORIES", "Idea", "Roadmap", "ideate", "lay_out", "parse_ideas", "save"]

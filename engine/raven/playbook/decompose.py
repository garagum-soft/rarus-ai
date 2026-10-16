"""Decomposition: a large goal split, recursively, into a tree of tasks small enough for one agent each.

A planner agent proposes how to split one task at a time -- into two to
`max_children` subtasks that together do exactly that task, each either atomic
(one agent, one sitting) or to be split again, with dependencies among
siblings. Everything else is this module's, and deterministic: the breadth-first
recursion, the limits (depth, children per task, leaves in the whole tree), the
check of every answer (titles, unique ids, dependencies that name a sibling, no
cycle) with one repair round when it fails, an optional reviewer with a bounded
number of revisions, and what the tree becomes:

- dependencies lifted to the leaves (a task that depends on a sibling makes
  every leaf under it depend on that sibling's finishing leaves), and the
  leaves in levels -- the waves that can run at once;
- the tree saved as ``tree.json`` with a readable ``plan.md`` beside it, every
  task carrying its status as it is worked;
- the leaves compiled into ``run_subagent_dag`` nodes, each prompt carrying the
  overall goal, the tasks above it and the outputs of the leaves it waits on.

The planner is any ``ask(role, prompt) -> text`` coroutine, so the same code
runs under the command line, a scenario or a test.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from json_repair import repair_json

Ask = Callable[[str, str], Awaitable[str]]
OnEvent = Callable[[dict[str, Any]], Awaitable[None] | None]

DEFAULT_DEPTH = 3
DEFAULT_CHILDREN = 6
DEFAULT_LEAVES = 24
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class Task:
    id: str
    """Dotted path of slugs from the root (``api.tests``); the root is ``root``."""
    title: str
    goal: str
    acceptance: str = ""
    depends_on: list[str] = field(default_factory=list)
    """Ids of sibling tasks this one waits on."""
    agent: str = ""
    depth: int = 0
    children: list[Task] = field(default_factory=list)
    leaf_reason: str = ""
    """Why a leaf was not split: ``atomic``, ``max depth``, ``leaf budget`` or ``planner failed: ...``."""
    status: str = "planned"
    """``planned``, ``running``, ``done``, ``failed`` or ``skipped``."""
    output: str = ""
    error: str = ""

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        known = set(cls.__dataclass_fields__) - {"children"}
        task = cls(**{k: v for k, v in raw.items() if k in known})
        task.children = [cls.from_dict(c) for c in raw.get("children") or [] if isinstance(c, dict)]
        return task


@dataclass
class TaskTree:
    id: str
    goal: str
    root: Task
    created_at: float = 0.0
    limits: dict[str, int] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def tasks(self) -> list[Task]:
        return list(self.root.walk())

    def leaves(self) -> list[Task]:
        return [t for t in self.root.walk() if t.is_leaf]

    def find(self, task_id: str) -> Task | None:
        return next((t for t in self.root.walk() if t.id == task_id), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskTree:
        return cls(
            id=str(raw["id"]),
            goal=str(raw["goal"]),
            root=Task.from_dict(raw["root"]),
            created_at=float(raw.get("created_at") or 0),
            limits=dict(raw.get("limits") or {}),
            reviews=list(raw.get("reviews") or []),
        )

    def outline(self) -> str:
        """The tree as a person reads it: one line per task, leaves marked, levels at the end."""
        lines = [f"# Plan {self.id}", "", f"Goal: {self.goal}", ""]
        for task in self.root.walk():
            if task is self.root:
                continue
            indent = "  " * (task.depth - 1)
            mark = "-" if task.is_leaf else "+"
            after = f" (after {', '.join(d.rsplit('.', 1)[-1] for d in task.depends_on)})" if task.depends_on else ""
            state = f" [{task.status}]" if task.status != "planned" else ""
            who = f" @{task.agent}" if task.agent and task.is_leaf else ""
            lines.append(f"{indent}{mark} {task.id}: {task.title}{after}{who}{state}")
        lines += ["", f"{len(self.leaves())} leaves in {len(levels(self))} level(s):"]
        for n, level in enumerate(levels(self), 1):
            lines.append(f"  {n}. {', '.join(level)}")
        for review in self.reviews:
            verdict = "approved" if review.get("approved") else "changes asked"
            lines.append(f"Review {review.get('round')}: {verdict}")
        return "\n".join(lines) + "\n"


def new_plan_id() -> str:
    return f"plan-{secrets.token_hex(3)}"


def slug(text: str, *, fallback: str = "task") -> str:
    out = _SLUG.sub("-", (text or "").lower()).strip("-")[:32].strip("-")
    return out or fallback


# -- one split -------------------------------------------------------------------------------


@dataclass
class Split:
    atomic: bool = False
    why: str = ""
    children: list[dict[str, Any]] = field(default_factory=list)


def parse_split(text: str) -> Split:
    """A planner's answer: either ``{"atomic": true}`` or ``{"subtasks": [...]}`` (a bare list too)."""
    found = re.search(r"[\[{].*[\]}]", text or "", re.S)
    if not found:
        raise ValueError("the answer holds no JSON")
    raw = json.loads(repair_json(found.group(0)))
    if isinstance(raw, dict) and raw.get("atomic") is True and not raw.get("subtasks"):
        return Split(atomic=True, why=str(raw.get("why") or ""))
    rows = raw.get("subtasks") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError('expected {"subtasks": [...]} or {"atomic": true}')
    return Split(children=[r for r in rows if isinstance(r, dict)])


def check_split(split: Split, *, max_children: int, agents: list[str] | None = None) -> list[str]:
    """Everything wrong with a proposed split; empty when it may be applied.

    Fixes in place what has one obvious fix: an id derived from the title, a
    dependency spelled as a title, an agent the roster does not have dropped.
    """
    if split.atomic:
        return []
    errors: list[str] = []
    rows = split.children
    if len(rows) < 2:
        errors.append(f'a split needs at least 2 subtasks (got {len(rows)}); answer {{"atomic": true}} instead')
    if len(rows) > max_children:
        errors.append(f"at most {max_children} subtasks (got {len(rows)}); group them")
    seen: dict[str, int] = {}
    by_title: dict[str, str] = {}
    for n, row in enumerate(rows, 1):
        title = str(row.get("title") or "").strip()
        goal = str(row.get("goal") or row.get("description") or "").strip()
        if not title:
            errors.append(f"subtask {n} has no title")
        if not goal:
            errors.append(f"subtask {n} ({title or 'untitled'}) has no goal")
        sid = slug(str(row.get("id") or title), fallback=f"t{n}")
        if sid in seen:
            sid = f"{sid}-{n}"
        seen[sid] = n
        row["id"], row["title"], row["goal"] = sid, title, goal
        by_title[title.lower()] = sid
        if agents is not None and row.get("agent") and str(row["agent"]) not in agents:
            row["agent"] = ""
    for row in rows:
        deps = []
        for dep in row.get("depends_on") or []:
            dep = str(dep).strip()
            target = slug(dep) if slug(dep) in seen else by_title.get(dep.lower(), "")
            if not target:
                errors.append(f"{row['id']} depends on {dep!r}, which is not one of its siblings")
            elif target == row["id"]:
                errors.append(f"{row['id']} depends on itself")
            else:
                deps.append(target)
        row["depends_on"] = list(dict.fromkeys(deps))
    if not errors and _order({r["id"]: r["depends_on"] for r in rows}) is None:
        errors.append("the dependencies among the subtasks form a cycle")
    return errors


def _order(deps: dict[str, list[str]]) -> list[list[str]] | None:
    """Kahn's levels of a dependency map, ties alphabetical; None on a cycle."""
    left = {k: set(v) & set(deps) for k, v in deps.items()}
    out: list[list[str]] = []
    while left:
        ready = sorted(k for k, v in left.items() if not v)
        if not ready:
            return None
        out.append(ready)
        for k in ready:
            del left[k]
        for v in left.values():
            v.difference_update(ready)
    return out


# -- the recursion ---------------------------------------------------------------------------


def _split_prompt(
    tree_goal: str,
    task: Task,
    chain: list[Task],
    siblings: list[Task],
    *,
    max_children: int,
    max_depth: int,
    agents: list[str] | None,
    notes: str,
    context: str,
) -> str:
    # The chain runs root .. task; what is above is everything strictly between.
    above = (
        "\n".join(f"{'  ' * i}- {t.title}: {t.goal}" for i, t in enumerate(chain[1:-1]))
        or "(none: it is the whole goal or a first-level part of it)"
    )
    others = ", ".join(s.title for s in siblings if s is not task) or "none"
    roster = f'\nAgents that can be assigned to a subtask (optional "agent"): {", ".join(agents)}.' if agents else ""
    return (
        "You are planning how a large piece of work is split among agents. Split ONE task.\n\n"
        f"The overall goal:\n{tree_goal}\n"
        + (f"\nWhat is known about the setting:\n{context}\n" if context else "")
        + f"\nThe tasks above this one:\n{above}\n"
        f"\nThe task to split: {task.title}\nIts goal: {task.goal}\n"
        + (f"Done when: {task.acceptance}\n" if task.acceptance else "")
        + f"Its siblings (not yours to cover): {others}\n"
        + (f"\nA reviewer's notes on an earlier plan, to address:\n{notes}\n" if notes else "")
        + "\nSplit it into 2 to "
        f"{max_children} subtasks that together achieve exactly its goal, with no overlap. "
        f'This task is at depth {task.depth} of at most {max_depth}. Mark a subtask "atomic": true when one '
        'agent can do it in one sitting; it will not be split further. Use "depends_on" (ids of sibling '
        "subtasks) only where one truly needs another's result. If this task should not be split at all, "
        'answer {"atomic": true, "why": "..."}.'
        f"{roster}\n\nAnswer with JSON only:\n"
        '{"subtasks": [{"id": "short-slug", "title": "...", "goal": "what it must achieve", '
        '"acceptance": "how to tell it is done", "atomic": true, "depends_on": ["other-id"], "agent": ""}]}'
    )


async def _emit(on_event: OnEvent | None, event: dict[str, Any]) -> None:
    if on_event is None:
        return
    result = on_event(event)
    if asyncio.iscoroutine(result):
        await result


async def _propose(ask: Ask, prompt: str, *, max_children: int, agents: list[str] | None) -> tuple[Split | None, str]:
    """A checked split, asking once more with the errors when the first answer fails."""
    problems = ""
    for attempt in (1, 2):
        text = await ask(
            "planner",
            prompt
            if attempt == 1
            else f"{prompt}\n\nYour previous answer was refused:\n{problems}\nAnswer again, JSON only.",
        )
        try:
            split = parse_split(text)
        except ValueError as exc:
            problems = f"- {exc}"
            continue
        errors = check_split(split, max_children=max_children, agents=agents)
        if not errors:
            return split, ""
        problems = "\n".join(f"- {e}" for e in errors)
    return None, problems.replace("\n", " ")


async def _grow(
    tree: TaskTree,
    ask: Ask,
    *,
    max_depth: int,
    max_children: int,
    max_leaves: int,
    parallel: int,
    agents: list[str] | None,
    notes: str,
    context: str,
    on_event: OnEvent | None,
) -> None:
    parents: dict[str, Task] = {}
    frontier = [tree.root]
    gate = asyncio.Semaphore(max(1, parallel))
    while frontier:

        async def one(task: Task) -> tuple[Task, Split | None, str]:
            chain = _chain(task, parents)
            siblings = parents[task.id].children if task.id in parents else [task]
            prompt = _split_prompt(
                tree.goal,
                task,
                chain,
                siblings,
                max_children=max_children,
                max_depth=max_depth,
                agents=agents,
                notes=notes,
                context=context,
            )
            async with gate:
                try:
                    split, why = await _propose(ask, prompt, max_children=max_children, agents=agents)
                except Exception as exc:  # noqa: BLE001 -- a planner that fails leaves the task whole
                    split, why = None, f"{type(exc).__name__}: {exc}"
            return task, split, why

        answers = await asyncio.gather(*(one(t) for t in frontier))
        frontier = []
        for task, split, why in answers:
            if split is None:
                task.leaf_reason = f"planner failed: {why}"[:300]
            elif split.atomic:
                task.leaf_reason = "atomic"
            elif len(tree.leaves()) + len(split.children) - 1 > max_leaves:
                task.leaf_reason = "leaf budget"
            else:
                for row in split.children:
                    child = Task(
                        id=row["id"] if task is tree.root else f"{task.id}.{row['id']}",
                        title=row["title"],
                        goal=row["goal"],
                        acceptance=str(row.get("acceptance") or "").strip(),
                        agent=str(row.get("agent") or ""),
                        depth=task.depth + 1,
                    )
                    task.children.append(child)
                    parents[child.id] = task
                for child, row in zip(task.children, split.children, strict=True):
                    prefix = "" if task is tree.root else f"{task.id}."
                    child.depends_on = [f"{prefix}{d}" for d in row["depends_on"]]
                    if row.get("atomic") is True:
                        child.leaf_reason = "atomic"
                    elif child.depth >= max_depth:
                        child.leaf_reason = "max depth"
                    else:
                        frontier.append(child)
            await _emit(
                on_event,
                {
                    "event": "split",
                    "task": task.id,
                    "children": [c.id for c in task.children],
                    "leaf": task.leaf_reason,
                },
            )


def _chain(task: Task, parents: dict[str, Task]) -> list[Task]:
    """The root, every task between, and ``task`` itself."""
    chain = [task]
    while chain[-1].id in parents:
        chain.append(parents[chain[-1].id])
    return list(reversed(chain))


def _review_prompt(tree: TaskTree) -> str:
    return (
        "Review this plan for the goal below. It is good when the leaves together achieve the goal, nothing "
        "overlaps, every leaf can be done by one agent in one sitting, and the dependencies are real. Answer "
        "APPROVED on a line of its own if it is good; otherwise list exactly what is wrong.\n\n"
        f"Goal: {tree.goal}\n\n{tree.outline()}"
    )


async def decompose(
    goal: str,
    ask: Ask,
    *,
    max_depth: int = DEFAULT_DEPTH,
    max_children: int = DEFAULT_CHILDREN,
    max_leaves: int = DEFAULT_LEAVES,
    parallel: int = 3,
    agents: list[str] | None = None,
    review_rounds: int = 0,
    context: str = "",
    plan_id: str | None = None,
    on_event: OnEvent | None = None,
) -> TaskTree:
    """Split ``goal`` into a tree of tasks; with ``review_rounds``, a reviewer may send it back that many times."""
    if not goal or not goal.strip():
        raise ValueError("nothing to decompose: the goal is empty")
    if max_depth < 1 or max_children < 2 or max_leaves < 2:
        raise ValueError("limits: depth at least 1, children at least 2, leaves at least 2")
    limits = {"max_depth": max_depth, "max_children": max_children, "max_leaves": max_leaves}
    pid = plan_id or new_plan_id()
    notes = ""
    reviews: list[dict[str, Any]] = []
    for round_no in range(0, max(0, review_rounds) + 1):
        tree = TaskTree(
            id=pid,
            goal=goal.strip(),
            root=Task(id="root", title="the goal", goal=goal.strip()),
            created_at=time.time(),
            limits=limits,
            reviews=reviews,
        )
        await _emit(on_event, {"event": "plan.start", "round": round_no})
        await _grow(
            tree, ask, parallel=parallel, agents=agents, notes=notes, context=context, on_event=on_event, **limits
        )
        if round_no >= review_rounds:
            return tree
        verdict = await ask("reviewer", _review_prompt(tree))
        approved = any(line.strip().upper().strip(".!*") == "APPROVED" for line in verdict.splitlines())
        reviews.append({"round": round_no + 1, "approved": approved, "notes": verdict.strip()[:2000]})
        await _emit(on_event, {"event": "review", "round": round_no + 1, "approved": approved})
        if approved:
            return tree
        notes = verdict.strip()[:2000]
    return tree  # pragma: no cover -- the loop always returns


# -- what the tree becomes -------------------------------------------------------------------


def leaf_dependencies(tree: TaskTree) -> dict[str, list[str]]:
    """Every leaf -> the leaves it must wait for, the tree's sibling dependencies lifted to the leaves.

    A task that depends on a sibling makes every leaf under it wait for that
    sibling's finishing leaves (those no other leaf under the sibling waits on),
    and a leaf inherits what every task above it waits for.
    """
    by_id = {t.id: t for t in tree.root.walk()}
    finishing_of: dict[str, list[str]] = {}

    def finishing(task: Task) -> list[str]:
        # The leaves under `task` that nothing else under it waits on: a leaf
        # waited on by a sibling-dependency inside the subtree finishes earlier.
        if task.id not in finishing_of:
            under = [t for t in task.walk() if t.is_leaf]
            waited: set[str] = set()
            for leaf in under:
                node: Task | None = leaf
                while node is not None and node is not task:
                    for dep in node.depends_on:
                        if dep in by_id:
                            waited.update(t.id for t in by_id[dep].walk() if t.is_leaf)
                    node = _parent(node, by_id)
            finishing_of[task.id] = [leaf.id for leaf in under if leaf.id not in waited]
        return finishing_of[task.id]

    out: dict[str, list[str]] = {}
    for leaf in tree.leaves():
        need: list[str] = []
        node: Task | None = leaf
        while node is not None and node is not tree.root:
            for dep in node.depends_on:
                if dep in by_id:
                    need.extend(finishing(by_id[dep]))
            node = _parent(node, by_id)
        out[leaf.id] = list(dict.fromkeys(need))
    return out


def _parent(task: Task, by_id: dict[str, Task]) -> Task | None:
    if "." in task.id:
        return by_id.get(task.id.rsplit(".", 1)[0])
    return by_id.get("root") if task.id != "root" else None


def levels(tree: TaskTree) -> list[list[str]]:
    """The leaves in waves: each wave needs only leaves of earlier waves."""
    order = _order(leaf_dependencies(tree))
    if order is None:  # pragma: no cover -- sibling checks keep the lifted graph acyclic
        raise ValueError("the plan's dependencies form a cycle")
    return order


def dag_id(tree: TaskTree, leaf_id: str) -> str:
    return f"{tree.id}_{leaf_id.replace('.', '_')}"


def _context_block(tree: TaskTree, leaf: Task) -> str:
    by_id = {t.id: t for t in tree.root.walk()}
    chain, node = [], _parent(leaf, by_id)
    while node is not None and node is not tree.root:
        chain.append(node)
        node = _parent(node, by_id)
    above = "".join(f"- {t.title}: {t.goal}\n" for t in reversed(chain))
    return (
        f"You are doing one part of a larger piece of work.\n\nThe overall goal:\n{tree.goal}\n"
        + (f"\nThe parts of the plan this one belongs to:\n{above}" if above else "")
        + f"\nYour part: {leaf.title}\n{leaf.goal}\n"
        + (f"Done when: {leaf.acceptance}\n" if leaf.acceptance else "")
        + "\nDo only your part. Finish with what you produced, or why it could not be done."
    )


def leaf_prompt(tree: TaskTree, leaf: Task, outputs: dict[str, str], *, excerpt: int = 4000) -> str:
    """A leaf's task with the outputs of the leaves it waits on written in."""
    text = _context_block(tree, leaf)
    deps = leaf_dependencies(tree).get(leaf.id, [])
    for dep in deps:
        done = tree.find(dep)
        body = (outputs.get(dep) or "")[:excerpt]
        text += f"\n\nResult of the earlier part '{done.title if done else dep}' ({dep}):\n{body or '(no output)'}"
    return text


def to_dag_nodes(tree: TaskTree, *, agent: str = "Raven") -> list[dict[str, Any]]:
    """The leaves as ``run_subagent_dag`` nodes: ids unique to this plan, earlier results by placeholder."""
    deps = leaf_dependencies(tree)
    nodes = []
    for leaf in tree.leaves():
        template = _context_block(tree, leaf)
        for dep in deps[leaf.id]:
            done = tree.find(dep)
            template += (
                f"\n\nResult of the earlier part '{done.title if done else dep}' ({dep}):\n"
                f"{{{{ {dag_id(tree, dep)}.output }}}}"
            )
        nodes.append(
            {
                "id": dag_id(tree, leaf.id),
                "subagent": leaf.agent or agent,
                "node_summary": leaf.title,
                "prompt_template": template,
                "depends_on": [dag_id(tree, d) for d in deps[leaf.id]],
            }
        )
    return nodes


# -- the record ------------------------------------------------------------------------------


def plan_dir(project: Path, plan_id: str) -> Path:
    return project / ".raven" / "plans" / plan_id


def save(tree: TaskTree, directory: Path) -> tuple[Path, Path]:
    """``tree.json`` and ``plan.md`` in ``directory``; written whole each time, so a reader never sees half."""
    from raven.utils.atomic_io import atomic_replace

    directory.mkdir(parents=True, exist_ok=True)
    data, page = directory / "tree.json", directory / "plan.md"
    atomic_replace(data, json.dumps(tree.to_dict(), ensure_ascii=False, indent=2))
    atomic_replace(page, tree.outline())
    return data, page


def load(path: Path) -> TaskTree:
    target = path / "tree.json" if path.is_dir() else path
    return TaskTree.from_dict(json.loads(target.read_text(encoding="utf-8")))


__all__ = [
    "DEFAULT_CHILDREN",
    "DEFAULT_DEPTH",
    "DEFAULT_LEAVES",
    "Split",
    "Task",
    "TaskTree",
    "check_split",
    "dag_id",
    "decompose",
    "leaf_dependencies",
    "leaf_prompt",
    "levels",
    "load",
    "new_plan_id",
    "parse_split",
    "plan_dir",
    "save",
    "slug",
    "to_dag_nodes",
]

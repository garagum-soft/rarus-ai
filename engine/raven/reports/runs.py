"""Every run the journals record, as one normalised entry each.

Three kinds of run leave a record under a conversation's metadata directory
(``sessions/<group>/<chat_id>/subagents/``):

- a **graph run** (``run_subagent_dag``): ``mas_dag/<run_id>/`` holds the graph,
  the manifest the runner writes as it finishes and the receipt over both;
- a **spawn**: ``nodes/<id>.meta.json`` beside its prompt and output;
- a **stint**: ``mas_dag/stints/<stint_id>.json``.

``nodes.json`` is the conversation's index of the first two; this module reads
it first and the per-run files after, and tolerates every one of them being
absent or half-written -- a run killed mid-way is a run the report should still
show, as unfinished.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RUNNING = ("running", "pending", "unrecorded")


@dataclass
class RunEntry:
    kind: str
    """dag, spawn or stint."""
    id: str
    session_key: str
    title: str
    status: str
    started_ms: int = 0
    ended_ms: int = 0
    agents: list[str] = field(default_factory=list)
    nodes: dict[str, int] = field(default_factory=dict)
    """Node count per status (a graph run); one row for a spawn."""
    failures: list[dict[str, str]] = field(default_factory=list)
    """``{node, agent, error}`` for every node that failed, with its first error line."""
    retried: list[str] = field(default_factory=list)
    """Nodes that took more than one attempt."""
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    files_changed: int = 0
    receipt: str = "none"
    """verified, broken or none (a graph run only)."""
    record: str = ""
    """Where the run's own record is, for a reader who wants the whole of it."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Stint facts: rounds, failing checks, violations, open questions, integration."""

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.ended_ms - self.started_ms) / 1000) if self.ended_ms and self.started_ms else 0.0

    @property
    def finished(self) -> bool:
        return self.status not in RUNNING

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "finished")

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["duration_s"] = round(self.duration_s, 1)
        return out


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first_line(text: Any, limit: int = 200) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def session_key_of(transcript: Path) -> str:
    """The key a transcript's own metadata states, else ``<group>:<stem>``."""
    key = ""
    try:
        with transcript.open(encoding="utf-8") as fh:
            for line in fh:
                if '"_type"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("_type") == "metadata" and row.get("key"):
                    key = str(row["key"])
    except OSError:
        pass
    return key or f"{transcript.parent.name}:{transcript.stem}"


def conversations(workspace: Path) -> Iterator[tuple[str, Path | None, Path]]:
    """``(session key, transcript, metadata directory)`` for every stored conversation.

    A metadata directory with run records but no transcript (a graph run driven
    from the command line, a transcript retention deleted) is yielded too, with
    no transcript and the key its path spells.
    """
    root = Path(workspace) / "sessions"
    if not root.is_dir():
        return
    seen: set[Path] = set()
    for transcript in sorted(root.glob("*/*.jsonl")):
        sdir = transcript.with_suffix("")
        seen.add(sdir)
        yield session_key_of(transcript), transcript, sdir
    for history in sorted(root.glob("*/*/subagents")):
        sdir = history.parent
        if sdir not in seen:
            yield f"{sdir.parent.name}:{sdir.name}", None, sdir


def _activity(meta: dict[str, Any]) -> dict[str, int]:
    return {
        "tokens_in": int(meta.get("tokens_in") or 0),
        "tokens_out": int(meta.get("tokens_out") or 0),
        "tool_calls": len(meta.get("tool_calls") or []),
        "tool_failures": int(meta.get("tool_failures") or 0),
        "files_changed": len(meta.get("files") or []),
    }


def _attempts(nodes_dir: Path, node: str) -> int:
    """How many attempts a node took. Every attempt, the first included, writes its own
    ``<node>.attempt-<n>.prompt.md``, so the highest ``n`` is the count."""
    numbers = []
    for path in nodes_dir.glob(f"{node}.attempt-*.prompt.md"):
        middle = path.name[len(node) + len(".attempt-") : -len(".prompt.md")]
        if middle.isdigit():
            numbers.append(int(middle))
    return max(numbers, default=0)


def _receipt(run_dir: Path, nodes_dir: Path) -> str:
    if not (run_dir / "receipt.json").is_file():
        return "none"
    try:
        from raven.agent.subagent.receipts import verify_receipt

        return "verified" if verify_receipt(run_dir, nodes_dir).ok else "broken"
    except Exception:  # noqa: BLE001 - a receipt that cannot be checked is reported, not raised
        return "broken"


def _dag_entry(key: str, sdir: Path, run: dict[str, Any], registry_nodes: dict[str, Any]) -> RunEntry:
    run_id = str(run.get("run_id") or "")
    run_dir = sdir / "subagents" / "mas_dag" / run_id
    nodes_dir = sdir / "subagents" / "nodes"
    graph = _json(run_dir / "graph.json") or {}
    manifest = _json(run_dir / "manifest.json") or {}
    # The graph's own order (the order the planner wrote), else the index's.
    graph_ids = [n.get("id") for n in graph.get("nodes") or [] if isinstance(n, dict)]
    node_ids = [str(n) for n in (graph_ids or run.get("nodes") or []) if n]
    spec_agent = {
        str(n.get("id")): str(n.get("subagent") or "") for n in graph.get("nodes") or [] if isinstance(n, dict)
    }
    entry = RunEntry(
        kind="dag",
        id=run_id,
        session_key=key,
        title=str(graph.get("task_summary") or run.get("summary") or run_id),
        status="running",
        record=str(run_dir),
    )
    statuses: Counter[str] = Counter()
    starts, ends = [], []
    for node in node_ids:
        row = manifest.get(node) if isinstance(manifest.get(node), dict) else {}
        reg = registry_nodes.get(node) or {}
        status = str(row.get("status") or reg.get("status") or "unrecorded")
        statuses[status] += 1
        agent = str(row.get("subagent") or spec_agent.get(node) or "")
        if agent and agent not in entry.agents:
            entry.agents.append(agent)
        started = int(row.get("started_at") or reg.get("started_at_ms") or 0)
        ended = int(row.get("ended_at") or reg.get("ended_at_ms") or 0)
        if started:
            starts.append(started)
        if ended:
            ends.append(ended)
        if status == "failed":
            entry.failures.append({"node": node, "agent": agent, "error": _first_line(row.get("error")) or "failed"})
        if _attempts(nodes_dir, node) > 1:
            entry.retried.append(node)
        for k, v in _activity(row).items():
            setattr(entry, k, getattr(entry, k) + v)
    entry.nodes = dict(statuses)
    entry.started_ms = min(starts) if starts else 0
    entry.ended_ms = max(ends) if ends and not any(s in RUNNING for s in statuses) else 0
    if manifest:
        if statuses.get("failed"):
            entry.status = "failed"
        elif statuses.get("cancelled"):
            entry.status = "cancelled"
        elif node_ids and statuses.get("completed", 0) == len(node_ids):
            entry.status = "completed"
        else:
            entry.status = "partial"
    elif any(s not in RUNNING for s in statuses) and not any(s in RUNNING for s in statuses):
        entry.status = "interrupted"
    entry.receipt = _receipt(run_dir, nodes_dir)
    return entry


def _spawn_entry(key: str, sdir: Path, node: str, reg: dict[str, Any]) -> RunEntry:
    nodes_dir = sdir / "subagents" / "nodes"
    meta = _json(nodes_dir / f"{node}.meta.json") or {}
    status = str(meta.get("status") or reg.get("status") or "unrecorded")
    entry = RunEntry(
        kind="spawn",
        id=node,
        session_key=key,
        title=str(meta.get("task_summary") or meta.get("label") or node),
        status=status,
        started_ms=int(meta.get("started_at_ms") or reg.get("started_at_ms") or 0),
        ended_ms=int(meta.get("ended_at_ms") or reg.get("ended_at_ms") or 0),
        agents=[str(meta["agent"])] if meta.get("agent") else [],
        nodes={status: 1},
        record=str(nodes_dir / f"{node}.meta.json"),
        **_activity(meta),
    )
    if status == "failed":
        error = ""
        try:
            error = (nodes_dir / f"{node}.error.md").read_text(encoding="utf-8")
        except OSError:
            pass
        entry.failures.append(
            {"node": node, "agent": entry.agents[0] if entry.agents else "", "error": _first_line(error) or "failed"}
        )
    return entry


def _stint_entries(key: str, sdir: Path) -> Iterator[RunEntry]:
    root = sdir / "subagents" / "mas_dag" / "stints"
    if not root.is_dir():
        return
    from raven.stint.record import StintStore

    try:
        records = StintStore(root).list()
    except Exception:  # noqa: BLE001 - a store that cannot be read is skipped, the rest still report
        return
    for rec in records:
        failing = sorted(
            {
                str(check.get("name"))
                for rnd in rec.rounds[-1:]
                for check in rnd.verify
                if isinstance(check, dict) and check.get("status") in ("failed", "timeout")
            }
        )
        open_questions = [q for q in rec.questions if not q.get("answered_at") and not q.get("answer")]
        agents: list[str] = []
        for role in (rec.spec or {}).get("roles") or []:
            name = str(role.get("name") or "") if isinstance(role, dict) else ""
            if name and name not in agents:
                agents.append(name)
        yield RunEntry(
            kind="stint",
            id=rec.stint_id,
            session_key=key,
            title=f"{rec.playbook}: {rec.stop_reason or rec.status}",
            status=rec.status,
            started_ms=int(rec.started_at_ms or 0),
            ended_ms=int(rec.ended_at_ms or 0),
            agents=agents,
            nodes=dict(Counter(rnd.status for rnd in rec.rounds)),
            record=str(root / f"{rec.stint_id}.json"),
            extra={
                "playbook": rec.playbook,
                "rounds": len(rec.rounds),
                "stop_reason": rec.stop_reason,
                "failing_checks": failing,
                "violations": sum(len(rnd.violations) for rnd in rec.rounds),
                "open_questions": [str(q.get("text") or "")[:200] for q in open_questions],
                "integration": (rec.integration or {}).get("status", ""),
                "branch": rec.branch,
            },
        )


def collect_runs(workspace: Path, *, since_ms: int = 0, until_ms: int = 0) -> list[RunEntry]:
    """Every run under ``workspace`` that started in ``[since_ms, until_ms)`` (0: unbounded), newest first.

    A run whose start was never recorded is kept: it is the run a reader most
    needs to see.
    """
    out: list[RunEntry] = []
    for key, _transcript, sdir in conversations(workspace):
        registry = _json(sdir / "subagents" / "nodes.json") or {}
        reg_nodes = registry.get("nodes") if isinstance(registry.get("nodes"), dict) else {}
        for run in registry.get("runs") or []:
            if isinstance(run, dict) and run.get("run_id"):
                out.append(_dag_entry(key, sdir, run, reg_nodes))
        for node, reg in reg_nodes.items():
            if isinstance(reg, dict) and reg.get("kind") == "spawn":
                out.append(_spawn_entry(key, sdir, str(node), reg))
        out.extend(_stint_entries(key, sdir))

    def inside(e: RunEntry) -> bool:
        if not e.started_ms:
            return True
        return (not since_ms or e.started_ms >= since_ms) and (not until_ms or e.started_ms < until_ms)

    return sorted((e for e in out if inside(e)), key=lambda e: (e.started_ms, e.id), reverse=True)


__all__ = ["RUNNING", "RunEntry", "collect_runs", "conversations", "session_key_of"]

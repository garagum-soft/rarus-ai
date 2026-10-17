"""The period report: conversations, runs, their retrospective and what they cost.

Built from three journals: the conversations' transcripts (who asked what, which
turns failed), the run records (``raven.reports.runs``) and the usage ledger
(``<RARUS_HOME>/telemetry/usage-*.jsonl``). Cost counts only what a provider
reported, as the ledger does; calls without a reported cost are counted and
said, never estimated into the total.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from raven.reports.retro import period_retro, run_retro
from raven.reports.runs import RunEntry, collect_runs, conversations


def parse_since(text: str, *, now: float | None = None) -> int:
    """``7d``, ``24h``, ``90m`` or a date (``2026-10-01``) as epoch ms."""
    text = (text or "").strip()
    now = time.time() if now is None else now
    units = {"d": 86400, "h": 3600, "m": 60}
    if text[:-1].isdigit() and text[-1:] in units:
        return int((now - int(text[:-1]) * units[text[-1]]) * 1000)
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError as exc:
        raise ValueError(f"not a period: {text!r} (use 7d, 24h, 90m or a date like 2026-10-01)") from exc


def _ms(ts: Any) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def conversation_stats(workspace: Path, since_ms: int, until_ms: int, *, only: str | None = None) -> dict[str, Any]:
    """Conversations with any message in the period, the turns asked and how the failed ones ended."""
    active, asked, answered = 0, 0, 0
    ended: Counter[str] = Counter()
    for key, transcript, _sdir in conversations(workspace):
        if (only and key != only) or transcript is None:
            continue
        seen = False
        try:
            lines = transcript.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("_type") == "metadata":
                continue
            at = _ms(row.get("timestamp"))
            if not at or at < since_ms or (until_ms and at >= until_ms):
                continue
            seen = True
            role = row.get("role")
            if role == "user" and not row.get("origin"):
                asked += 1
            elif role == "assistant" and isinstance(row.get("turn_ended"), dict):
                ended[str(row["turn_ended"].get("status") or "failed")] += 1
            elif role == "assistant" and row.get("content") and not row.get("tool_calls"):
                answered += 1
        active += seen
    return {"conversations": active, "asked": asked, "answered": answered, "turns_ended": dict(ended)}


def usage_stats(telemetry: Path, since_ms: int, until_ms: int, *, only: str | None = None) -> dict[str, Any]:
    """Calls, tokens and reported cost in the period, by model and by conversation."""
    from raven.token_wise.usage_tracker import read_usage_rows, row_root

    start = datetime.fromtimestamp(since_ms / 1000).date() - timedelta(days=1) if since_ms else date(2000, 1, 1)
    end = datetime.fromtimestamp((until_ms or time.time() * 1000) / 1000).date() + timedelta(days=1)
    totals = {"calls": 0, "input": 0, "output": 0, "cost_usd": 0.0, "cost_missing_calls": 0}
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "tokens": 0, "cost_usd": 0.0})
    by_session: dict[str, float] = defaultdict(float)
    for path in sorted(Path(telemetry).glob("usage-*.jsonl")) if Path(telemetry).is_dir() else []:
        try:
            day = date.fromisoformat(path.stem.removeprefix("usage-"))
        except ValueError:
            continue
        if day < start or day > end:
            continue
        for row in read_usage_rows(path):
            if row.get("_type") == "tool_call":
                continue
            at = _ms(row.get("ts"))
            if not at or (since_ms and at < since_ms) or (until_ms and at >= until_ms):
                continue
            root = str(row_root(row) or "")
            if only and root != only:
                continue
            tokens = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
            totals["calls"] += 1
            totals["input"] += int(row.get("input_tokens") or 0)
            totals["output"] += int(row.get("output_tokens") or 0)
            model = by_model[str(row.get("model") or "?")]
            model["calls"] += 1
            model["tokens"] += tokens
            cost = row.get("cost_usd")
            if row.get("schema_version") == 2 and isinstance(cost, (int, float)):
                totals["cost_usd"] += float(cost)
                model["cost_usd"] += float(cost)
                by_session[root] += float(cost)
            else:
                totals["cost_missing_calls"] += 1
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    top = sorted(by_session.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        **totals,
        "by_model": {m: {**v, "cost_usd": round(v["cost_usd"], 6)} for m, v in sorted(by_model.items())},
        "top_sessions": [{"session_key": k, "cost_usd": round(v, 6)} for k, v in top],
    }


def build_report(
    workspace: Path,
    telemetry: Path,
    *,
    since_ms: int,
    until_ms: int = 0,
    session: str | None = None,
) -> dict[str, Any]:
    runs = collect_runs(workspace, since_ms=since_ms, until_ms=until_ms)
    if session:
        runs = [r for r in runs if r.session_key == session]
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for r in runs:
        by_kind[r.kind][r.status] += 1
    retro = period_retro(runs)
    return {
        "since_ms": since_ms,
        "until_ms": until_ms or int(time.time() * 1000),
        "generated_ms": int(time.time() * 1000),
        "session": session or "",
        "conversations": conversation_stats(workspace, since_ms, until_ms, only=session),
        "runs": [r.to_dict() for r in runs],
        "runs_by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "usage": usage_stats(telemetry, since_ms, until_ms, only=session),
        "retro": {"went_well": retro.went_well, "went_wrong": retro.went_wrong, "open": retro.open_items},
    }


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "-"


def _dur(seconds: float) -> str:
    if not seconds:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m" if seconds < 5400 else f"{seconds / 3600:.1f}h"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any], *, failed_detail: int = 10) -> str:
    c, u = report["conversations"], report["usage"]
    lines = [
        f"# Raven report: {_when(report['since_ms'])} to {_when(report['until_ms'])}",
        "",
        f"Built {datetime.fromtimestamp(report['generated_ms'] / 1000, tz=timezone.utc).astimezone():%Y-%m-%d %H:%M} "
        "from the conversations, the run records and the usage ledger."
        + (f" Conversation: `{report['session']}`." if report["session"] else ""),
        "",
        "## Summary",
        "",
        f"- Conversations with activity: {c['conversations']}; requests: {c['asked']}; answers: {c['answered']}.",
    ]
    if c["turns_ended"]:
        lines.append(
            "- Turns that did not finish: " + ", ".join(f"{n} {s}" for s, n in sorted(c["turns_ended"].items())) + "."
        )
    runs = report["runs"]
    if report["runs_by_kind"]:
        parts = []
        for kind, statuses in report["runs_by_kind"].items():
            parts.append(
                f"{kind} {sum(statuses.values())} (" + ", ".join(f"{n} {s}" for s, n in sorted(statuses.items())) + ")"
            )
        lines.append("- Runs: " + "; ".join(parts) + ".")
    else:
        lines.append("- Runs: none.")
    cost = f"${u['cost_usd']:.4f} reported" if u["cost_usd"] else "no reported cost"
    missing = f", {u['cost_missing_calls']} calls without a reported cost" if u["cost_missing_calls"] else ""
    lines.append(f"- Model calls: {u['calls']}; tokens in/out: {u['input']}/{u['output']}; {cost}{missing}.")
    lines += ["", "## Retrospective", ""]
    retro = report["retro"]
    for title, items in (
        ("What went well", retro["went_well"]),
        ("What went wrong", retro["went_wrong"]),
        ("Still open", retro["open"]),
    ):
        lines.append(f"**{title}**")
        lines.extend(f"- {i}" for i in items or ["nothing recorded"])
        lines.append("")
    lines += ["## Runs", ""]
    if runs:
        lines += [
            "| Started | Kind | Run | Status | Agents | Took | Nodes | Conversation |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in runs:
            nodes = ", ".join(f"{n} {s}" for s, n in sorted(r["nodes"].items()))
            lines.append(
                f"| {_when(r['started_ms'])} | {r['kind']} | {_cell(r['title'])[:60]} (`{r['id']}`) | {r['status']} "
                f"| {_cell(', '.join(r['agents']))} | {_dur(r['duration_s'])} | {nodes} | `{r['session_key']}` |"
            )
    else:
        lines.append("No runs in this period.")
    failed = [r for r in runs if r["failures"] or r["status"] in ("failed", "interrupted")][:failed_detail]
    if failed:
        lines += ["", "## Failed runs", ""]
        for r in failed:
            entry = RunEntry(**{k: v for k, v in r.items() if k in RunEntry.__dataclass_fields__})
            lines.append(run_retro(entry).render(f"{r['kind']} `{r['id']}`: {r['title']}", level=3).rstrip())
            lines.append(f"Record: `{r['record']}`")
            lines.append("")
    if u["by_model"]:
        lines += ["", "## Cost by model", "", "| Model | Calls | Tokens | Reported cost |", "|---|---|---|---|"]
        for model, v in u["by_model"].items():
            lines.append(f"| {model} | {v['calls']} | {v['tokens']} | ${v['cost_usd']:.4f} |")
    if u["top_sessions"]:
        lines += ["", "**Costliest conversations**", ""]
        lines.extend(f"- `{s['session_key']}`: ${s['cost_usd']:.4f}" for s in u["top_sessions"])
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["build_report", "conversation_stats", "parse_since", "render_markdown", "usage_stats"]

"""Fast mode: a small task done straight away, without the planning pipeline.

The full path turns a goal into a playbook first -- the generator plans a graph
of nodes, then the graph runs. For a small task that planning costs more than
the work. Fast mode skips it: the task goes to one agent as written, in the
project as it is, and the checks the caller named decide whether it is done. A
failed check sends the same agent a further attempt with what failed, up to a
small number of attempts, each under a tight time limit. Steps run one after
another and are recorded, so a fast run can be read back like any other.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from raven.stint.verify import CheckSpec, run_checks

Agent = Callable[[str, Path], Awaitable[str]]
Event = Callable[[dict[str, Any]], None]

DEFAULT_ATTEMPTS = 2
DEFAULT_STEP_TIMEOUT_S = 300.0
_TAIL = 2000


@dataclass
class FastStep:
    attempt: int
    kind: str  # "agent" | "checks"
    seconds: float
    ok: bool
    detail: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FastResult:
    task: str
    project: str
    started: str
    passed: bool = False
    answer: str = ""
    steps: list[FastStep] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return max((s.attempt for s in self.steps), default=0)

    def report(self) -> str:
        lines = [f"fast: {'done' if self.passed else 'not done'} after {self.attempts} attempt(s)"]
        for step in self.steps:
            mark = "ok" if step.ok else "failed"
            lines.append(f"  [{step.attempt}] {step.kind}: {mark} in {step.seconds:.1f}s {step.detail}".rstrip())
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "attempts": self.attempts}


def _brief(task: str, failures: str) -> str:
    if not failures:
        return task
    return (
        f"{task}\n\nYour previous attempt did not pass these checks. Fix what they report and stop when they pass:\n"
        f"{failures}"
    )


def _failures(results: Sequence[Any]) -> str:
    parts = []
    for result in results:
        if result.ok:
            continue
        tail = (result.stdout_tail + "\n" + result.stderr_tail).strip()[-_TAIL:]
        parts.append(f"- {result.name} (`{result.command}`): {result.summary().detail}\n{tail}")
    return "\n".join(parts)


async def run_fast(
    task: str,
    project: Path,
    agent: Agent,
    *,
    checks: Sequence[CheckSpec] = (),
    attempts: int = DEFAULT_ATTEMPTS,
    step_timeout_s: float = DEFAULT_STEP_TIMEOUT_S,
    log_dir: Path | None = None,
    on_event: Event | None = None,
) -> FastResult:
    """Do ``task`` in ``project`` with ``agent``, check it, and retry with what failed.

    With no checks the agent's own answer is the result: one attempt, done
    when the agent returns. An agent that runs past ``step_timeout_s`` or
    raises ends that attempt as failed.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    emit = on_event or (lambda _event: None)
    result = FastResult(task=task, project=str(project), started=datetime.now().isoformat(timespec="seconds"))
    logs = log_dir or project / ".raven" / "fast"
    failures = ""
    for attempt in range(1, attempts + 1):
        emit({"event": "attempt_started", "attempt": attempt})
        began = time.monotonic()
        try:
            answer = await asyncio.wait_for(agent(_brief(task, failures), project), timeout=step_timeout_s)
        except asyncio.TimeoutError:
            result.steps.append(
                FastStep(attempt, "agent", time.monotonic() - began, False, f"no answer in {step_timeout_s:.0f}s")
            )
            emit({"event": "agent_failed", "attempt": attempt, "reason": "timeout"})
            failures = f"- the attempt ran out of time ({step_timeout_s:.0f}s); do less and finish"
            continue
        except Exception as exc:  # noqa: BLE001 - a failed attempt is recorded, the next one may still pass
            result.steps.append(FastStep(attempt, "agent", time.monotonic() - began, False, str(exc)[:200]))
            emit({"event": "agent_failed", "attempt": attempt, "reason": str(exc)[:200]})
            failures = f"- the attempt failed: {exc}"
            continue
        result.answer = answer
        result.steps.append(FastStep(attempt, "agent", time.monotonic() - began, True))
        emit({"event": "agent_finished", "attempt": attempt})
        if not checks:
            result.passed = True
            break
        began = time.monotonic()
        ran = await asyncio.to_thread(run_checks, list(checks), cwd=project, log_dir=logs / f"attempt-{attempt}")
        ok = all(r.ok for r in ran)
        result.steps.append(
            FastStep(
                attempt,
                "checks",
                time.monotonic() - began,
                ok,
                ", ".join(f"{r.name} {r.status}" for r in ran),
                [{"name": r.name, "status": r.status} for r in ran],
            )
        )
        emit({"event": "checks_finished", "attempt": attempt, "ok": ok})
        if ok:
            result.passed = True
            break
        failures = _failures(ran)
    emit({"event": "fast_finished", "passed": result.passed, "attempts": result.attempts})
    return result


def save(result: FastResult, root: Path, tag: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{tag}.json"
    path.write_text(json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = ["DEFAULT_ATTEMPTS", "DEFAULT_STEP_TIMEOUT_S", "FastResult", "FastStep", "run_fast", "save"]

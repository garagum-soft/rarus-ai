"""The capability matrix: what each roster agent has been seen to do, and who to give a task to.

The availability probe (``probe.py``) answers "does this agent start and
reply". It says nothing about what the agent can do once it runs, and the
roster's own descriptions are claims, not observations. A capability probe is a
small task whose result is checked rather than read: the token has to come
back, the file has to exist, the command's output cannot be guessed, the code
has to pass when it is run. Each run is one observation -- agent, capability,
pass or fail, how long -- kept in ``capability_matrix.json`` under the raven
home. Real runs add observations too (fast mode records how a task it routed
went).

Assignment reads the matrix. A task's needs are inferred from its words (a
file to read, something to run, code to write); each candidate scores the
product of its smoothed pass rates over those needs -- (passes + 1) /
(observations + 2), so an unprobed agent sits at an even 0.5 rather than at
zero or one -- and ties go to the better-observed, then the faster agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from raven.utils.atomic_io import atomic_replace, write_transaction

Runner = Callable[[str, str, Path], Awaitable[str]]


# -- the probes -----------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    capability: str
    description: str
    prepare: Callable[[Path, str], str]
    check: Callable[[Path, str, str], tuple[bool, str]]


def _reply_task(ws: Path, nonce: str) -> str:
    return f"Reply with exactly this token and nothing else: {nonce}"


def _reply_check(ws: Path, nonce: str, reply: str) -> tuple[bool, str]:
    return (nonce in reply, "token returned" if nonce in reply else "token missing from the reply")


def _read_task(ws: Path, nonce: str) -> str:
    secret = hashlib.sha256(f"read:{nonce}".encode()).hexdigest()[:20]
    (ws / f"probe-{nonce}.txt").write_text(secret, encoding="utf-8")
    return f"Read the file probe-{nonce}.txt in your working directory and reply with its exact contents."


def _read_check(ws: Path, nonce: str, reply: str) -> tuple[bool, str]:
    secret = hashlib.sha256(f"read:{nonce}".encode()).hexdigest()[:20]
    return (
        secret in reply,
        "file contents returned" if secret in reply else "the file's contents are not in the reply",
    )


def _write_task(ws: Path, nonce: str) -> str:
    return (
        f"Create a file named out-{nonce}.txt in your working directory containing exactly the text "
        f"{nonce[::-1]}. Then reply DONE."
    )


def _write_check(ws: Path, nonce: str, reply: str) -> tuple[bool, str]:
    path = ws / f"out-{nonce}.txt"
    if not path.is_file():
        return False, "the file was not created"
    ok = path.read_text(encoding="utf-8", errors="replace").strip() == nonce[::-1]
    return ok, "file written" if ok else "the file holds something else"


def _run_task(ws: Path, nonce: str) -> str:
    return (
        "Run this command in your working directory and reply with exactly what it prints:\n"
        f"python -c \"import hashlib;print(hashlib.sha256(b'{nonce}').hexdigest()[:16])\""
    )


def _run_check(ws: Path, nonce: str, reply: str) -> tuple[bool, str]:
    expected = hashlib.sha256(nonce.encode()).hexdigest()[:16]
    return (
        expected in reply,
        "command output returned" if expected in reply else "the command's output is not in the reply",
    )


def _code_task(ws: Path, nonce: str) -> str:
    return (
        f"Write a Python file solution_{nonce}.py in your working directory that defines a function "
        "digit_sum(n: int) -> int returning the sum of the decimal digits of a non-negative integer. "
        "Then reply DONE."
    )


def _code_check(ws: Path, nonce: str, reply: str) -> tuple[bool, str]:
    path = ws / f"solution_{nonce}.py"
    if not path.is_file():
        return False, "no solution file"
    try:
        spec = importlib.util.spec_from_file_location(f"probe_solution_{nonce}", path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        cases = {0: 0, 7: 7, 1234: 10, 99999: 45, 10**12 + 5: 6}
        ok = all(module.digit_sum(n) == want for n, want in cases.items())
    except Exception as exc:  # noqa: BLE001 - a broken solution is a failed probe
        return False, f"the code does not run: {type(exc).__name__}"
    return ok, "the code passes" if ok else "the code gives wrong answers"


PROBES: dict[str, Probe] = {
    p.capability: p
    for p in (
        Probe("reply", "answers as asked", _reply_task, _reply_check),
        Probe("read_files", "reads a file in its workspace", _read_task, _read_check),
        Probe("write_files", "writes a file in its workspace", _write_task, _write_check),
        Probe("run_commands", "runs a shell command and reports its output", _run_task, _run_check),
        Probe("code", "writes code that works", _code_task, _code_check),
    )
}


# -- the matrix -------------------------------------------------------------------


@dataclass
class Observation:
    agent: str
    capability: str
    ok: bool
    elapsed_ms: int
    source: str = "probe"
    note: str = ""
    at: float = 0.0


@dataclass
class Cell:
    passes: int = 0
    fails: int = 0
    last_ok: bool | None = None
    last_at: float = 0.0
    total_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def observations(self) -> int:
        return self.passes + self.fails

    @property
    def rate(self) -> float:
        """Smoothed pass rate: an unobserved capability is an even 0.5."""
        return (self.passes + 1) / (self.observations + 2)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.observations if self.observations else 0.0


def default_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "capability_matrix.json"


class CapabilityMatrix:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def _load(self) -> dict[str, dict[str, Cell]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        known = set(Cell.__dataclass_fields__)
        return {
            agent: {cap: Cell(**{k: v for k, v in cell.items() if k in known}) for cap, cell in caps.items()}
            for agent, caps in raw.items()
            if isinstance(caps, dict)
        }

    def cells(self) -> dict[str, dict[str, Cell]]:
        return self._load()

    def cell(self, agent: str, capability: str) -> Cell:
        return self._load().get(agent, {}).get(capability, Cell())

    def record(self, observation: Observation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            data = self._load()
            cell = data.setdefault(observation.agent, {}).setdefault(observation.capability, Cell())
            if observation.ok:
                cell.passes += 1
            else:
                cell.fails += 1
            cell.last_ok = observation.ok
            cell.last_at = observation.at or time.time()
            cell.total_ms += max(0, observation.elapsed_ms)
            if observation.note:
                cell.notes = [*cell.notes, f"{observation.source}: {observation.note}"][-5:]
            payload = {a: {c: vars(v) for c, v in caps.items()} for a, caps in data.items()}
            atomic_replace(self.path, json.dumps(payload, ensure_ascii=False, indent=2))


# -- running probes ---------------------------------------------------------------


async def probe_agent(
    agent: str,
    runner: Runner,
    matrix: CapabilityMatrix,
    *,
    root: Path,
    capabilities: Sequence[str] | None = None,
    timeout_s: float = 180.0,
) -> list[Observation]:
    """Run each capability probe on ``agent`` in a fresh workspace and record what was seen."""
    out: list[Observation] = []
    for name in capabilities or list(PROBES):
        probe = PROBES[name]
        nonce = secrets.token_hex(6)
        workspace = root / f"{agent}-{name}-{nonce}"
        workspace.mkdir(parents=True, exist_ok=True)
        task = probe.prepare(workspace, nonce)
        started = time.monotonic()
        try:
            reply = await asyncio.wait_for(runner(agent, task, workspace), timeout_s)
            ok, note = await asyncio.to_thread(probe.check, workspace, nonce, reply or "")
        except asyncio.TimeoutError:
            ok, note = False, f"no answer in {timeout_s:.0f}s"
        except Exception as exc:  # noqa: BLE001 - a crashed run is a failed probe, recorded as such
            ok, note = False, f"the run failed: {str(exc)[:160]}"
        observation = Observation(
            agent=agent,
            capability=name,
            ok=ok,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            note=note,
            at=time.time(),
        )
        matrix.record(observation)
        out.append(observation)
    return out


# -- assignment ---------------------------------------------------------------------

_NEEDS: dict[str, tuple[str, ...]] = {
    "read_files": (r"\bread\b", r"\bfile", r"\blog\b", r"\bopen\b", r"прочит", r"файл"),
    "write_files": (r"\bwrite\b", r"\bcreate\b", r"\bsave\b", r"\bedit\b", r"\bfix\b", r"запиш", r"созда", r"исправ"),
    "run_commands": (
        r"\brun\b",
        r"\btests?\b",
        r"\bcommand",
        r"\bexecute\b",
        r"\bbuild\b",
        r"запуст",
        r"выполн",
        r"тест",
    ),
    "code": (
        r"\bcode\b",
        r"\bfunction\b",
        r"\bimplement",
        r"\brefactor",
        r"\bbug\b",
        r"\bclass\b",
        r"код",
        r"функци",
        r"реализ",
    ),
}


def infer_needs(task: str) -> set[str]:
    """The capabilities a task's words ask for; ``reply`` always."""
    text = task.lower()
    needs = {"reply"}
    for capability, patterns in _NEEDS.items():
        if any(re.search(p, text) for p in patterns):
            needs.add(capability)
    return needs


@dataclass(frozen=True)
class Ranked:
    agent: str
    score: float
    evidence: int
    avg_ms: float
    why: str


def rank(needs: Iterable[str], candidates: Iterable[str], matrix: CapabilityMatrix) -> list[Ranked]:
    """Candidates by how well their observations cover ``needs``, best first."""
    cells = matrix.cells()
    wanted = sorted(set(needs))
    out: list[Ranked] = []
    for agent in candidates:
        mine = cells.get(agent, {})
        score, evidence, ms, parts = 1.0, 0, 0.0, []
        for need in wanted:
            cell = mine.get(need, Cell())
            score *= cell.rate
            evidence += cell.observations
            ms += cell.avg_ms
            parts.append(f"{need} {cell.passes}/{cell.observations}" if cell.observations else f"{need} unprobed")
        out.append(Ranked(agent=agent, score=score, evidence=evidence, avg_ms=ms, why=", ".join(parts)))
    return sorted(out, key=lambda r: (-r.score, -r.evidence, r.avg_ms, r.agent))


def choose(
    task: str, candidates: Iterable[str], matrix: CapabilityMatrix, *, require_evidence: bool = True
) -> Ranked | None:
    """The best candidate for ``task``.

    Only agents the matrix has seen run are picked by default: an unprobed
    agent's even 0.5 would otherwise outrank one that was tried and fell short,
    and the agent picked blind may not even start (one that needs a model the
    host has no key for).
    """
    pool = list(candidates)
    if require_evidence:
        seen = matrix.cells()
        pool = [a for a in pool if seen.get(a)]
    ranked = rank(infer_needs(task), pool, matrix)
    return ranked[0] if ranked else None


def record_run(agent: str, task: str, ok: bool, elapsed_ms: int, matrix: CapabilityMatrix, *, note: str = "") -> None:
    """A real run is evidence too: it counts for each capability the task needed."""
    for need in infer_needs(task):
        matrix.record(Observation(agent, need, ok, elapsed_ms, source="run", note=note, at=time.time()))


__all__ = [
    "PROBES",
    "CapabilityMatrix",
    "Cell",
    "Observation",
    "Probe",
    "Ranked",
    "choose",
    "infer_needs",
    "probe_agent",
    "rank",
    "record_run",
]

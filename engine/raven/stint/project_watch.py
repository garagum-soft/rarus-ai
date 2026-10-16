"""The project watch: problems found and filed as tasks, with nobody asking.

A person registers a project once (``raven watch add``) with the checks that
say it is healthy -- its tests, its linter, its build -- and, optionally, the
GitHub repository whose CI runs on it. From then on a resident host scans it on
its own: when the checked-out commit moved or the interval passed, every check
is run as a real command (``raven.stint.verify.run_check``: the project tree as
the working directory, a timeout, the process group killed on expiry, an
environment without the host's secrets), and the CI runs of the branch are read
from the GitHub API.

A failure is a *finding*. A new one is filed as a task on the project's
backlog (``.stint/backlog.json``, role ``scanner``, its source naming the
check, so a later scan finds it filed instead of filing it twice) and raised on
the operator's attention queue, which notifies through the rules a person set
(F44, F61). A finding that clears -- the check passes again, CI goes green --
resolves its attention item; the task stays for whoever owns the backlog to
close, because a green run is evidence, not a verdict.

Nothing runs until a project is registered: with an empty watch list the
gateway builds no watch at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from raven.utils.atomic_io import atomic_replace, write_transaction

logger = logging.getLogger(__name__)

DEFAULT_EVERY_S = 3600
MIN_EVERY_S = 60
TICK_S = 60.0
SOURCE_PREFIX = "raven-watch:"

Fetch = Callable[[str, dict[str, str]], dict[str, Any]]


@dataclass
class WatchCheck:
    name: str
    run: str
    timeout_s: float = 900.0


@dataclass
class WatchedProject:
    path: str
    every_s: int = DEFAULT_EVERY_S
    checks: list[WatchCheck] = field(default_factory=list)
    github: str = ""
    """``owner/repo`` whose CI runs to read; empty reads none."""
    branch: str = ""
    """The branch whose CI is read; empty is the checkout's own."""
    last_scan_at: float = 0.0
    last_head: str = ""
    failing: dict[str, str] = field(default_factory=dict)
    """Finding key -> its one-line detail, as of the last scan."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WatchedProject:
        checks = [WatchCheck(**c) for c in raw.get("checks") or [] if isinstance(c, dict)]
        known = set(cls.__dataclass_fields__) - {"checks"}
        return cls(checks=checks, **{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True)
class Finding:
    key: str
    """``check:<name>`` or ``ci:<name>``; with the project, the finding's identity."""
    title: str
    detail: str
    priority: str = "normal"
    link: str = ""


@dataclass
class ScanResult:
    project: str
    head: str
    scanned: bool
    reason: str = ""
    findings: list[Finding] = field(default_factory=list)
    passing: list[str] = field(default_factory=list)
    """Keys checked this scan that did not fail."""


def default_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "watch.json"


def _project_id(path: str) -> str:
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:10]


class WatchStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def projects(self) -> list[WatchedProject]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [WatchedProject.from_dict(p) for p in raw.get("projects") or [] if isinstance(p, dict)]

    def _save(self, projects: list[WatchedProject]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.path, json.dumps({"projects": [asdict(p) for p in projects]}, ensure_ascii=False, indent=2))

    def put(self, project: WatchedProject) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            rest = [p for p in self.projects() if Path(p.path).resolve() != Path(project.path).resolve()]
            self._save([*rest, project])

    def remove(self, path: str) -> bool:
        if not self.path.exists():
            return False
        with write_transaction(self.path):
            projects = self.projects()
            rest = [p for p in projects if Path(p.path).resolve() != Path(path).resolve()]
            if len(rest) == len(projects):
                return False
            self._save(rest)
        return True

    def update(self, project: WatchedProject) -> None:
        """Write back a scanned project, unless it was removed meanwhile."""
        with write_transaction(self.path):
            projects = self.projects()
            for i, p in enumerate(projects):
                if Path(p.path).resolve() == Path(project.path).resolve():
                    projects[i] = project
                    self._save(projects)
                    return


# -- scanning ------------------------------------------------------------------------------


def _git(project: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(project), *args],  # noqa: S607 - git location varies; PATH lookup intended
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _tail_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:240] if lines else ""


def _run_checks(project: WatchedProject, log_dir: Path) -> tuple[list[Finding], list[str]]:
    from raven.stint.verify import CheckSpec, curated_env, run_check

    findings, passing = [], []
    for check in project.checks:
        result = run_check(
            CheckSpec(name=check.name, command=check.run, timeout_sec=check.timeout_s),
            cwd=Path(project.path),
            log_dir=log_dir,
            env=curated_env(),
        )
        key = f"check:{check.name}"
        if result.ok:
            passing.append(key)
            continue
        said = _tail_line(result.stderr_tail) or _tail_line(result.stdout_tail)
        detail = result.summary().detail + (f": {said}" if said else "")
        findings.append(
            Finding(
                key=key,
                title=f"{check.name} fails in {Path(project.path).name}",
                detail=f"`{check.run}` -- {detail}",
                link=str(result.log_path or ""),
            )
        )
    return findings, passing


_RED = ("failure", "timed_out", "cancelled", "action_required", "startup_failure")


def _ci(project: WatchedProject, branch: str, fetch: Fetch, token: str) -> tuple[list[Finding], list[str]]:
    """The finished CI runs of ``branch``: a red one is a finding, a green one passes."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{project.github}/commits/{branch}/check-runs?per_page=100"
    data = fetch(url, headers)
    findings, passing = [], []
    for run in data.get("check_runs") or []:
        if not isinstance(run, dict) or run.get("status") != "completed":
            continue
        name = str(run.get("name") or "ci")
        key = f"ci:{name}"
        if run.get("conclusion") in _RED:
            out = run.get("output") if isinstance(run.get("output"), dict) else {}
            said = str(out.get("title") or out.get("summary") or "").strip().splitlines()
            findings.append(
                Finding(
                    key=key,
                    title=f"CI {name} is {run.get('conclusion')} on {project.github}@{branch}",
                    detail=(said[0][:240] if said else f"conclusion {run.get('conclusion')}"),
                    priority="high",
                    link=str(run.get("html_url") or ""),
                )
            )
        elif run.get("conclusion") in ("success", "neutral", "skipped"):
            passing.append(key)
    return findings, passing


def scan(
    project: WatchedProject,
    *,
    now: float | None = None,
    force: bool = False,
    fetch: Fetch | None = None,
    log_root: Path | None = None,
) -> ScanResult:
    """One scan, when it is due: the commit moved or ``every_s`` passed (or ``force``)."""
    now = time.time() if now is None else now
    root = Path(project.path)
    head = _git(root, "rev-parse", "HEAD")
    if not root.is_dir():
        return ScanResult(project.path, head, scanned=False, reason="the project directory is gone")
    due = force or head != project.last_head or now - project.last_scan_at >= max(project.every_s, MIN_EVERY_S)
    if not due:
        return ScanResult(project.path, head, scanned=False, reason="not due: same commit, interval not passed")
    if log_root is None:
        log_root = default_path().parent / "watch" / "logs"
    result = ScanResult(project.path, head, scanned=True)
    found, passing = _run_checks(project, log_root / _project_id(project.path))
    result.findings.extend(found)
    result.passing.extend(passing)
    if project.github:
        branch = project.branch or _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
        from raven.stint.trackers import TrackerError, _http_get

        try:
            found, passing = _ci(project, branch, fetch or _http_get, os.environ.get("GITHUB_TOKEN", ""))
            result.findings.extend(found)
            result.passing.extend(passing)
        except (TrackerError, OSError, ValueError) as exc:
            result.reason = f"CI not read: {exc}"
    return result


# -- filing ----------------------------------------------------------------------------------


def _attention_id(project: str, key: str) -> str:
    return f"watch:{_project_id(project)}:{key}"


def file_findings(project: WatchedProject, result: ScanResult, *, queue: Any = None) -> dict[str, list[str]]:
    """Task and attention item for each new finding; cleared findings resolved. What was done."""
    from raven.proactive_engine import attention
    from raven.stint import backlog as backlog_mod

    queue = queue or attention.queue()
    done: dict[str, list[str]] = {"filed": [], "already_filed": [], "cleared": []}
    root = Path(project.path)
    if result.findings:
        try:
            log = backlog_mod.start(root)
        except backlog_mod.BacklogError as exc:
            logger.warning("project watch: %s has an unreadable backlog: %s", root, exc)
            log = None
        changed = False
        for f in result.findings:
            source = f"{SOURCE_PREFIX}{f.key}"
            open_task = None
            if log is not None:
                open_task = next((t for t in log.tasks if t.source == source and not t.closed), None)
            if open_task is not None:
                done["already_filed"].append(f.key)
                task_id = open_task.id
            elif log is not None:
                task = backlog_mod.apply(
                    log,
                    "add",
                    role=backlog_mod.SCANNER,
                    title=f.title,
                    source=source,
                    severity=f.priority,
                    note=(f.detail + (f" ({f.link})" if f.link else ""))[:1000],
                )
                task_id = task.id
                changed = True
                done["filed"].append(f.key)
            else:
                task_id = 0
            queue.raise_item(
                _attention_id(project.path, f.key),
                kind="discovered_task",
                priority=f.priority,
                title=f.title,
                detail=f.detail,
                source="project_watch",
                data={"project": project.path, "key": f.key, "task": task_id, "link": f.link},
            )
        if changed and log is not None:
            backlog_mod.save(root, log)
    for key in result.passing:
        if key in project.failing:
            queue.resolve(_attention_id(project.path, key), "passing again")
            done["cleared"].append(key)
    return done


def record_scan(project: WatchedProject, result: ScanResult, now: float | None = None) -> WatchedProject:
    """The project as the scan leaves it: when, at which commit, what is failing."""
    if not result.scanned:
        return project
    failing = {f.key: f.detail for f in result.findings}
    return WatchedProject(
        path=project.path,
        every_s=project.every_s,
        checks=project.checks,
        github=project.github,
        branch=project.branch,
        last_scan_at=time.time() if now is None else now,
        last_head=result.head,
        failing=failing,
    )


def watch_once(
    store: WatchStore, *, now: float | None = None, force: bool = False, fetch: Fetch | None = None
) -> list[tuple[ScanResult, dict[str, list[str]]]]:
    """Scan every registered project that is due and file what it found."""
    out = []
    for project in store.projects():
        try:
            result = scan(project, now=now, force=force, fetch=fetch)
            done = (
                file_findings(project, result) if result.scanned else {"filed": [], "already_filed": [], "cleared": []}
            )
            store.update(record_scan(project, result, now))
        except Exception as exc:  # noqa: BLE001 - one project's failure must not stop the others
            logger.warning("project watch: scanning %s failed: %s", project.path, exc)
            continue
        out.append((result, done))
    return out


class ProjectWatch:
    """The resident loop: a pass over the watch list every minute, each project when it is due."""

    def __init__(self, store: WatchStore | None = None, *, tick_s: float = TICK_S) -> None:
        self.store = store or WatchStore()
        self.tick_s = tick_s
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(watch_once, self.store)
            except Exception:  # noqa: BLE001 - a bad pass is logged; the next one runs
                logger.exception("project watch pass failed")
            await asyncio.sleep(self.tick_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="project-watch")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


__all__ = [
    "DEFAULT_EVERY_S",
    "Finding",
    "ProjectWatch",
    "ScanResult",
    "WatchCheck",
    "WatchStore",
    "WatchedProject",
    "file_findings",
    "record_scan",
    "scan",
    "watch_once",
]

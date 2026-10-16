"""Continuous integration as an agent reads it: a commit's checks, and why the red ones are red.

GitHub is the CI that ships. Both of its reporting channels are read: the check
runs (GitHub Actions and every app that reports through the Checks API) and the
commit statuses (the older API that other CI services report through), for a
branch, a commit or a pull request's head. A red check is explained with what
the CI itself said: its annotations (file, line, message) and, for a GitHub
Actions job, the error lines of its log with the lines that led up to them.

Read-only: nothing here writes to GitHub, pushes or re-runs anything. Every
request goes to api.github.com with ``GITHUB_TOKEN`` when it is set -- needed
for a private repository and for job logs, and for polling, since an anonymous
caller gets sixty requests an hour.

``wait`` polls until every check has finished (or the first one fails, when
asked), and gives a freshly pushed commit a grace period for its checks to
appear rather than calling an empty list "no CI".
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from raven.stint.trackers import TrackerError, _http_get

API = "https://api.github.com"

Fetch = Callable[[str, dict[str, str]], Any]
FetchText = Callable[[str, dict[str, str]], str]

RED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale", "error"})
"""Conclusions that fail a commit; ``error`` is a commit status's, the rest are check runs'."""
GREEN = frozenset({"success", "neutral", "skipped"})

DEFAULT_APPEAR_S = 120.0
"""How long a commit may show no checks at all before ``wait`` takes that as the answer."""

_LOG_LINES = 60
_LOG_CONTEXT = 12
_ANNOTATIONS = 20
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")


class CiError(TrackerError):
    """A repository, ref or answer that cannot be read."""


@dataclass
class Check:
    name: str
    status: str
    """``completed``, or what GitHub says while it is not (queued, in_progress, pending...)."""
    conclusion: str = ""
    url: str = ""
    summary: str = ""
    source: str = "checks"
    """``checks`` (a check run) or ``status`` (a commit status)."""
    run_id: int = 0
    """The check run's id; for a GitHub Actions run it is also the job's."""
    app: str = ""
    sha: str = ""

    @property
    def pending(self) -> bool:
        return self.status != "completed"

    @property
    def red(self) -> bool:
        return not self.pending and self.conclusion in RED


@dataclass
class Failure:
    name: str
    annotations: list[str] = field(default_factory=list)
    log: str = ""
    note: str = ""
    """Why the log or annotations could not be read, when they could not."""


@dataclass
class CiReport:
    repo: str
    ref: str
    sha: str = ""
    checks: list[Check] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    waited_s: float = 0.0
    timed_out: bool = False

    @property
    def state(self) -> str:
        """``none`` (no checks), ``pending``, ``failure`` or ``success``.

        ``failure`` as soon as any finished check is red, whatever is still
        running: the commit cannot go green any more.
        """
        if not self.checks:
            return "none"
        if any(c.red for c in self.checks):
            return "failure"
        if any(c.pending for c in self.checks):
            return "pending"
        return "success"

    @property
    def red(self) -> list[Check]:
        return [c for c in self.checks if c.red]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state}

    def render(self, *, local_head: str = "") -> str:
        """The report as an agent or a person reads it."""
        what = f"{self.repo}@{self.ref}" + (f" ({self.sha[:12]})" if self.sha and self.sha != self.ref else "")
        lines = [f"CI for {what}: {self.state.upper()}"]
        if self.timed_out:
            lines[0] += f" (still not finished after waiting {self.waited_s:.0f}s)"
        elif self.waited_s:
            lines[0] += f" (after waiting {self.waited_s:.0f}s)"
        if local_head and self.sha and local_head != self.sha:
            lines.append(
                f"Note: the checkout is at {local_head[:12]}, CI ran on {self.sha[:12]} -- the local commit may not be pushed yet."
            )
        if not self.checks:
            lines.append("No checks or statuses are reported for this commit (no CI, or it has not started yet).")
        order = {"failure": 0, "pending": 1, "success": 2}
        for c in sorted(
            self.checks, key=lambda c: (order["failure" if c.red else "pending" if c.pending else "success"], c.name)
        ):
            said = c.conclusion if not c.pending else c.status
            tail = f" -- {c.summary}" if c.summary else ""
            link = f" <{c.url}>" if c.url else ""
            lines.append(f"- [{said}] {c.name}{tail}{link}")
        for f in self.failures:
            lines.append("")
            lines.append(f"## {f.name}")
            if f.annotations:
                lines.append("Annotations:")
                lines.extend(f"  {a}" for a in f.annotations)
            if f.log:
                lines.append("Log (error lines and what led up to them):")
                lines.append(f.log)
            if f.note:
                lines.append(f"({f.note})")
        return "\n".join(lines)


# -- where -------------------------------------------------------------------------------------

_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


def parse_remote(url: str) -> str:
    """``owner/repo`` from a GitHub remote URL (https or ssh); empty for anything else."""
    found = _REMOTE.match((url or "").strip())
    return f"{found.group(1)}/{found.group(2)}" if found else ""


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


def repo_of(project: Path) -> str:
    """The GitHub repository a checkout's ``origin`` points at, or empty."""
    return parse_remote(_git(project, "remote", "get-url", "origin"))


def branch_of(project: Path) -> str:
    """The checkout's branch, or empty on a detached head."""
    name = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
    return "" if name in ("", "HEAD") else name


def head_of(project: Path) -> str:
    return _git(project, "rev-parse", "HEAD")


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _check_repo(repo: str) -> str:
    repo = (repo or "").strip().removesuffix(".git")
    if not _REPO.match(repo):
        raise CiError(f"{repo!r} is not a GitHub repository; give it as owner/repo")
    return repo


# -- reading -----------------------------------------------------------------------------------


def headers(token: str | None = None) -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "") if token is None else token
    out = {"Accept": "application/vnd.github+json", "User-Agent": "raven-ci", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        out["Authorization"] = f"Bearer {token}"
    return out


def _get_json(url: str, hdrs: dict[str, str]) -> Any:
    """The tracker's GitHub GET, with a network failure reported as a :class:`CiError`."""
    import httpx

    try:
        return _http_get(url, hdrs)
    except httpx.HTTPError as exc:
        raise CiError(f"GitHub could not be reached: {exc}") from exc


def resolve_pr(repo: str, number: int, *, fetch: Fetch | None = None, token: str | None = None) -> tuple[str, str]:
    """A pull request's head: its branch and commit."""
    fetch = fetch or _get_json
    data = fetch(f"{API}/repos/{_check_repo(repo)}/pulls/{int(number)}", headers(token))
    head = data.get("head") if isinstance(data, dict) else None
    if not isinstance(head, dict) or not head.get("sha"):
        raise CiError(f"{repo}#{number}: no head commit in GitHub's answer")
    return str(head.get("ref") or ""), str(head["sha"])


def read(repo: str, ref: str, *, fetch: Fetch | None = None, token: str | None = None) -> CiReport:
    """Every check run and commit status of ``ref`` (a branch, a tag or a commit), as of now."""
    repo = _check_repo(repo)
    if not ref or not ref.strip():
        raise CiError("no ref to read CI for")
    fetch = fetch or _get_json
    hdrs = headers(token)
    report = CiReport(repo=repo, ref=ref)
    at = quote(ref.strip(), safe="")
    runs = fetch(f"{API}/repos/{repo}/commits/{at}/check-runs?per_page=100", hdrs)
    for run in (runs.get("check_runs") if isinstance(runs, dict) else None) or []:
        if not isinstance(run, dict):
            continue
        out = run.get("output") if isinstance(run.get("output"), dict) else {}
        said = str(out.get("title") or out.get("summary") or "").strip().splitlines()
        app = run.get("app") if isinstance(run.get("app"), dict) else {}
        report.checks.append(
            Check(
                name=str(run.get("name") or "check"),
                status=str(run.get("status") or "queued"),
                conclusion=str(run.get("conclusion") or ""),
                url=str(run.get("html_url") or run.get("details_url") or ""),
                summary=said[0][:200] if said else "",
                source="checks",
                run_id=int(run.get("id") or 0),
                app=str(app.get("slug") or ""),
                sha=str(run.get("head_sha") or ""),
            )
        )
    status = fetch(f"{API}/repos/{repo}/commits/{at}/status", hdrs)
    if isinstance(status, dict):
        report.sha = str(status.get("sha") or "")
        for s in status.get("statuses") or []:
            if not isinstance(s, dict):
                continue
            state = str(s.get("state") or "pending")
            report.checks.append(
                Check(
                    name=str(s.get("context") or "status"),
                    status="pending" if state == "pending" else "completed",
                    conclusion="" if state == "pending" else state,
                    url=str(s.get("target_url") or ""),
                    summary=str(s.get("description") or "")[:200],
                    source="status",
                )
            )
    if not report.sha:
        report.sha = next((c.sha for c in report.checks if c.sha), "")
    return report


def _get_text(url: str, hdrs: dict[str, str]) -> str:
    """A job log: GitHub answers with a redirect to storage that must not be sent the token."""
    import httpx

    try:
        first = httpx.get(url, headers=hdrs, timeout=30.0, follow_redirects=False)
        if first.status_code in (301, 302, 303, 307, 308) and first.headers.get("location"):
            second = httpx.get(first.headers["location"], timeout=60.0, follow_redirects=True)
            if second.status_code >= 400:
                raise CiError(f"the log storage answered {second.status_code}")
            return second.text
    except httpx.HTTPError as exc:
        raise CiError(f"the log could not be fetched: {exc}") from exc
    if first.status_code == 404:
        raise CiError("not found (gone, or a private repository without GITHUB_TOKEN)")
    if first.status_code in (401, 403):
        raise CiError(f"GitHub answered {first.status_code}: reading job logs needs GITHUB_TOKEN")
    if first.status_code >= 400:
        raise CiError(f"GitHub answered {first.status_code}")
    return first.text


def log_excerpt(text: str, *, max_lines: int = _LOG_LINES, context: int = _LOG_CONTEXT) -> str:
    """The error lines of a GitHub Actions log and the lines that led up to each; else its tail."""
    lines = [_STAMP.sub("", line.rstrip()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return ""
    errors = [i for i, line in enumerate(lines) if "##[error]" in line]
    if not errors:
        return "\n".join(lines[-max_lines:])
    keep: list[int] = []
    for i in errors:
        for j in range(max(0, i - context), i + 1):
            if j not in keep:
                keep.append(j)
    keep = keep[-max_lines:]
    out, last = [], None
    for j in keep:
        if last is not None and j != last + 1:
            out.append("...")
        out.append(lines[j].replace("##[error]", "ERROR: "))
        last = j
    return "\n".join(out)


def explain(
    report: CiReport,
    *,
    fetch: Fetch | None = None,
    fetch_text: FetchText | None = None,
    token: str | None = None,
    logs: bool = True,
) -> CiReport:
    """Fill ``report.failures`` with what the CI said about each red check."""
    fetch = fetch or _get_json
    fetch_text = fetch_text or _get_text
    hdrs = headers(token)
    report.failures = []
    for check in report.red:
        failure = Failure(name=check.name)
        notes = []
        if check.source == "checks" and check.run_id:
            try:
                found = fetch(f"{API}/repos/{report.repo}/check-runs/{check.run_id}/annotations?per_page=50", hdrs)
                for a in (found if isinstance(found, list) else [])[:_ANNOTATIONS]:
                    if not isinstance(a, dict):
                        continue
                    where = str(a.get("path") or "")
                    if a.get("start_line"):
                        where += f":{a.get('start_line')}"
                    level = str(a.get("annotation_level") or "")
                    msg = " ".join(str(a.get("message") or "").split())[:300]
                    failure.annotations.append(f"{where} [{level}] {msg}".strip())
            except (TrackerError, OSError, ValueError) as exc:
                notes.append(f"annotations not read: {exc}")
            if logs and check.app == "github-actions":
                try:
                    failure.log = log_excerpt(
                        fetch_text(f"{API}/repos/{report.repo}/actions/jobs/{check.run_id}/logs", hdrs)
                    )
                except (TrackerError, OSError, ValueError) as exc:
                    notes.append(f"log not read: {exc}")
        elif check.source == "status":
            notes.append("reported through the commit status API; open the link for its log")
        if not failure.annotations and not failure.log and check.summary:
            failure.note = check.summary
        if notes:
            failure.note = "; ".join([*([failure.note] if failure.note else []), *notes])
        report.failures.append(failure)
    return report


def _poll_every(token: str, every_s: float | None) -> float:
    # Two requests a poll, and an anonymous caller has sixty an hour.
    return every_s if every_s is not None else (30.0 if token else 90.0)


def _settled(report: CiReport, waited: float, *, timeout_s: float, fail_fast: bool, appear_s: float) -> bool:
    """Whether a poll may stop at ``report``; marks it timed out when that is why."""
    report.waited_s = round(waited, 1)
    state = report.state
    if state == "success" or (state == "failure" and (fail_fast or not any(c.pending for c in report.checks))):
        return True
    # A push is seen by the API before its workflows are: no checks at all is
    # only an answer once they had time to appear.
    if state == "none" and waited >= min(appear_s, timeout_s):
        return True
    if waited >= timeout_s:
        report.timed_out = True
        return True
    return False


def wait(
    repo: str,
    ref: str,
    *,
    timeout_s: float,
    every_s: float | None = None,
    fail_fast: bool = False,
    appear_s: float = DEFAULT_APPEAR_S,
    fetch: Fetch | None = None,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CiReport:
    """Poll ``ref`` until its CI has finished, failed (with ``fail_fast``) or ``timeout_s`` passed.

    A commit with no checks at all is given ``appear_s`` for them to show up and
    only then reported as ``none``.
    """
    token = os.environ.get("GITHUB_TOKEN", "") if token is None else token
    every = _poll_every(token, every_s)
    began = clock()
    while True:
        report = read(repo, ref, fetch=fetch, token=token)
        waited = clock() - began
        if _settled(report, waited, timeout_s=timeout_s, fail_fast=fail_fast, appear_s=appear_s):
            return report
        sleep(max(1.0, min(every, timeout_s - waited)))


async def wait_async(
    repo: str,
    ref: str,
    *,
    timeout_s: float,
    every_s: float | None = None,
    fail_fast: bool = False,
    appear_s: float = DEFAULT_APPEAR_S,
    fetch: Fetch | None = None,
    token: str | None = None,
) -> CiReport:
    """:func:`wait` for an event loop: each read in a thread, the pause on the loop, so a cancel stops it."""
    token = os.environ.get("GITHUB_TOKEN", "") if token is None else token
    every = _poll_every(token, every_s)
    began = time.monotonic()
    while True:
        report = await asyncio.to_thread(read, repo, ref, fetch=fetch, token=token)
        waited = time.monotonic() - began
        if _settled(report, waited, timeout_s=timeout_s, fail_fast=fail_fast, appear_s=appear_s):
            return report
        await asyncio.sleep(max(1.0, min(every, timeout_s - waited)))


__all__ = [
    "API",
    "GREEN",
    "RED",
    "Check",
    "CiError",
    "CiReport",
    "Failure",
    "branch_of",
    "explain",
    "head_of",
    "headers",
    "log_excerpt",
    "parse_remote",
    "read",
    "repo_of",
    "resolve_pr",
    "wait",
    "wait_async",
]

"""External issue trackers: turning a link to an issue into a task on the backlog.

A provider recognises its own links and reads the issue they point at; GitHub
Issues is the one that ships. Importing files the issue as a backlog task with
the same title and the link as its ``source``, so the task always says where it
came from. The link is the task's identity: importing it again finds the task
already filed instead of filing a second one, and never touches the task's
state, which is the stint's own.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from raven.stint import backlog as backlog_mod

Fetch = Callable[[str, dict[str, str]], dict[str, Any]]


class TrackerError(ValueError):
    """A link no provider recognises, or an issue that could not be read."""


@dataclass(frozen=True)
class ExternalIssue:
    url: str
    title: str
    body: str = ""
    state: str = ""
    labels: list[str] = field(default_factory=list)
    tracker: str = ""


class TrackerProvider(Protocol):
    name: str

    def matches(self, link: str) -> bool: ...

    def fetch(self, link: str) -> ExternalIssue: ...


_GITHUB_URL = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/(\d+)/?(?:[?#].*)?$")
_GITHUB_SHORT = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")


def _http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    import httpx

    response = httpx.get(url, headers=headers, timeout=20.0, follow_redirects=True)
    if response.status_code == 404:
        raise TrackerError(f"{url}: not found (a private repository needs GITHUB_TOKEN)")
    if response.status_code >= 400:
        raise TrackerError(f"{url}: GitHub answered {response.status_code}")
    return response.json()


class GitHubIssues:
    """GitHub issues and pull requests, read through the REST API; GITHUB_TOKEN is used when set."""

    name = "github"

    def __init__(self, fetch: Fetch | None = None, token: str | None = None) -> None:
        self._fetch = fetch or _http_get
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")

    @staticmethod
    def parse(link: str) -> tuple[str, str, int] | None:
        found = _GITHUB_URL.match(link.strip()) or _GITHUB_SHORT.match(link.strip())
        return (found.group(1), found.group(2), int(found.group(3))) if found else None

    def matches(self, link: str) -> bool:
        return self.parse(link) is not None

    def fetch(self, link: str) -> ExternalIssue:
        parsed = self.parse(link)
        if parsed is None:
            raise TrackerError(f"{link} is not a GitHub issue link")
        owner, repo, number = parsed
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "raven-tracker"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = self._fetch(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", headers)
        if data.get("pull_request"):
            kind = "pull request"
        else:
            kind = "issue"
        title = str(data.get("title") or "").strip()
        if not title:
            raise TrackerError(f"{link}: the {kind} has no title")
        return ExternalIssue(
            url=str(data.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}"),
            title=title,
            body=str(data.get("body") or ""),
            state=str(data.get("state") or ""),
            labels=[str(label.get("name")) for label in data.get("labels") or [] if isinstance(label, dict)],
            tracker=self.name,
        )


def provider_for(link: str, providers: Sequence[TrackerProvider] | None = None) -> TrackerProvider:
    for provider in providers if providers is not None else (GitHubIssues(),):
        if provider.matches(link):
            return provider
    raise TrackerError(
        f"no tracker recognises {link!r}; supported: GitHub issue and pull request links (owner/repo#N or the URL)"
    )


def _summary_note(issue: ExternalIssue, limit: int = 400) -> str:
    body = " ".join(issue.body.split())
    parts = [f"imported from {issue.tracker}"]
    if issue.state:
        parts.append(f"state {issue.state}")
    if issue.labels:
        parts.append("labels " + ", ".join(issue.labels))
    note = "; ".join(parts)
    return f"{note}. {body[:limit]}{'...' if len(body) > limit else ''}" if body else note


def import_issue(
    project: Path,
    link: str,
    *,
    role: str = backlog_mod.HUMAN,
    providers: Sequence[TrackerProvider] | None = None,
) -> tuple[backlog_mod.Task, bool]:
    """File the linked issue as a backlog task; returns the task and whether it was new."""
    issue = provider_for(link, providers).fetch(link)
    backlog = backlog_mod.start(project)
    for task in backlog.tasks:
        if task.source == issue.url:
            return task, False
    task = backlog_mod.apply(backlog, "add", role=role, title=issue.title, source=issue.url, note=_summary_note(issue))
    backlog_mod.save(project, backlog)
    return task, True


__all__ = ["ExternalIssue", "GitHubIssues", "TrackerError", "TrackerProvider", "import_issue", "provider_for"]

"""External trackers: a link to an issue becomes a backlog task with its title and the link as source."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.stint import backlog as backlog_mod
from raven.stint.trackers import GitHubIssues, TrackerError, import_issue, provider_for

URL = "https://github.com/acme/widgets/issues/42"


class _GitHub:
    """What the REST API answers for one issue, and every request made of it."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.data = data or {
            "html_url": URL,
            "title": "Crash when the cart is empty",
            "body": "Steps:\n1. open the cart\n2. press pay",
            "state": "open",
            "labels": [{"name": "bug"}],
        }

    def __call__(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        self.requests.append((url, headers))
        return self.data


def test_links_are_recognised_in_both_spellings() -> None:
    assert GitHubIssues.parse(URL) == ("acme", "widgets", 42)
    assert GitHubIssues.parse("acme/widgets#42") == ("acme", "widgets", 42)
    assert GitHubIssues.parse(URL + "#issuecomment-1") == ("acme", "widgets", 42)
    assert GitHubIssues.parse("https://github.com/acme/widgets/pull/7") == ("acme", "widgets", 7)
    assert GitHubIssues.parse("https://github.com/acme/widgets/commit/abc") is None
    with pytest.raises(TrackerError, match="no tracker recognises"):
        provider_for("https://example.com/ticket/1")


def test_the_issue_is_read_from_the_rest_api_with_the_token_when_there_is_one() -> None:
    api = _GitHub()
    issue = GitHubIssues(fetch=api, token="t0k").fetch("acme/widgets#42")

    assert api.requests[0][0] == "https://api.github.com/repos/acme/widgets/issues/42"
    assert api.requests[0][1]["Authorization"] == "Bearer t0k"
    assert (issue.title, issue.url, issue.state, issue.labels) == ("Crash when the cart is empty", URL, "open", ["bug"])


def test_an_issue_becomes_a_task_with_the_same_title_and_the_link_as_source(tmp_path: Path) -> None:
    task, new = import_issue(tmp_path, URL, providers=[GitHubIssues(fetch=_GitHub(), token="")])

    assert new
    stored = backlog_mod.load(tmp_path).get(task.id)
    assert stored.title == "Crash when the cart is empty"
    assert stored.source == URL
    assert stored.state == backlog_mod.OPEN
    assert "imported from github" in stored.note and "labels bug" in stored.note and "open the cart" in stored.note


def test_importing_the_same_link_twice_files_one_task(tmp_path: Path) -> None:
    provider = GitHubIssues(fetch=_GitHub(), token="")
    first, _ = import_issue(tmp_path, URL, providers=[provider])
    again, new = import_issue(tmp_path, "acme/widgets#42", providers=[provider])

    assert not new and again.id == first.id
    assert len(backlog_mod.load(tmp_path).tasks) == 1


def test_an_issue_with_no_title_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TrackerError, match="no title"):
        import_issue(tmp_path, URL, providers=[GitHubIssues(fetch=_GitHub({"title": ""}), token="")])
    assert not backlog_mod.backlog_path(tmp_path).exists()


def test_the_command_line_imports_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli.commands import app
    from raven.stint import trackers

    monkeypatch.setattr(trackers, "_http_get", _GitHub())
    runner = CliRunner()

    result = runner.invoke(app, ["playbook", "stint", "task", "import", URL, "--project", str(tmp_path)])
    again = runner.invoke(app, ["playbook", "stint", "task", "import", URL, "--project", str(tmp_path)])
    bad = runner.invoke(app, ["playbook", "stint", "task", "import", "nope", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "imported" in result.output and "Crash when the cart is empty" in result.output
    assert "already on the backlog" in again.output
    assert bad.exit_code == 1 and "no tracker recognises" in bad.output
    assert [t.source for t in backlog_mod.load(tmp_path).tasks] == [URL]

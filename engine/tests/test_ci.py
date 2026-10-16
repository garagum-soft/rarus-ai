"""Reading CI for agents (raven/stint/ci.py), the ``ci_status`` tool, ``raven ci`` and the ``ci-fix`` scenario.

GitHub is replaced by a fake that answers the same REST shapes; nothing here
reaches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.stint import ci

API = ci.API


def _run(
    name: str,
    conclusion: str | None,
    *,
    status: str = "completed",
    run_id: int = 1,
    app: str = "github-actions",
    sha: str = "a" * 40,
    title: str = "",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/runs/{run_id}",
        "head_sha": sha,
        "app": {"slug": app},
        "output": {"title": title},
    }


class FakeGitHub:
    """The REST answers ``ci`` asks for, keyed by URL suffix; records what was asked and with which headers."""

    def __init__(
        self,
        runs: list[dict[str, Any]] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        sha: str = "a" * 40,
    ) -> None:
        self.runs = runs or []
        self.statuses = statuses or []
        self.sha = sha
        self.annotations: dict[int, list[dict[str, Any]]] = {}
        self.logs: dict[int, str] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.asked: list[tuple[str, dict[str, str]]] = []

    def json(self, url: str, headers: dict[str, str]) -> Any:
        self.asked.append((url, headers))
        path = url.removeprefix(API)
        if "/check-runs?" in path and "/commits/" in path:
            return {"total_count": len(self.runs), "check_runs": self.runs}
        if path.endswith("/status"):
            return {"state": "pending", "sha": self.sha, "statuses": self.statuses}
        if "/annotations" in path:
            run_id = int(path.split("/check-runs/")[1].split("/")[0])
            return self.annotations.get(run_id, [])
        if "/pulls/" in path:
            return self.pulls[int(path.rsplit("/", 1)[1])]
        raise AssertionError(f"unexpected request {url}")

    def text(self, url: str, headers: dict[str, str]) -> str:
        self.asked.append((url, headers))
        run_id = int(url.split("/actions/jobs/")[1].split("/")[0])
        if run_id not in self.logs:
            raise ci.CiError("GitHub answered 403: reading job logs needs GITHUB_TOKEN")
        return self.logs[run_id]


@pytest.fixture(autouse=True)
def _no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# -- where -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("https://github.com/HKUDS/Raven.git", "HKUDS/Raven"),
        ("https://github.com/HKUDS/Raven", "HKUDS/Raven"),
        ("https://x-access-token:abc@github.com/o/r.git", "o/r"),
        ("git@github.com:o/r.git", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
        ("https://gitlab.com/o/r.git", ""),
        ("", ""),
    ],
)
def test_the_repository_is_read_from_a_github_remote_and_nothing_else(url: str, repo: str) -> None:
    assert ci.parse_remote(url) == repo


def test_a_checkout_gives_its_origin_branch_and_head(tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q", "-b", "feat/ci"], check=True)
    subprocess.run([*git, "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True
    )

    assert ci.repo_of(tmp_path) == "o/r"
    assert ci.branch_of(tmp_path) == "feat/ci"
    assert len(ci.head_of(tmp_path)) == 40


# -- reading ---------------------------------------------------------------------------------


def test_checks_and_statuses_are_both_read_and_a_branch_with_a_slash_is_one_ref() -> None:
    gh = FakeGitHub(
        runs=[_run("tests", "success"), _run("lint", "neutral", run_id=2)],
        statuses=[{"context": "ci/circle", "state": "success", "description": "ok", "target_url": "https://c/1"}],
    )

    report = ci.read("o/r", "feat/x", fetch=gh.json, token="")

    assert [(c.name, c.source) for c in report.checks] == [
        ("tests", "checks"),
        ("lint", "checks"),
        ("ci/circle", "status"),
    ]
    assert report.state == "success" and report.sha == "a" * 40
    assert all("/commits/feat%2Fx/" in url for url, _ in gh.asked)
    assert all("Authorization" not in h for _, h in gh.asked)


@pytest.mark.parametrize(
    ("runs", "statuses", "state"),
    [
        ([], [], "none"),
        ([_run("tests", None, status="in_progress")], [], "pending"),
        ([_run("tests", "success")], [{"context": "ext", "state": "pending"}], "pending"),
        ([_run("tests", "failure"), _run("lint", None, status="queued", run_id=2)], [], "failure"),
        ([_run("tests", "success")], [{"context": "ext", "state": "error"}], "failure"),
        ([_run("tests", "timed_out")], [], "failure"),
        ([_run("tests", "skipped")], [], "success"),
    ],
)
def test_the_state_is_red_once_anything_finished_red_and_green_only_when_all_did(runs, statuses, state) -> None:
    assert ci.read("o/r", "main", fetch=FakeGitHub(runs, statuses).json, token="").state == state


def test_the_token_is_sent_to_the_api_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    gh = FakeGitHub([_run("tests", "success")])

    ci.read("o/r", "main", fetch=gh.json)

    assert all(h.get("Authorization") == "Bearer t0k" for _, h in gh.asked)


def test_a_malformed_repository_is_refused_before_anything_is_asked() -> None:
    gh = FakeGitHub()
    with pytest.raises(ci.CiError, match="owner/repo"):
        ci.read("not a repo", "main", fetch=gh.json)
    assert gh.asked == []


def test_a_pull_request_is_read_at_its_head() -> None:
    gh = FakeGitHub()
    gh.pulls[7] = {"head": {"ref": "fix-it", "sha": "b" * 40}}

    assert ci.resolve_pr("o/r", 7, fetch=gh.json, token="") == ("fix-it", "b" * 40)


# -- why it is red ---------------------------------------------------------------------------

LOG = "\n".join(
    [
        "2026-10-14T10:00:00.0000000Z ##[group]Run pytest",
        "2026-10-14T10:00:01.0000000Z collected 3 items",
        "2026-10-14T10:00:02.0000000Z tests/test_a.py::test_one PASSED",
        "2026-10-14T10:00:03.0000000Z tests/test_a.py::test_two FAILED",
        "2026-10-14T10:00:04.0000000Z E   assert 2 == 3",
        "2026-10-14T10:00:05.0000000Z ##[error]Process completed with exit code 1.",
        "2026-10-14T10:00:06.0000000Z Post job cleanup.",
    ]
)


def test_the_log_excerpt_is_the_error_lines_and_what_led_up_to_them() -> None:
    out = ci.log_excerpt(LOG, context=2)

    assert out.splitlines() == [
        "tests/test_a.py::test_two FAILED",
        "E   assert 2 == 3",
        "ERROR: Process completed with exit code 1.",
    ]


def test_a_log_without_error_lines_gives_its_tail_and_gaps_are_marked() -> None:
    plain = "\n".join(f"line {i}" for i in range(100))
    assert ci.log_excerpt(plain, max_lines=3).splitlines() == ["line 97", "line 98", "line 99"]
    two = "a\nb\n##[error]one\nc\nd\ne\nf\n##[error]two"
    assert ci.log_excerpt(two, context=1).splitlines() == ["b", "ERROR: one", "...", "f", "ERROR: two"]


def test_a_red_check_is_explained_by_its_annotations_and_its_job_log() -> None:
    gh = FakeGitHub(
        [
            _run("tests", "failure", run_id=11),
            _run("docs", "failure", run_id=12, app="readthedocs", title="Build failed"),
        ]
    )
    gh.annotations[11] = [
        {"path": "src/a.py", "start_line": 4, "annotation_level": "failure", "message": "assert 2 == 3"}
    ]
    gh.logs[11] = LOG
    report = ci.read("o/r", "main", fetch=gh.json, token="")

    ci.explain(report, fetch=gh.json, fetch_text=gh.text, token="")

    tests, docs = report.failures
    assert tests.annotations == ["src/a.py:4 [failure] assert 2 == 3"]
    assert "ERROR: Process completed with exit code 1." in tests.log
    # Not an Actions job: no log to ask for, and its own summary says why.
    assert docs.log == "" and docs.note == "Build failed"
    assert not any("/actions/jobs/12/" in url for url, _ in gh.asked)
    text = report.render()
    assert text.startswith("CI for o/r@main") and "FAILURE" in text
    assert "## tests" in text and "src/a.py:4" in text and "assert 2 == 3" in text


def test_a_log_that_cannot_be_read_is_said_rather_than_raised() -> None:
    gh = FakeGitHub([_run("tests", "failure", run_id=11)])
    report = ci.explain(ci.read("o/r", "main", fetch=gh.json, token=""), fetch=gh.json, fetch_text=gh.text, token="")

    assert "log not read" in report.failures[0].note and "GITHUB_TOKEN" in report.failures[0].note


def test_the_job_log_redirect_is_followed_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_get(url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        calls.append((url, headers))
        if url.startswith(API):
            return httpx.Response(302, headers={"location": "https://storage.example/log.txt"})
        return httpx.Response(200, text="the log")

    monkeypatch.setattr(httpx, "get", fake_get)

    assert ci._get_text(f"{API}/repos/o/r/actions/jobs/1/logs", ci.headers("t0k")) == "the log"
    assert calls[0][1]["Authorization"] == "Bearer t0k"
    assert calls[1][1] is None


def test_a_note_says_when_the_checkout_is_ahead_of_what_ci_ran_on() -> None:
    report = ci.CiReport(repo="o/r", ref="main", sha="a" * 40, checks=[ci.Check("tests", "completed", "success")])

    assert "may not be pushed yet" in report.render(local_head="b" * 40)
    assert "may not be pushed yet" not in report.render(local_head="a" * 40)


# -- waiting ---------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def _sequence(*states: list[dict[str, Any]]):
    """A fetch whose check runs move through ``states``, one per poll, staying on the last."""
    gh = FakeGitHub()
    polls = iter(states)
    last: list[list[dict[str, Any]]] = [[]]

    def fetch(url: str, headers: dict[str, str]) -> Any:
        if "/check-runs?" in url:
            last[0] = next(polls, last[0])
            gh.runs = last[0]
        return gh.json(url, headers)

    return fetch


def test_waiting_polls_until_every_check_has_finished() -> None:
    clock = _Clock()
    fetch = _sequence(
        [_run("tests", None, status="queued")],
        [_run("tests", None, status="in_progress")],
        [_run("tests", "success")],
    )

    report = ci.wait("o/r", "main", timeout_s=600, every_s=20, fetch=fetch, token="", sleep=clock.sleep, clock=clock)

    assert report.state == "success" and clock.slept == [20, 20] and report.waited_s == 40 and not report.timed_out


def test_a_red_check_waits_for_the_rest_unless_told_to_stop_at_the_first() -> None:
    first_red = [_run("tests", "failure"), _run("lint", None, status="in_progress", run_id=2)]
    all_done = [_run("tests", "failure"), _run("lint", "success", run_id=2)]

    clock = _Clock()
    report = ci.wait(
        "o/r",
        "main",
        timeout_s=600,
        every_s=10,
        fetch=_sequence(first_red, all_done),
        token="",
        sleep=clock.sleep,
        clock=clock,
    )
    assert report.state == "failure" and clock.slept == [10]

    clock = _Clock()
    report = ci.wait(
        "o/r",
        "main",
        timeout_s=600,
        every_s=10,
        fail_fast=True,
        fetch=_sequence(first_red, all_done),
        token="",
        sleep=clock.sleep,
        clock=clock,
    )
    assert report.state == "failure" and clock.slept == []


def test_no_checks_are_given_time_to_appear_before_they_are_the_answer() -> None:
    clock = _Clock()
    report = ci.wait(
        "o/r",
        "main",
        timeout_s=600,
        every_s=30,
        appear_s=60,
        fetch=_sequence([]),
        token="",
        sleep=clock.sleep,
        clock=clock,
    )

    assert report.state == "none" and report.waited_s == 60 and not report.timed_out


def test_checks_still_running_at_the_deadline_come_back_marked_timed_out() -> None:
    clock = _Clock()
    report = ci.wait(
        "o/r",
        "main",
        timeout_s=50,
        every_s=30,
        fetch=_sequence([_run("tests", None, status="in_progress")]),
        token="",
        sleep=clock.sleep,
        clock=clock,
    )

    assert report.state == "pending" and report.timed_out and clock.slept == [30, 20]
    assert "still not finished" in report.render()


def test_an_anonymous_poll_is_spaced_for_sixty_requests_an_hour() -> None:
    assert ci._poll_every("", None) == 90.0 and ci._poll_every("t", None) == 30.0 and ci._poll_every("", 5) == 5


async def test_waiting_on_the_event_loop_answers_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci, "_poll_every", lambda token, every: 0.0)
    fetch = _sequence([_run("tests", None, status="queued")], [_run("tests", "success")])

    report = await ci.wait_async("o/r", "main", timeout_s=30, fetch=fetch, token="")

    assert report.state == "success"


# -- the tool --------------------------------------------------------------------------------


def _checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True
    )
    return repo


async def test_the_tool_reads_the_checkouts_own_repository_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.tools.ci_status import CiStatusTool

    repo = _checkout(tmp_path)
    gh = FakeGitHub([_run("tests", "failure", run_id=5)])
    gh.logs[5] = LOG
    monkeypatch.setattr(ci, "_get_json", gh.json)
    monkeypatch.setattr(ci, "_get_text", gh.text)

    text = await CiStatusTool(workspace=repo, allowed_dirs=(repo,)).execute()

    assert text.startswith("CI for o/r@main") and "FAILURE" in text and "ERROR: Process completed" in text
    assert gh.asked[0][0].startswith(f"{API}/repos/o/r/commits/main/")


async def test_the_tool_reads_a_pull_request_and_refuses_what_it_cannot_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.tools.ci_status import CiStatusTool

    repo = _checkout(tmp_path)
    gh = FakeGitHub([_run("tests", "success")])
    gh.pulls[3] = {"head": {"ref": "topic", "sha": "c" * 40}}
    monkeypatch.setattr(ci, "_get_json", gh.json)
    tool = CiStatusTool(workspace=repo, allowed_dirs=(repo,))

    text = await tool.execute(pr=3)
    assert text.startswith("Pull request #3 (topic)") and "SUCCESS" in text

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert (await tool.execute(path=str(outside))).startswith("Error:")
    plain = repo / "sub"
    plain.mkdir()
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)
    assert "no GitHub 'origin' remote" in await tool.execute(path=str(plain))


async def test_a_network_failure_is_a_tool_error_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from raven.agent.tools.ci_status import CiStatusTool

    repo = _checkout(tmp_path)

    def down(url: str, headers: dict[str, str], **kw: Any) -> Any:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", down)

    text = await CiStatusTool(workspace=repo, allowed_dirs=(repo,)).execute()

    assert text.startswith("Error reading CI") and "could not be reached" in text


def test_the_tool_is_served_only_when_asked_and_runs_unasked_for() -> None:
    from raven.config.schema import ToolsConfig
    from raven.permissions.rules import DEFAULT_ALLOW_TOOLS

    assert ToolsConfig().ci_status is False
    assert ToolsConfig.model_validate({"ciStatus": True}).ci_status is True
    assert "ci_status" in DEFAULT_ALLOW_TOOLS


# -- the command line ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "code", "said"),
    [
        ([_run("tests", "success")], 0, "SUCCESS"),
        ([_run("tests", "failure")], 1, "FAILURE"),
        ([_run("tests", None, status="queued")], 2, "PENDING"),
    ],
)
def test_the_command_prints_the_report_and_exits_by_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs, code, said
) -> None:
    from raven.cli.commands import app

    repo = _checkout(tmp_path)
    gh = FakeGitHub(runs)
    monkeypatch.setattr(ci, "_get_json", gh.json)

    result = CliRunner().invoke(app, ["ci", "status", str(repo)])
    assert result.exit_code == code, result.output
    assert said in result.output

    result = CliRunner().invoke(app, ["ci", "status", str(repo), "--json", "--no-logs"])
    assert json.loads(result.output)["state"] == said.lower()


def test_the_command_says_why_it_could_not_read(tmp_path: Path) -> None:
    from raven.cli.commands import app

    result = CliRunner().invoke(app, ["ci", "status", str(tmp_path)])

    assert result.exit_code == 3 and "no GitHub 'origin' remote" in result.output


# -- the fix loop ----------------------------------------------------------------------------

PY = sys.executable.replace("\\", "/")


async def test_the_fix_loop_hands_the_red_report_to_the_fixer_and_ends_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent.manager import SubagentManager
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.scenario.catalog import discover
    from raven.scenario.runner import run_scenario

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    repo = _checkout(tmp_path)
    pushed = tmp_path / "pushed.txt"
    asked = tmp_path / "asked.txt"
    # A fixer that "pushes" by leaving a marker the fake GitHub reads, and keeps what it was asked.
    fixer = (
        f'"{PY}" -c "import sys, pathlib; t = sys.stdin.read(); '
        f"pathlib.Path({str(asked)!r}).write_text(t, encoding='utf-8'); "
        f"pathlib.Path({str(pushed)!r}).write_text('x'); print('PUSHED ' + 'b' * 40)\""
    )
    gh = FakeGitHub()
    gh.annotations[9] = [
        {"path": "src/a.py", "start_line": 4, "annotation_level": "failure", "message": "assert 2 == 3"}
    ]

    def fetch(url: str, headers: dict[str, str]) -> Any:
        green = pushed.exists()
        gh.sha = "b" * 40 if green else "a" * 40
        gh.runs = [_run("tests", "success" if green else "failure", run_id=9, app="other", sha=gh.sha)]
        return gh.json(url, headers)

    monkeypatch.setattr(ci, "_get_json", fetch)
    monkeypatch.setattr(ci, "_poll_every", lambda token, every: 0.0)
    manager = SubagentManager(
        provider=type("_NoModel", (), {"get_default_model": lambda self: "none"})(),  # type: ignore[arg-type]
        workspace=repo,
        agents=[ThirdPartyCliSubagentConfig(name="fixer", command=fixer)],
    )
    spec = discover(repo)["ci-fix"]

    outcome = await run_scenario(spec, {"fixer": "fixer", "task": "add numbers"}, manager=manager, workspace=repo)

    assert outcome.ok, outcome.error
    assert outcome.result.startswith("CI is green on o/r@main") and "after 1 fix(es)" in outcome.result
    prompt = asked.read_text(encoding="utf-8")
    assert "CI is red on branch main of o/r" in prompt and "src/a.py:4 [failure] assert 2 == 3" in prompt
    assert "What the branch is for: add numbers" in prompt and "PUSHED <commit sha>" in prompt

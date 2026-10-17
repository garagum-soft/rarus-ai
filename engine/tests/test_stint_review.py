"""Independent code review: the author cannot approve their own branch, and nothing merges without a verdict."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.playbook.executor import PlaybookExecutor
from raven.stint.git import ProjectGit
from raven.stint.integrate import integrate_branch
from raven.stint.record import StintStore
from raven.stint.review import Actor, ReviewBook, ReviewError, parse_agent_verdict, review_by_agent
from raven.stint.verify import CheckSpec
from tests import test_playbook_stint as stint_tests
from tests.test_playbook_stint import _spec
from tests.test_subagent_dag_runner import draining_dag_runs

CHECKLIST = ["tests cover the change", "no secrets in the diff"]
ALL_PASS = dict.fromkeys(CHECKLIST, "pass")
PY = sys.executable.replace("\\", "/")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(cwd: Path, name: str, text: str, message: str) -> None:
    (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _commit(project, "shared.txt", "base\n", "base")
    tree = tmp_path / "tree"
    ProjectGit(project).worktree_add(tree, "stint/x", ProjectGit(project).head())
    _commit(tree, "feature.py", "def f():\n    return 1\n", "the coder's work")
    return project, tree


@pytest.fixture
def book(tmp_path: Path) -> ReviewBook:
    return ReviewBook(tmp_path / "reviews")


def _request(book: ReviewBook, repos: tuple[Path, Path], **over):
    project, tree = repos
    kwargs = {"authors": ["agent:coder"], "checklist": CHECKLIST, **over}
    return book.request(project, tree, "stint/x", **kwargs)


def test_the_author_cannot_approve_what_it_wrote_and_someone_else_can(book, repos) -> None:
    req = _request(book, repos)

    with pytest.raises(ReviewError) as own:
        book.verdict(req.id, "agent:coder", "approve", checks=ALL_PASS)
    approved = book.verdict(req.id, "agent:reviewer", "approve", checks=ALL_PASS, comment="reads fine")

    assert own.value.code == "review_policy_denied" and "agent:coder wrote this branch" in str(own.value)
    assert approved.status == "approved" and [v["by"] for v in approved.verdicts] == ["agent:reviewer"]
    events = [(row["event"], row.get("by")) for row in book.journal()]
    assert events == [("requested", None), ("refused", "agent:coder"), ("verdict", "agent:reviewer")]
    assert req.files == ["feature.py"]


def test_policies_human_only_anyone_and_failing_closed_without_an_author(repos, tmp_path: Path) -> None:
    books = (ReviewBook(tmp_path / name) for name in ("human", "anyone", "nobody"))

    book = next(books)
    human = _request(book, repos, policy="human_only")
    with pytest.raises(ReviewError, match="only a person"):
        book.verdict(human.id, "agent:reviewer", "approve", checks=ALL_PASS)
    assert book.verdict(human.id, "user:alice", "approve", checks=ALL_PASS).status == "approved"

    book = next(books)
    anyone = _request(book, repos, policy="anyone")
    assert book.verdict(anyone.id, "agent:coder", "reject", comment="needs tests").status == "rejected"

    book = next(books)
    nobody = _request(book, repos, authors=[])
    with pytest.raises(ReviewError, match="names nobody"):
        book.verdict(nobody.id, "user:alice", "approve", checks=ALL_PASS)


def test_an_approval_answers_the_whole_checklist_and_a_rejection_says_why(book, repos) -> None:
    req = _request(book, repos)

    for checks, comment, decision, code in (
        ({CHECKLIST[0]: "pass"}, "", "approve", "checklist_incomplete"),
        ({**ALL_PASS, CHECKLIST[1]: "fail"}, "", "approve", "checklist_failed"),
        ({"made up": "pass"}, "", "approve", "check_unknown"),
        ({CHECKLIST[0]: "maybe"}, "", "approve", "check_invalid"),
        ({}, "", "reject", "reason_missing"),
        ({}, "x", "ship it", "decision_invalid"),
    ):
        with pytest.raises(ReviewError) as caught:
            book.verdict(req.id, "user:alice", decision, checks=checks, comment=comment)
        assert caught.value.code == code

    assert book.verdict(req.id, "user:alice", "reject", checks={CHECKLIST[1]: "fail"}).status == "rejected"
    with pytest.raises(ReviewError, match="not a"):
        Actor.parse("alice")


def test_a_merge_without_an_approving_verdict_is_blocked(book, repos) -> None:
    project, tree = repos
    before = _git(project, "rev-parse", "HEAD")

    unrequested = integrate_branch(project, tree, "stint/x", review=book)
    req = _request(book, repos)
    unjudged = integrate_branch(project, tree, "stint/x", review=book)
    book.verdict(req.id, "user:alice", "reject", comment="f returns the wrong value")
    rejected = integrate_branch(project, tree, "stint/x", review=book)

    for result in (unrequested, unjudged, rejected):
        assert result.status == "unreviewed", result
    assert "has no review" in unrequested.detail
    assert "no review verdict yet" in unjudged.detail
    assert "rejected" in rejected.detail and "wrong value" in rejected.detail
    assert f"rarus review show {req.id}" in rejected.escalation()
    assert _git(project, "rev-parse", "HEAD") == before and not (project / "feature.py").exists()
    # The project's branch was not even merged into the checkout: the gate stands before both steps.
    assert _git(tree, "log", "--format=%s", "-1") == "the coder's work"

    book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS)
    merged = integrate_branch(project, tree, "stint/x", review=book)

    assert merged.status == "merged" and (project / "feature.py").exists()
    assert book.get(req.id).status == "merged" and book.get(req.id).merged_commit == merged.commit
    assert [row["event"] for row in book.journal()][-1] == "merged"


def test_new_work_after_an_approval_needs_a_new_review(book, repos) -> None:
    project, tree = repos
    req = _request(book, repos)
    book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS)
    _commit(tree, "feature.py", "def f():\n    return 2\n", "slipped in after the review")

    blocked = integrate_branch(project, tree, "stint/x", review=book)
    with pytest.raises(ReviewError) as stale:
        book.verdict(req.id, "user:bob", "approve", checks=ALL_PASS)
    again = _request(book, repos)
    still = integrate_branch(project, tree, "stint/x", review=book)
    book.verdict(again.id, "user:bob", "approve", checks=ALL_PASS)

    assert blocked.status == "unreviewed" and "changed after it was put up for review" in blocked.detail
    assert stale.value.code == "stale_diff"
    assert still.status == "unreviewed" and "no review verdict yet" in still.detail
    assert integrate_branch(project, tree, "stint/x", review=book).status == "merged"


def test_taking_the_target_branch_in_keeps_the_verdict(book, repos, tmp_path: Path) -> None:
    """The merge's first step merges the project's branch into the checkout; the change itself is unchanged."""
    project, tree = repos
    req = _request(book, repos)
    book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS)
    _commit(project, "other.txt", "a person's work\n", "meanwhile on main")
    failing = CheckSpec(name="tests", command=f'"{sys.executable}" -c "raise SystemExit(1)"', timeout_sec=60)

    first = integrate_branch(project, tree, "stint/x", checks=[failing], log_dir=tmp_path / "logs", review=book)
    second = integrate_branch(project, tree, "stint/x", review=book)

    assert first.status == "checks_failed" and (tree / "other.txt").exists()
    assert second.status == "merged", second


async def test_a_reviewer_agent_is_refused_before_it_runs_when_it_wrote_the_work(book, repos) -> None:
    req = _request(book, repos)
    ran: list[str] = []

    async def runner(agent: str, task: str, workspace: Path) -> str:
        ran.append(agent)
        assert "def f()" in task and CHECKLIST[0] in task
        return 'Looks good. {"decision": "approve", "checks": {"tests cover the change": "pass", "no secrets in the diff": "pass"}, "comment": "small and safe"}'

    with pytest.raises(ReviewError):
        await review_by_agent(book, req.id, "coder", runner)
    judged = await review_by_agent(book, req.id, "reviewer", runner)

    assert ran == ["reviewer"]
    assert judged.status == "approved" and judged.verdicts[-1]["by"] == "agent:reviewer"
    assert parse_agent_verdict("no json here") is None
    assert parse_agent_verdict('{"decision": "approve|reject"}') is None


def test_a_reviewer_that_plays_a_role_is_refused_when_the_playbook_loads() -> None:
    with pytest.raises(ValueError, match="plays a role"):
        _spec(isolation="worktree", integrate="merge", review={"reviewer": "echo"})
    with pytest.raises(ValueError, match="human_only"):
        _spec(isolation="worktree", integrate="merge", review={"reviewer": "judge", "policy": "human_only"})


# -- the main path: a stint finishes, its branch is reviewed, and only then merges ---------------

JUDGE = """import sys
sys.stdin.read()
print('{"decision": "approve", "checks": {"builds": "pass"}, "comment": "independent look: fine"}')
"""


def _judge(tmp_path: Path) -> str:
    """A reviewer agent's command: reads the diff, approves it."""
    script = tmp_path / "judge.py"
    script.write_text(JUDGE, encoding="utf-8")
    return f'"{PY}" "{script.as_posix()}"'


class TestTheMainPath:
    @pytest.fixture(autouse=True)
    async def _drain(self):
        async with draining_dag_runs():
            yield

    @staticmethod
    def _executor(tmp_path: Path, announced: list[str]) -> PlaybookExecutor:
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        ProjectGit(project).ensure_repo()

        async def announce(run_id: str, text: str, origin: dict) -> None:
            announced.append(text)

        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(
                    name="echo", command=f'"{PY}" -c "import sys; sys.stdout.write(sys.stdin.read())"'
                ),
                ThirdPartyCliSubagentConfig(name="judge", command=_judge(tmp_path)),
            ],
            announce=announce,
            charge=lambda _key: None,
        )
        tool.set_context("web", "default", "web:stint")
        return PlaybookExecutor(dag_tool=tool, workspace=project)

    async def _run(self, tmp_path: Path, review: dict):
        announced: list[str] = []
        executor = self._executor(tmp_path, announced)
        spec = _spec(confirm=False, isolation="worktree", integrate="merge", review=review)
        await executor.execute(spec, {}, max_rounds=1)
        summary = await stint_tests.TestRunning._await_announce(announced)
        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        return record, summary

    async def test_a_reviewer_agent_judges_the_finished_branch_and_the_merge_follows(self, tmp_path: Path) -> None:
        record, summary = await self._run(tmp_path, {"reviewer": "judge", "checklist": ["builds"]})

        assert record.integration.get("status") == "merged", record.integration
        req = ReviewBook().get(record.integration["review"])
        assert req.authors == ["agent:echo"] and req.status == "merged"
        assert [(v["by"], v["decision"]) for v in req.verdicts] == [("agent:judge", "approve")]
        assert "now merged into" in summary

    async def test_without_a_verdict_the_stint_does_not_merge_and_a_person_finishes_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from raven.cli import review_commands
        from raven.cli.commands import app

        monkeypatch.setattr(review_commands, "_a_person_at_the_terminal", lambda: True)

        record, summary = await self._run(tmp_path, {"checklist": ["builds"]})
        project = tmp_path / "project"
        before = _git(project, "rev-parse", "HEAD")

        assert record.integration.get("status") == "unreviewed", record.integration
        rid = record.integration["review"]
        assert any(q["role"] == "integration" and rid in q["text"] for q in record.questions)
        assert "NOT merged (unreviewed)" in summary

        runner = CliRunner()
        blocked = runner.invoke(app, ["review", "merge", rid])
        own = runner.invoke(
            app, ["review", "verdict", rid, "--as", "agent:echo", "--approve", "--check", "builds=pass"]
        )
        shown = runner.invoke(app, ["review", "show", rid])
        good = runner.invoke(
            app, ["review", "verdict", rid, "--as", "user:alice", "--approve", "--check", "builds=pass"]
        )
        merged = runner.invoke(app, ["review", "merge", rid])
        logged = runner.invoke(app, ["review", "log", rid])

        assert blocked.exit_code == 1 and _git(project, "rev-parse", "HEAD") == before
        assert own.exit_code == 1 and "agent:echo wrote this branch" in own.output
        assert "authors  agent:echo" in shown.output and "check    builds" in shown.output
        assert good.exit_code == 0 and "approved by user:alice" in good.output
        assert merged.exit_code == 0, merged.output
        assert "merged " + record.branch in merged.output
        events = [line.split()[3] for line in logged.output.splitlines()]
        assert events == ["requested", "refused", "verdict", "merged"], logged.output


def test_the_command_line_has_a_roster_agent_review_and_refuses_the_author(tmp_path: Path, repos, monkeypatch) -> None:
    from raven.cli.commands import app

    home = tmp_path / "home"
    home.mkdir()
    agents = [
        {"name": "judge", "kind": "cli", "command": _judge(tmp_path)},
        {"name": "coder", "kind": "cli", "command": f'"{PY}" -c "print(1)"'},
    ]
    (home / "config.json").write_text(json.dumps({"subagents": {"agents": agents}}), encoding="utf-8")
    monkeypatch.setenv("RARUS_HOME", str(home))
    project, tree = repos
    runner = CliRunner()

    asked = runner.invoke(
        app,
        [
            "review",
            "request",
            "stint/x",
            "-p",
            str(project),
            "--workdir",
            str(tree),
            "--author",
            "agent:coder",
            "--check",
            "builds",
        ],
    )
    rid = asked.output.split(":")[0]
    own = runner.invoke(app, ["review", "run", rid, "--agent", "coder"])
    judged = runner.invoke(app, ["review", "run", rid, "--agent", "judge"])
    listed = runner.invoke(app, ["review", "list"])
    merged = runner.invoke(app, ["review", "merge", rid])

    assert asked.exit_code == 0, asked.output
    assert own.exit_code == 1 and "agent:coder wrote this branch" in own.output
    assert judged.exit_code == 0 and "approve by agent:judge" in judged.output, judged.output
    assert rid in listed.output and "approved" in listed.output
    assert merged.exit_code == 0 and (project / "feature.py").exists()


# -- the review cannot be talked out of its terms ---------------------------------------------------


def test_asking_again_never_relaxes_the_terms(book, repos) -> None:
    """The gate's own advice is to request again; that must not turn human_only into not_creator."""
    first = _request(book, repos, policy="human_only", verify=[{"name": "tests", "run": "pytest"}])
    again = _request(book, repos, policy="anyone", authors=["agent:dev"], checklist=[])

    assert first.id == again.id
    assert again.policy == "human_only"
    assert again.authors == ["agent:coder", "agent:dev"]
    assert again.checklist == CHECKLIST and again.verify == [{"name": "tests", "run": "pytest"}]


def test_an_approval_given_under_other_terms_does_not_open_the_merge(book, repos) -> None:
    project, tree = repos
    req = _request(book, repos)
    book.verdict(req.id, "agent:reviewer", "approve", checks=ALL_PASS)
    assert book.gate(project, "stint/x")[0]

    widened = _request(book, repos, checklist=["the migration is reversible"])
    assert widened.status == "pending"
    unanswered = book.gate(project, "stint/x")
    assert not unanswered[0] and "the migration is reversible" in unanswered[1]

    _request(book, repos, authors=["agent:reviewer"])
    authored = book.gate(project, "stint/x")
    assert not authored[0] and "agent:reviewer wrote this branch" in authored[1]
    assert integrate_branch(project, tree, "stint/x", review=book).status == "unreviewed"


def test_human_only_refuses_the_person_who_wrote_it(book, repos) -> None:
    req = _request(book, repos, policy="human_only", authors=["user:dev"])

    with pytest.raises(ReviewError, match="user:dev wrote this branch"):
        book.verdict(req.id, "user:dev", "approve", checks=ALL_PASS)
    assert book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS).status == "approved"


def test_a_person_s_verdict_is_refused_without_a_terminal(tmp_path: Path, repos, monkeypatch) -> None:
    """An agent's shell has no terminal on its input, so `--as user:...` from it is not a person."""
    from raven.cli import review_commands
    from raven.cli.commands import app

    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "home"))
    project, tree = repos
    rid = ReviewBook().request(project, tree, "stint/x", authors=["agent:coder"], checklist=["builds"]).id
    runner = CliRunner()

    posed = runner.invoke(app, ["review", "verdict", rid, "--as", "user:dev", "--approve", "--check", "builds=pass"])

    assert posed.exit_code == 1 and "interactive terminal" in posed.output
    assert ReviewBook().get(rid).verdicts == []

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(review_commands.sys, "stdin", _Tty())
    assert review_commands._a_person_at_the_terminal()
    monkeypatch.setenv("RAVEN_MAIL_AS", "coder")
    assert not review_commands._a_person_at_the_terminal()


async def test_an_agent_cannot_approve_a_diff_it_would_be_shown_cut(book, repos) -> None:
    project, tree = repos
    _commit(tree, "notes.md", "x" * 61_000 + "\n", "a long file before the change that matters")
    req = _request(book, repos)
    ran: list[str] = []

    async def runner(agent: str, task: str, workspace: Path) -> str:
        ran.append(agent)
        return '{"decision": "approve", "checks": {}, "comment": "fine"}'

    with pytest.raises(ReviewError) as caught:
        await review_by_agent(book, req.id, "reviewer", runner)

    assert caught.value.code == "diff_too_large" and ran == []
    assert book.get(req.id).verdicts == []


def test_the_merge_goes_only_into_the_branch_that_was_reviewed(book, repos) -> None:
    project, tree = repos
    req = _request(book, repos)
    book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS)

    _git(project, "checkout", "-q", "-b", "release")
    elsewhere = integrate_branch(project, tree, "stint/x", review=book)
    _git(project, "checkout", "-q", "--detach")
    detached = integrate_branch(project, tree, "stint/x", review=book)

    assert elsewhere.status == "unreviewed" and "reviewed for a merge into main" in elsewhere.detail
    assert detached.status == "refused" and "detached HEAD" in detached.detail
    assert not (project / "feature.py").exists()
    _git(project, "checkout", "-q", "main")
    assert integrate_branch(project, tree, "stint/x", review=book).status == "merged"


def test_a_commit_made_while_the_checks_run_does_not_merge(book, repos, tmp_path: Path) -> None:
    project, tree = repos
    req = _request(book, repos)
    book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS)
    late = CheckSpec(
        name="tests",
        command='git -c user.name=t -c user.email=t@t commit -q --allow-empty -m "rode in on the checks"',
        timeout_sec=60,
    )
    before = _git(project, "rev-parse", "HEAD")

    result = integrate_branch(project, tree, "stint/x", checks=[late], log_dir=tmp_path / "logs", review=book)

    assert result.status == "refused" and "moved while its checks ran" in result.detail
    assert _git(project, "rev-parse", "HEAD") == before


def test_the_checks_before_a_merge_do_not_see_the_host_s_keys(book, repos, tmp_path: Path, monkeypatch) -> None:
    project, tree = repos
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET-123")
    req = _request(book, repos)
    book.verdict(req.id, "user:alice", "approve", checks=ALL_PASS)
    probe = CheckSpec(
        name="env",
        command=f"\"{PY}\" -c \"import os; print(os.environ.get('OPENAI_API_KEY', 'absent'))\"",
        timeout_sec=60,
    )

    result = integrate_branch(project, tree, "stint/x", checks=[probe], log_dir=tmp_path / "logs", review=book)

    assert result.status == "merged", result
    seen = (tmp_path / "logs" / "env.out").read_text(encoding="utf-8")
    assert "absent" in seen and "sk-SECRET" not in seen

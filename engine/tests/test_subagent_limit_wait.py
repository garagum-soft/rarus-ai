"""A spawned agent that stops on its model provider's usage limit.

CLI and ACP agents (Claude Code, Codex CLI, Gemini CLI...) report the limit in
their own words, not as response headers: read from those words, a reset near
enough (``agents.defaults.rateLimitWaitMaxS``) is waited out and the task asked
again; a later one fails the run saying when it lifts. Off by default.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.agent.subagent import manager as manager_module
from raven.agent.subagent.manager import SubagentManager, limit_note, limit_retry_task
from raven.providers.rate_limits import limit_reset, parse_duration

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """The instance registry a spawn writes lands under this test's home, not the real ~/.rarus."""
    home = tmp_path / "raven-home"
    home.mkdir()
    monkeypatch.setenv("RARUS_HOME", str(home))


# -- reading the agents' words -----------------------------------------------------------------


def test_days_count_in_a_duration() -> None:
    assert parse_duration("2 days 3 hours 4 minutes") == pytest.approx(2 * 86400 + 3 * 3600 + 4 * 60)
    assert parse_duration("1d") == pytest.approx(86400)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        (f"CLI agent 'claude' exited 1: Claude AI usage limit reached|{int(NOW + 5400)}", 5400),
        ("CLI agent 'codex' exited 1: You've hit your usage limit. Try again in 2 days 3 hours.", 2 * 86400 + 3 * 3600),
        ("Error: [API Error: 429 Too Many Requests] Please retry in 45s", 45),
        ("Quota exceeded for quota metric 'Requests'. Your quota will reset after 1h2m3s.", 3723),
    ],
)
def test_a_limit_and_its_reset_read_from_the_agents_words(text: str, seconds: float) -> None:
    assert limit_reset(text, now=NOW) == pytest.approx(seconds)


def test_the_codex_clock_today_and_on_a_date() -> None:
    local = datetime.fromtimestamp(NOW).astimezone()
    later = local + timedelta(hours=3)
    clock = f"{(later.hour % 12) or 12}:{later.minute:02d} {'PM' if later.hour >= 12 else 'AM'}"
    today = limit_reset(f"You've hit your usage limit. Try again at {clock}.", now=NOW)
    month = later.strftime("%b")
    dated = limit_reset(
        f"You've hit your usage limit. Upgrade to Pro or try again at {month} {later.day}th, {later.year} {clock}.",
        now=NOW,
    )

    assert today is not None and 2 * 3600 < today <= 3 * 3600
    assert dated is not None and 2 * 3600 < dated <= 3 * 3600


def test_a_retry_that_is_not_about_a_limit_is_not_one() -> None:
    # A dropped connection's "retrying in 5s" is not a limit to wait out.
    assert limit_reset("connection reset by peer; retrying in 5s", now=NOW) is None
    # A limit that says nothing of when it lifts has no reset to wait for.
    assert limit_reset("You've hit your usage limit.", now=NOW) is None


def test_the_note_says_when_it_lifts_and_nothing_for_another_failure() -> None:
    note = limit_note(f"Claude AI usage limit reached|{int(NOW + 7260)}", now=NOW)

    assert note.startswith(" Its model provider's usage limit lifts at ") and "(in 2h 1m)" in note
    assert limit_note("CLI agent 'x' exited 2: bad flag", now=NOW) == ""


def test_the_task_asked_again_says_part_may_be_done() -> None:
    text = limit_retry_task("write the report", time.time())

    assert text.startswith("[raven] Your previous attempt at this task stopped at ")
    assert text.endswith("\n\nwrite the report")


# -- the manager -------------------------------------------------------------------------------


class _LimitedAgent:
    """A CLI-like agent that fails ``limits`` times on a usage limit lifting in ``reset`` seconds."""

    kind = "cli"
    is_stateful = False

    def __init__(self, limits: int, reset: float, message: str = "Claude AI usage limit reached|{epoch}") -> None:
        self.limits, self.reset, self.message = limits, reset, message
        self.tasks: list[str] = []

    async def run(self, task: str, **_kwargs) -> str:
        self.tasks.append(task)
        if len(self.tasks) <= self.limits:
            said = self.message.format(epoch=int(time.time() + self.reset))
            raise RuntimeError(f"CLI agent 'cc' exited 1: {said}")
        return "done"


def _manager(tmp_path: Path, agent: _LimitedAgent, monkeypatch, **kw) -> tuple[SubagentManager, list[dict]]:
    from raven.providers.base import LLMProvider

    class _Idle(LLMProvider):
        async def chat(self, *a, **k):  # pragma: no cover - the agent under test never calls it
            raise AssertionError

        def get_default_model(self) -> str:
            return "m"

    mgr = SubagentManager(provider=_Idle(api_key="k"), workspace=tmp_path, **kw)
    monkeypatch.setattr(mgr, "_resolve_backend", lambda _agent: agent)
    monkeypatch.setattr(manager_module, "_LIMIT_MARGIN_S", 0.05)
    monkeypatch.setattr(manager_module, "_LIMIT_PULSE_S", 0.05)
    announced: list[dict] = []

    async def capture(task_id, label, task, result, origin, status, **_) -> None:
        announced.append({"result": result, "status": status})

    monkeypatch.setattr(mgr, "_announce_result", capture)
    return mgr, announced


async def _run(mgr: SubagentManager) -> None:
    await mgr._run_subagent_inner(
        "task-a",
        "write the report",
        "report",
        {"channel": "tui", "chat_id": "default", "session_key": "tui:s", "agent": "cc"},
        None,
        mgr.provider,
        mgr.model,
    )


async def test_by_default_the_run_fails_and_says_when_the_limit_lifts(tmp_path, monkeypatch) -> None:
    agent = _LimitedAgent(limits=1, reset=0.2)
    mgr, announced = _manager(tmp_path, agent, monkeypatch)

    await _run(mgr)

    (only,) = announced
    assert only["status"] == "error" and len(agent.tasks) == 1
    assert "usage limit reached" in only["result"] and "Its model provider's usage limit lifts at " in only["result"]


@pytest.mark.production_timing
async def test_a_near_reset_is_waited_out_and_the_task_asked_again(tmp_path, monkeypatch) -> None:
    # The Claude CLI says the reset as whole epoch seconds, so 2 s ahead is at least 1.
    agent = _LimitedAgent(limits=1, reset=2.0)
    mgr, announced = _manager(tmp_path, agent, monkeypatch, rate_limit_wait_max_s=5)
    started = time.monotonic()

    await _run(mgr)

    assert announced == [{"result": "done", "status": "ok"}]
    assert time.monotonic() - started >= 0.9, "the stated reset was waited out"
    first, second = agent.tasks
    assert first == "write the report"
    assert second.startswith("[raven] Your previous attempt") and second.endswith("write the report")


@pytest.mark.production_timing
async def test_the_codex_wording_is_waited_out_too(tmp_path, monkeypatch) -> None:
    agent = _LimitedAgent(limits=1, reset=0, message="You've hit your usage limit. Try again in 1 seconds.")
    mgr, announced = _manager(tmp_path, agent, monkeypatch, rate_limit_wait_max_s=5)

    await _run(mgr)

    assert announced[0]["status"] == "ok" and len(agent.tasks) == 2


async def test_a_reset_past_the_cap_fails_at_once(tmp_path, monkeypatch) -> None:
    agent = _LimitedAgent(limits=9, reset=3600)
    mgr, announced = _manager(tmp_path, agent, monkeypatch, rate_limit_wait_max_s=5)
    started = time.monotonic()

    await _run(mgr)

    assert announced[0]["status"] == "error" and len(agent.tasks) == 1
    assert "(in 1h 0m)" in announced[0]["result"] or "(in 59m)" in announced[0]["result"]
    assert time.monotonic() - started < 2


@pytest.mark.production_timing
async def test_at_most_three_waits_in_one_run(tmp_path, monkeypatch) -> None:
    agent = _LimitedAgent(limits=9, reset=0)
    mgr, announced = _manager(tmp_path, agent, monkeypatch, rate_limit_wait_max_s=5)

    await _run(mgr)

    assert announced[0]["status"] == "error"
    assert len(agent.tasks) == 4, "the first try and three after waits"


async def test_another_failure_is_not_waited_on(tmp_path, monkeypatch) -> None:
    agent = _LimitedAgent(limits=1, reset=0, message="connection refused; retrying in 1s")
    mgr, announced = _manager(tmp_path, agent, monkeypatch, rate_limit_wait_max_s=5)

    await _run(mgr)

    assert announced[0]["status"] == "error" and len(agent.tasks) == 1
    assert "lifts at" not in announced[0]["result"]


def test_the_loop_hands_the_manager_its_wait_cap(tmp_path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring, TurnPolicy
    from raven.agent.loop.recovery import RecoveryLimits
    from raven.providers.base import LLMProvider

    class _Idle(LLMProvider):
        async def chat(self, *a, **k):  # pragma: no cover
            raise AssertionError

        def get_default_model(self) -> str:
            return "m"

    default = AgentLoop(provider=_Idle(api_key="k"), workspace=tmp_path, model="m", engine=EngineWiring())
    waiting = AgentLoop(
        provider=_Idle(api_key="k"),
        workspace=tmp_path,
        model="m",
        engine=EngineWiring(),
        policy=TurnPolicy(empty_recovery=RecoveryLimits(rate_limit_wait_max_s=18000)),
    )

    assert default.subagents.rate_limit_wait_max_s == 0
    assert waiting.subagents.rate_limit_wait_max_s == 18000

"""The operator's attention queue: what waits on a person, ordered by priority, kept across restarts."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from raven.proactive_engine.attention import AttentionQueue

T0 = 1_000_000.0


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "attention.json"


def test_three_problems_of_different_priorities_come_out_most_pressing_first_and_survive_a_restart(path: Path) -> None:
    q = AttentionQueue(path)
    q.raise_item("q1", kind="question", priority="normal", title="Which branch?", now=T0)
    q.raise_item("hg", kind="host_guard", priority="critical", title="Disk full", now=T0 + 1)
    q.raise_item("a1", kind="approval", priority="high", title="Approve: rm -rf build", now=T0 + 2)

    assert [i.id for i in q.items(now=T0 + 3)] == ["hg", "a1", "q1"]

    restarted = AttentionQueue(path)  # a new process reading the same file
    assert [i.id for i in restarted.items(now=T0 + 3)] == ["hg", "a1", "q1"]
    assert restarted.summary(now=T0 + 3) == {"critical": 1, "high": 1, "normal": 1, "low": 0}


def test_equal_priorities_come_oldest_first_and_waiting_raises_one_level(path: Path) -> None:
    q = AttentionQueue(path)
    q.raise_item("old", kind="question", priority="normal", title="old", escalate_after_s=300, now=T0)
    q.raise_item("new", kind="question", priority="normal", title="new", escalate_after_s=300, now=T0 + 10)
    q.raise_item("hi", kind="approval", priority="high", title="hi", now=T0 + 20)

    assert [i.id for i in q.items(now=T0 + 30)] == ["hi", "old", "new"]
    later = q.items(now=T0 + 305)  # "old" has waited past five minutes; "new" not yet
    assert [(i.id, i.effective_priority(T0 + 305)) for i in later] == [
        ("old", "high"),
        ("hi", "high"),
        ("new", "normal"),
    ]


def test_raising_the_same_problem_again_updates_it_rather_than_adding_one(path: Path) -> None:
    q = AttentionQueue(path)
    q.raise_item("x", kind="k", priority="low", title="first", now=T0)
    q.raise_item("x", kind="k", priority="high", title="second", now=T0 + 5)
    q.raise_item("x", kind="k", priority="normal", title="third", now=T0 + 6)

    [item] = q.items(now=T0 + 7)
    assert (item.title, item.priority, item.created) == ("third", "high", T0), "never lowered, first raise kept"


def test_a_resolved_item_leaves_the_queue_but_stays_on_record(path: Path) -> None:
    q = AttentionQueue(path)
    q.raise_item("x", kind="k", title="t", now=T0)

    assert q.resolve("x", "answered", now=T0 + 1) is True
    assert q.items(now=T0 + 2) == [] and q.resolve("x", now=T0 + 3) is False
    [kept] = q.items(include_resolved=True, now=T0 + 2)
    assert (kept.status, kept.resolution) == ("resolved", "answered")


def test_an_item_whose_owning_process_died_is_closed_as_orphaned(path: Path) -> None:
    alive = {os.getpid(): True}
    q = AttentionQueue(path, pid_alive=lambda pid: alive.get(pid, False))
    q.raise_item("mine", kind="approval", title="t", owned=True, now=T0)
    q.raise_item("free", kind="host_guard", title="t", now=T0)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[0]["owner_pid"] = 999_999_999  # raised by a process that is gone
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert q.reap(now=T0 + 1) == ["mine"]
    assert [i.id for i in q.items(now=T0 + 2)] == ["free"], "an item owned by no process is never reaped"


def test_a_bad_priority_is_refused(path: Path) -> None:
    with pytest.raises(ValueError):
        AttentionQueue(path).raise_item("x", kind="k", priority="urgent", title="t")


# -- the sources --------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _open(home: Path) -> list[str]:
    return [i.id.split(":")[0] for i in AttentionQueue(home / "attention.json").items()]


async def test_an_approval_waits_in_the_queue_until_it_is_answered(home: Path) -> None:
    from raven.rpc.approval_broker import ApprovalBroker

    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    waiting = asyncio.create_task(
        broker.await_approval(
            conversation_id="s1", turn_id="t", tool_call_id="c", command="rm build", description="Delete files"
        )
    )
    while not frames:
        await asyncio.sleep(0)

    [item] = AttentionQueue(home / "attention.json").items()
    assert (item.kind, item.priority, item.session_key, item.title) == ("approval", "high", "s1", "Approve: rm build")
    broker.resolve(frames[0]["params"]["approval_id"], "allow", conversation_id="s1")
    await waiting
    assert _open(home) == []


async def test_a_question_and_a_credential_wait_in_the_queue_too(home: Path) -> None:
    from raven.contracts.asking import CredentialRequest
    from raven.rpc.credential_broker import CredentialBroker
    from raven.rpc.question_broker import QuestionBroker

    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    questions = QuestionBroker(send_frame=send)
    asked = asyncio.create_task(questions.await_question("s2", prompt="Which branch?", choices=["main", "dev"]))
    credentials = CredentialBroker(send, sinks={"env": lambda target, value: asyncio.sleep(0)})
    wanted = asyncio.create_task(
        credentials.request_credential(
            conversation_id="s3", turn_id="t", request=CredentialRequest(target="env:KEY", label="API key")
        )
    )
    while len(frames) < 2:
        await asyncio.sleep(0)

    assert sorted(_open(home)) == ["credential", "question"]
    questions.reply("s2", "main")
    credential_request = next(f for f in frames if f["method"] == "credential.request")
    credentials.skip(credential_request["params"]["request_id"], "s3")
    assert await asked == "main"
    await wanted
    assert _open(home) == []


async def test_the_host_guard_raises_a_critical_item_and_leaves_a_paused_one_on_recovery(home: Path) -> None:
    from raven.proactive_engine.host_guard import HostGuard, HostSample, Thresholds

    def sample(free: float) -> HostSample:
        return HostSample(disk_free_ratio=free, disk_free_gb=100.0, memory_used_ratio=None, cpu_used_ratio=None)

    samples = [sample(0.01), sample(0.5)]
    guard = HostGuard(
        thresholds=Thresholds(min_disk_free_ratio=0.05),
        notify=lambda text: None,
        stop_agents=lambda reason: None,
        sampler=lambda: samples.pop(0),
    )

    await guard.check()
    [breach] = AttentionQueue(home / "attention.json").items()
    await guard.check()
    [paused] = AttentionQueue(home / "attention.json").items()

    assert (breach.id, breach.priority) == ("host_guard", "critical")
    assert (paused.id, paused.priority) == ("host_guard:paused", "high")


def test_a_stint_question_is_queued_until_it_is_answered(home: Path, tmp_path: Path) -> None:
    from raven.stint import decisions

    project = tmp_path / "proj"
    project.mkdir()
    question = decisions.ask(project, "Use Postgres or SQLite?", blocks=[3])

    provisional = decisions.ask(project, "Ship on Friday?", provisional="yes, unless told otherwise")

    items = AttentionQueue(home / "attention.json").items()
    assert [(i.kind, i.priority) for i in items] == [("stint_question", "high"), ("stint_question", "low")]
    assert question.id in items[0].title and len(items[0].id) < 30
    decisions.answer_now(project, question.id, "SQLite")
    decisions.answer_now(project, provisional.id, "yes")
    assert _open(home) == []


async def test_the_rpc_lists_and_dismisses(home: Path) -> None:
    from raven.rpc.methods.attention import attention_list, attention_resolve

    q = AttentionQueue(home / "attention.json")
    q.raise_item("a", kind="k", priority="low", title="later")
    q.raise_item("b", kind="k", priority="critical", title="now")

    listed = await attention_list({})
    dismissed = await attention_resolve({"id": "b"})
    after = await attention_list({})

    assert [i["id"] for i in listed["items"]] == ["b", "a"] and listed["summary"]["critical"] == 1
    assert dismissed == {"resolved": True} and [i["id"] for i in after["items"]] == ["a"]

"""Agent mail: sub-agents of one conversation write to each other by name, and the log is the history."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.agent.subagent import mailbox
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.mailbox import Mailbox, MailError
from raven.agent.subagent.manager import SubagentManager
from raven.providers.base import LLMResponse, ToolCallRequest

SESSION = "tui:mail-test"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return home


def _box() -> Mailbox:
    box = Mailbox(SESSION)
    box.join_many([("alice", "coder", "alice"), ("bob", "reviewer", "bob"), ("tester-1", "tester", "tester-1")])
    return box


def test_a_message_is_addressed_by_name_kept_in_history_and_read_once() -> None:
    box = _box()

    sent = box.send("alice", "bob", "the parser is in src/parse.py", kind="question")
    first, again = box.take("bob"), box.take("bob")

    assert (sent.id, sent.sender, sent.to) == ("m1", "alice", "bob")
    assert [m.body for m in first] == ["the parser is in src/parse.py"] and again == []
    # Reading consumed the inbox, not the history.
    assert [m.id for m in box.history()] == ["m1"]
    assert [m.id for m in box.history(member="alice", peer="bob")] == ["m1"]
    assert box.history(member="tester-1") == []
    # A fresh handle on the same conversation reads the same files.
    assert [m.to for m in Mailbox(SESSION).history()] == ["bob"]


def test_names_resolve_exactly_then_without_case_then_by_agent() -> None:
    box = _box()

    assert box.resolve("bob", sender="alice") == "bob"
    assert box.resolve("BOB", sender="alice") == "bob"
    assert box.resolve("tester", sender="alice") == "tester-1"  # the one participant running `tester`
    box.join("tester-2", "tester", "tester-2")
    with pytest.raises(MailError) as ambiguous:
        box.resolve("tester", sender="alice")
    with pytest.raises(MailError) as unknown:
        box.resolve("carol", sender="alice")
    with pytest.raises(MailError) as myself:
        box.resolve("alice", sender="alice")

    assert ambiguous.value.code == "recipient_ambiguous" and "tester-1, tester-2" in str(ambiguous.value)
    assert unknown.value.code == "recipient_unknown" and "bob" in str(unknown.value)
    assert myself.value.code == "self_send"


def test_refused_messages_leave_nothing_behind() -> None:
    box = _box()

    for args, kwargs, code in (
        (("alice", "bob", ""), {}, "body_missing"),
        (("alice", "bob", "x" * (mailbox.MAX_BODY_CHARS + 1)), {}, "body_too_long"),
        (("alice", "bob", "ok"), {"kind": "shout"}, "kind_invalid"),
        (("alice", "bob", "ok"), {"kind": "reply", "reply_to": "m9"}, "reply_to_unknown"),
        (("alice", "nobody", "ok"), {}, "recipient_unknown"),
    ):
        with pytest.raises(MailError) as caught:
            box.send(*args, **kwargs)
        assert caught.value.code == code

    assert box.history() == [] and box.send("alice", "bob", "x" * mailbox.MAX_BODY_CHARS).id == "m1"


def test_the_read_cursor_never_skips_an_older_message() -> None:
    box = _box()
    box.send("alice", "bob", "one")
    box.send("tester-1", "bob", "two")

    box.mark_read("bob", 1)

    assert [m.body for m in box.unread("bob")] == ["two"]


async def test_a_live_turn_that_takes_steering_gets_the_mail_at_once() -> None:
    from raven.agent.subagent import activity

    box = _box()
    box.send("alice", "bob", "older one")
    box.send("tester-1", "bob", "newer one")
    pushed: list[str] = []

    async def steer(text: str) -> str:
        pushed.append(text)
        return "injected"

    with activity.collecting(instance=(SESSION, "reviewer", "bob")) as run:
        activity.offer_steer(run, steer)
        delivered = await mailbox.deliver_live(box, "bob")

    assert delivered and "older one" in pushed[0] and "newer one" in pushed[0]
    assert box.unread("bob") == []


# -- the main path: two built-in sub-agents of one conversation ---------------------------


class _Scripted:
    """A model that answers from a script; each step may wait for something first."""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = steps
        self.seen: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "scripted"

    async def chat_with_retry(self, *, messages, tools, model, **_: Any) -> LLMResponse:
        self.seen.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        if callable(step):
            step = await step(messages, tools)
        return step


def _call(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=f"call-{name}-{len(arguments)}", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _answer(text: str) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop")


def _text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content")) for m in messages)


async def test_subagent_a_writes_to_subagent_b_by_name_and_b_gets_it_and_answers(tmp_path: Path, monkeypatch) -> None:
    """A writes to B by name while B runs; B gets it at its next step, answers, and both are in the history."""
    manager = SubagentManager(provider=_Scripted([]), workspace=tmp_path / "ws")
    (tmp_path / "ws").mkdir()

    async def _no_announce(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_announce_result", _no_announce)
    b_started, a_sent = asyncio.Event(), asyncio.Event()

    async def b_first(messages, tools) -> LLMResponse:
        assert {"send_message", "read_messages"} <= {t["function"]["name"] for t in tools}
        b_started.set()
        await a_sent.wait()
        return _call("list_dir", path=".")

    async def b_second(messages, tools) -> LLMResponse:
        assert "hello bob, the schema is in docs/schema.md" in _text(messages)
        return _call("send_message", to="alice", message="thanks, reading it", kind="reply", reply_to="m1")

    async def a_first(messages, tools) -> LLMResponse:
        await b_started.wait()
        return _call("send_message", to="bob", message="hello bob, the schema is in docs/schema.md", kind="question")

    async def a_second(messages, tools) -> LLMResponse:
        assert "Sent m1 to bob" in _text(messages), messages[-1]["content"]
        a_sent.set()
        return _answer("told bob")

    b_model = _Scripted([b_first, b_second, _answer("done reading")])
    a_model = _Scripted([a_first, a_second])

    def origin(name: str) -> dict[str, Any]:
        return {
            "channel": "tui",
            "chat_id": "default",
            "session_key": SESSION,
            "agent": GENERIC_AGENT,
            "instance": name,
            "handle": name,
        }

    await asyncio.wait_for(
        asyncio.gather(
            manager._run_subagent_inner("tb", "review the schema", "review", origin("bob"), None, b_model, "m"),
            manager._run_subagent_inner("ta", "brief bob", "brief", origin("alice"), None, a_model, "m"),
        ),
        timeout=30,
    )

    box = Mailbox(SESSION)
    history = box.history()
    assert [(m.id, m.sender, m.to, m.kind) for m in history] == [
        ("m1", "alice", "bob", "question"),
        ("m2", "bob", "alice", "reply"),
    ]
    assert history[1].reply_to == "m1"
    # B got it in its transcript, as other agents' words (fenced).
    assert "m1 from alice, expects a reply" in _text(b_model.seen[1])
    assert "agent_mail" in _text(b_model.seen[1])
    # A finished before the reply came: it waits in A's inbox, and the history is readable from there.
    assert [m.body for m in box.unread("alice")] == ["thanks, reading it"]
    assert set(box.members()) == {"alice", "bob"}


async def test_read_messages_reads_the_inbox_and_the_history_with_one_peer(tmp_path: Path) -> None:
    from raven.agent.subagent.mail_tools import ReadMessagesTool, SendMessageTool

    box = _box()
    box.send("alice", "bob", "first")
    send, read = SendMessageTool(), ReadMessagesTool()

    assert (await read.execute()).startswith("Error: agent mail needs a run")
    with mailbox.speaking_as(SESSION, "reviewer", "bob", named=True):
        inbox = await read.execute()
        empty = await read.execute()
        replied = await send.execute(to="alice", message="second", kind="reply", reply_to="m1")
        refused = await send.execute(to="bob", message="talking to myself")
        thread = await read.execute(history=True, with_agent="alice")

    assert "first" in inbox and empty == "No unread messages."
    assert replied == "Sent m2 to alice (waiting in its inbox)."
    assert refused.startswith("Error: That is you")
    assert "m1" in thread and "from alice" in thread and "m2" in thread and "to alice" in thread


# -- an external CLI agent: identity in its environment, mail in its task, `raven mail` to answer --

CAROL = r"""
import os, subprocess, sys
task = sys.stdin.read()
print("as=" + os.environ.get("RAVEN_MAIL_AS", "?"))
print("got=" + str("ping carol" in task))
out = subprocess.run(
    [sys.executable, "-m", "raven", "mail", "send", "alice", "pong from carol", "--kind", "reply", "--reply-to", "m1"],
    capture_output=True, text=True,
)
print("send=" + (out.stdout.strip() or out.stderr.strip()[-300:]))
"""


async def test_a_cli_agent_gets_its_mail_with_the_task_and_answers_through_raven_mail(tmp_path: Path) -> None:
    from raven.agent.subagent.backends.cli_agent import CliAgentBackend

    script = tmp_path / "carol.py"
    script.write_text(CAROL, encoding="utf-8")
    py = sys.executable.replace("\\", "/")
    backend = CliAgentBackend(name="carol", command=f'"{py}" "{script.as_posix()}"', timeout=120)
    box = Mailbox(SESSION)
    box.join_many([("alice", "coder", "alice"), ("carol", "carol", "carol")])
    box.send("alice", "carol", "ping carol")

    with mailbox.speaking_as(SESSION, "carol", "carol", named=True):
        reply = await backend.run("do your part", task_id="t1", workspace=tmp_path, executor=None)

    assert "as=carol" in reply and "got=True" in reply, reply
    assert "send=sent m2 to alice" in reply, reply
    assert [(m.sender, m.to, m.reply_to) for m in box.history()] == [("alice", "carol", None), ("carol", "alice", "m1")]
    assert box.unread("carol") == []


def test_the_command_line_sends_reads_and_shows_the_history() -> None:
    from raven.cli.commands import app

    _box()
    runner = CliRunner()

    sent = runner.invoke(app, ["mail", "send", "bob", "status?", "--session", SESSION, "--as", "alice"])
    peeked = runner.invoke(app, ["mail", "inbox", "--session", SESSION, "--as", "bob", "--peek"])
    read = runner.invoke(app, ["mail", "inbox", "--session", SESSION, "--as", "bob", "--json"])
    after = runner.invoke(app, ["mail", "inbox", "--session", SESSION, "--as", "bob"])
    unknown = runner.invoke(app, ["mail", "send", "zed", "hi", "--session", SESSION, "--as", "alice"])
    history = runner.invoke(app, ["mail", "history", "--session", SESSION])
    members = runner.invoke(app, ["mail", "members", "--session", SESSION])
    sessions = runner.invoke(app, ["mail", "sessions"])

    assert sent.exit_code == 0 and "sent m1 to bob" in sent.output
    assert "status?" in peeked.output
    assert json.loads(read.output)[0]["body"] == "status?"
    assert "no unread messages" in after.output
    assert unknown.exit_code == 1 and "No participant" in unknown.output
    assert "m1  alice -> bob: status?" in history.output
    assert "bob" in members.output and "unread=0" in members.output
    assert SESSION in sessions.output and "sent=1" in sessions.output

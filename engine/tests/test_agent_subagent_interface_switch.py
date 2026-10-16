"""Switching an instance between Raven's chat and the agent's own terminal, keeping its conversation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.interface_switch import (
    InterfaceSwitch,
    SwitchError,
    SwitchHooks,
    TransitionLog,
    interface_mode,
    local_hooks,
    resume_template,
)
from raven.ops.pane_board import PaneBoard

KEY = ("cli:1", "claude", "fixer")


class Fake:
    """The two controllers, as the switch sees them."""

    def __init__(self) -> None:
        self.native_id: str | None = "conv-42"
        self.running = False
        self.idle_after = 0
        self.events: list[str] = []
        self.panes: dict[str, str] = {}

    def hooks(self) -> SwitchHooks:
        async def native(key):
            return self.native_id

        async def busy(key):
            return self.running

        async def cancel(key):
            self.events.append("cancel")
            self.running = False

        async def release(key):
            self.events.append("release")

        def open_pane(key, command):
            self.events.append(f"open {command}")
            self.panes["%7"] = command
            return "%7"

        def pane_idle(pane):
            self.idle_after -= 1
            return self.idle_after < 0

        def interrupt_pane(pane):
            self.events.append("ctrl-c")

        def close_pane(key, pane):
            self.events.append(f"close {pane}")
            self.panes.pop(pane, None)

        return SwitchHooks(native, busy, cancel, release, open_pane, pane_idle, interrupt_pane, close_pane)


@pytest.fixture
def log(tmp_path: Path) -> TransitionLog:
    return TransitionLog(tmp_path / "transitions.json")


async def test_chat_to_terminal_and_back_keeps_the_conversation(log: TransitionLog) -> None:
    fake = Fake()
    switch = InterfaceSwitch(fake.hooks(), log, poll_s=0.01)

    there = await switch.to_terminal(KEY, template="claude --resume {agent_id}")
    assert log.mode(KEY) == "terminal" and there.pane == "%7"
    back = await switch.to_chat(KEY)

    assert log.mode(KEY) == "chat"
    assert there.agent_id == back.agent_id == "conv-42", "the same native conversation on both sides"
    assert fake.events == ["release", "open claude --resume conv-42", "close %7"]
    assert [(t.source, t.target, t.phase) for t in log.all()] == [
        ("chat", "terminal", "completed"),
        ("terminal", "chat", "completed"),
    ]


async def test_drain_waits_for_the_turn_in_flight_and_interrupt_stops_it(log: TransitionLog) -> None:
    fake = Fake()
    fake.running = True
    switch = InterfaceSwitch(fake.hooks(), log, poll_s=0.01)

    with pytest.raises(SwitchError, match="did not finish"):
        await switch.to_terminal(KEY, template="claude --resume {agent_id}", timeout_s=0.05)
    assert log.latest(KEY).phase == "failed" and log.mode(KEY) == "chat"

    await switch.to_terminal(KEY, template="claude --resume {agent_id}", policy="interrupt")
    assert fake.events[0] == "cancel" and log.mode(KEY) == "terminal"

    fake.idle_after = 3
    await switch.to_chat(KEY, policy="interrupt")
    assert "ctrl-c" in fake.events and log.mode(KEY) == "chat"


async def test_a_switch_needs_a_conversation_and_a_resuming_command(log: TransitionLog) -> None:
    fake = Fake()
    switch = InterfaceSwitch(fake.hooks(), log, poll_s=0.01)
    with pytest.raises(SwitchError, match="must contain"):
        await switch.to_terminal(KEY, template="claude")
    fake.native_id = None
    with pytest.raises(SwitchError, match="no native conversation"):
        await switch.to_terminal(KEY, template="claude --resume {agent_id}")
    with pytest.raises(SwitchError, match="already in the chat"):
        await switch.to_chat(KEY)
    assert log.all() == []


async def test_a_conversation_that_changed_underneath_fails_the_switch_back(log: TransitionLog) -> None:
    fake = Fake()
    switch = InterfaceSwitch(fake.hooks(), log, poll_s=0.01)
    await switch.to_terminal(KEY, template="claude --resume {agent_id}")
    fake.native_id = "conv-99"

    with pytest.raises(SwitchError, match="conv-99"):
        await switch.to_chat(KEY)
    assert log.mode(KEY) == "terminal", "still where it was"


def test_known_agents_have_a_terminal_resume_command() -> None:
    assert resume_template("claude") == "claude --resume {agent_id}"
    assert resume_template("worker", "npx @zed-industries/codex-acp") == "codex resume {agent_id}"
    assert resume_template("hermes") is None


# -- through the instance registry and the pane board ---------------------------


class Tmux:
    def __init__(self) -> None:
        self.windows: dict[str, list[str]] = {}
        self.sent: list[tuple[str, ...]] = []
        self.commands: list[str] = []
        self._n = 0

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        cmd, *rest = args
        if cmd == "has-session":
            return (0 if self.windows else 1), ""
        if cmd in {"new-session", "new-window"}:
            self.windows[f"@{len(self.windows)}"] = [self._pane()]
            window = f"@{len(self.windows) - 1}"
            return 0, f"{window} {self.windows[window][0]}"
        if cmd == "split-window":
            pane = self._pane()
            self.windows["@0"].append(pane)
            self.commands.append(rest[-1])
            return 0, pane
        if cmd == "display-message":
            return 0, ("0" if rest[-1] == "#{pane_active}" else "240 60")
        if cmd == "list-panes":
            return 0, "\n".join(p for ps in self.windows.values() for p in ps)
        if cmd == "capture-pane":
            return 0, "> \n"
        if cmd == "kill-pane":
            for ps in self.windows.values():
                if rest[-1] in ps:
                    ps.remove(rest[-1])
            return 0, ""
        self.sent.append(tuple(args))
        return 0, ""

    def _pane(self) -> str:
        self._n += 1
        return f"%{self._n}"


async def test_the_local_switch_reads_the_registry_and_uses_a_pane(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "instances.json")
    await registry.commit(*KEY, "conv-42", kind="acp")
    tmux = Tmux()
    board = PaneBoard(tmp_path / "board.json", runner=tmux)
    switch = InterfaceSwitch(local_hooks(registry, board), TransitionLog(tmp_path / "t.json"), poll_s=0.01)

    there = await switch.to_terminal(KEY, template="claude --resume {agent_id}")
    seated = [s.name for w in board.windows() for s in w.agents]
    await switch.to_chat(KEY)

    assert tmux.commands == ["claude --resume conv-42"] and seated == ["claude:fixer@cli:1"]
    assert there.pane not in tmux.windows["@0"], "the terminal pane is closed on the way back"
    assert await registry.lookup(*KEY, kind="acp") == "conv-42"


async def test_a_running_chat_turn_blocks_an_interrupt_from_the_command_line(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "instances.json")
    await registry.commit(*KEY, "conv-42", kind="acp")
    await registry.upsert_spawn(*KEY, "running")
    switch = InterfaceSwitch(
        local_hooks(registry, PaneBoard(tmp_path / "b.json", runner=Tmux())), TransitionLog(tmp_path / "t.json")
    )

    with pytest.raises(SwitchError, match="running in the gateway"):
        await switch.to_terminal(KEY, template="claude --resume {agent_id}", policy="interrupt")


async def test_direct_chat_is_refused_while_the_terminal_holds_the_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import interface_switch
    from raven.agent.subagent.manager import SubagentManager
    from raven.config.schema import Config

    path = tmp_path / "t.json"
    monkeypatch.setattr(interface_switch, "default_path", lambda: path)
    switch = InterfaceSwitch(Fake().hooks(), TransitionLog(path), poll_s=0.01)
    await switch.to_terminal(KEY, template="claude --resume {agent_id}")
    assert interface_mode(*KEY) == "terminal"

    config = Config.model_validate(
        {
            "subagents": {
                "agents": [
                    {
                        "name": "claude",
                        "kind": "cli",
                        "command": "x --session {agent_id} {prompt}",
                        "resumeCommand": "x --resume {agent_id} {prompt}",
                    }
                ]
            }
        }
    )
    manager = SubagentManager(
        provider=None,  # type: ignore[arg-type]
        workspace=tmp_path,
        model="m",
        exec_config=config.tools.exec,
        agents=config.subagents.agents,
    )
    with pytest.raises(RuntimeError, match="in its terminal"):
        await manager.chat(session_key="cli:1", agent="claude", handle="fixer", text="hi")


def test_the_command_line_switches_and_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from raven.agent.subagent import instances, interface_switch
    from raven.cli.commands import app
    from raven.ops import pane_board

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    registry_path = tmp_path / "instances.json"
    asyncio.run(InstanceRegistry(registry_path).commit(*KEY, "conv-42", kind="acp"))
    monkeypatch.setattr(instances, "default_registry_path", lambda: registry_path)
    monkeypatch.setattr(interface_switch, "default_path", lambda: tmp_path / "t.json")
    tmux = Tmux()

    class Board(pane_board.PaneBoard):
        def __init__(self, state, **kw):
            super().__init__(state, runner=tmux)

    monkeypatch.setattr(pane_board, "PaneBoard", Board)
    runner = CliRunner()

    there = runner.invoke(app, ["sessions", "switch", *KEY, "--to", "terminal"])
    back = runner.invoke(app, ["sessions", "switch", *KEY, "--to", "chat"])
    listed = runner.invoke(app, ["sessions", "switches", "cli:1"])

    assert there.exit_code == 0, there.output
    assert "in the terminal" in there.output and "conversation conv-42" in there.output
    assert back.exit_code == 0 and "back in chat, conversation conv-42" in back.output
    assert (
        "chat -> terminal [drain] completed" in listed.output and "terminal -> chat [drain] completed" in listed.output
    )
    assert json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))[0]["agent_id"] == "conv-42"

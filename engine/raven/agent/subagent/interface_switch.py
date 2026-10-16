"""Switching an agent instance between Raven's chat and the agent's own terminal.

An instance of a stateful agent (Claude Code, Codex, ...) is one native
conversation -- the id the instance registry keeps for ``(session, agent,
handle)``. Raven's direct chat drives it in structured form; the agent's own
terminal interface can drive the very same conversation, resumed by that id. A
switch hands the conversation from one controller to the other, keeping the id
and therefore everything the agent knows:

- to the terminal: the chat side stops taking work -- ``drain`` waits for the
  turn in flight, ``interrupt`` cancels it -- and lets go of the conversation,
  then the agent's terminal resume command opens in a tmux pane on the pane
  board (:mod:`raven.ops.pane_board`);
- back to chat: the terminal side stops -- ``drain`` waits for the agent's idle
  prompt, ``interrupt`` sends Ctrl-C first -- its pane is closed, and the next
  direct chat resumes the same id.

Each switch is written down as it moves through its phases (requested,
draining, source_stopped, target_started, completed or failed), so a switch cut
short is visible and the instance's mode is always known. While an instance is
in the terminal, Raven's direct chat refuses it: two controllers on one
conversation would interleave it.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from raven.utils.atomic_io import atomic_replace

Mode = Literal["chat", "terminal"]
Policy = Literal["drain", "interrupt"]
Key = tuple[str, str, str]

_FILENAME = "interface_transitions.json"

#: How each known agent's own terminal resumes a conversation by its id.
TERMINAL_RESUME: dict[str, str] = {
    "claude": "claude --resume {agent_id}",
    "codex": "codex resume {agent_id}",
}


class SwitchError(RuntimeError):
    """A switch that cannot start or did not finish."""


def default_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


def resume_template(agent: str, command: str = "") -> str | None:
    """The terminal resume template for an agent, from its name or its command line."""
    haystack = f"{agent} {command}".lower()
    for needle, template in TERMINAL_RESUME.items():
        if needle in haystack:
            return template
    return None


@dataclass
class Transition:
    id: str
    session_key: str
    agent: str
    handle: str
    source: str
    target: str
    policy: str
    phase: str
    agent_id: str
    pane: str = ""
    error: str = ""
    created: float = 0.0
    updated: float = 0.0

    @property
    def key(self) -> Key:
        return (self.session_key, self.agent, self.handle)


class TransitionLog:
    """Every switch, as written down phase by phase, in one JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def all(self) -> list[Transition]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [Transition(**row) for row in rows if isinstance(row, dict)]

    def put(self, transition: Transition) -> None:
        transition.updated = time.time()
        rows = [t for t in self.all() if t.id != transition.id] + [transition]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.path, json.dumps([asdict(t) for t in rows], ensure_ascii=False, indent=2))

    def latest(self, key: Key) -> Transition | None:
        mine = [t for t in self.all() if t.key == key]
        return mine[-1] if mine else None

    def mode(self, key: Key) -> Mode:
        """The instance's interface: the target of its last completed switch, else chat."""
        done = [t for t in self.all() if t.key == key and t.phase == "completed"]
        return "terminal" if done and done[-1].target == "terminal" else "chat"


def interface_mode(session_key: str, agent: str, handle: str, path: Path | None = None) -> Mode:
    return TransitionLog(path).mode((session_key or "default", agent, handle))


@dataclass
class SwitchHooks:
    """What a switch needs from where it runs.

    ``native`` reads the instance's native conversation id (or ``None``);
    ``busy`` says whether a chat turn is in flight; ``cancel`` cancels it;
    ``release`` lets the chat side's connection go of the conversation;
    ``open_pane`` starts a command in a pane and returns the pane;
    ``pane_idle`` says whether the terminal agent waits at its prompt;
    ``interrupt_pane`` sends it Ctrl-C; ``close_pane`` closes the pane.
    """

    native: Callable[[Key], Awaitable[str | None]]
    busy: Callable[[Key], Awaitable[bool]]
    cancel: Callable[[Key], Awaitable[None]]
    release: Callable[[Key], Awaitable[None]]
    open_pane: Callable[[Key, str], str]
    pane_idle: Callable[[str], bool]
    interrupt_pane: Callable[[str], None]
    close_pane: Callable[[Key, str], None]


class InterfaceSwitch:
    def __init__(self, hooks: SwitchHooks, log: TransitionLog | None = None, *, poll_s: float = 0.5) -> None:
        self.hooks = hooks
        self.log = log or TransitionLog()
        self.poll_s = poll_s

    async def _wait(self, done: Callable[[], Awaitable[bool]], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while not await done():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.poll_s)
        return True

    def _start(self, key: Key, target: Mode, policy: Policy, agent_id: str) -> Transition:
        if policy not in ("drain", "interrupt"):
            raise SwitchError(f"policy must be drain or interrupt, not {policy!r}")
        latest = self.log.latest(key)
        if latest is not None and latest.phase not in ("completed", "failed"):
            raise SwitchError(f"a switch of this instance is already {latest.phase}")
        source = self.log.mode(key)
        if source == target:
            raise SwitchError(f"the instance is already in the {target}")
        now = time.time()
        transition = Transition(
            id=uuid.uuid4().hex[:12],
            session_key=key[0],
            agent=key[1],
            handle=key[2],
            source=source,
            target=target,
            policy=policy,
            phase="requested",
            agent_id=agent_id,
            pane=latest.pane if latest is not None and source == "terminal" else "",
            created=now,
        )
        self.log.put(transition)
        return transition

    def _fail(self, transition: Transition, error: str) -> SwitchError:
        transition.phase, transition.error = "failed", error
        self.log.put(transition)
        return SwitchError(error)

    def _phase(self, transition: Transition, phase: str) -> None:
        transition.phase = phase
        self.log.put(transition)

    async def to_terminal(
        self, key: Key, *, template: str, policy: Policy = "drain", timeout_s: float = 120.0
    ) -> Transition:
        agent_id = await self.hooks.native(key)
        if not agent_id:
            raise SwitchError("the instance has no native conversation yet; chat with it once first")
        if "{agent_id}" not in template:
            raise SwitchError("the terminal command must contain {agent_id}, or it would not resume this conversation")
        transition = self._start(key, "terminal", policy, agent_id)
        self._phase(transition, "draining")
        try:
            if policy == "interrupt" and await self.hooks.busy(key):
                await self.hooks.cancel(key)
            if not await self._wait(lambda: self._idle_chat(key), timeout_s):
                raise SwitchError(f"the chat turn in flight did not finish in {timeout_s:.0f}s")
            await self.hooks.release(key)
        except Exception as exc:  # noqa: BLE001 - every way out of a started switch is recorded
            raise self._fail(transition, str(exc)) from None
        self._phase(transition, "source_stopped")
        try:
            transition.pane = self.hooks.open_pane(key, template.replace("{agent_id}", agent_id))
        except Exception as exc:  # noqa: BLE001 - recorded as the switch's failure
            raise self._fail(transition, f"the terminal did not open: {exc}") from None
        self._phase(transition, "target_started")
        self._phase(transition, "completed")
        return transition

    async def _idle_chat(self, key: Key) -> bool:
        return not await self.hooks.busy(key)

    async def to_chat(self, key: Key, *, policy: Policy = "drain", timeout_s: float = 120.0) -> Transition:
        latest = self.log.latest(key)
        agent_id = latest.agent_id if latest is not None else ""
        transition = self._start(key, "chat", policy, agent_id)
        pane = transition.pane
        self._phase(transition, "draining")
        if pane:

            async def idle() -> bool:
                return self.hooks.pane_idle(pane)

            try:
                if policy == "interrupt":
                    self.hooks.interrupt_pane(pane)
                if not await self._wait(idle, timeout_s):
                    raise SwitchError(f"the terminal agent did not come back to its prompt in {timeout_s:.0f}s")
                self.hooks.close_pane(key, pane)
            except Exception as exc:  # noqa: BLE001 - every way out of a started switch is recorded
                raise self._fail(transition, str(exc)) from None
        self._phase(transition, "source_stopped")
        now_id = await self.hooks.native(key)
        if agent_id and now_id and now_id != agent_id:
            raise self._fail(transition, f"the instance now names conversation {now_id}, not {agent_id}")
        self._phase(transition, "target_started")
        self._phase(transition, "completed")
        return transition


def seat_name(key: Key) -> str:
    """The pane board's name for an instance's terminal."""
    session_key, agent, handle = key
    return f"{agent}:{handle}@{session_key}"


def local_hooks(registry: Any, board: Any, runner: Any = None) -> SwitchHooks:
    """Hooks for a switch run from this machine's command line.

    The chat side's state is read from the instance registry the gateway writes
    (a turn in flight is a ``running`` row). This process holds no chat
    connection and cannot cancel a gateway turn, so ``interrupt`` here only
    stops waiting for one -- and a ``running`` turn makes the switch refuse
    rather than open a second controller beside it.
    """
    from raven.ops.panes import TmuxPane, tmux_runner

    # The board's own tmux, so the pane checks talk to the server the panes are on.
    run = runner or getattr(board, "_run", None) or tmux_runner

    async def native(key: Key) -> str | None:
        row = await registry.row(*key)
        return (row or {}).get("agentId") or None

    async def busy(key: Key) -> bool:
        row = await registry.row(*key)
        return (row or {}).get("status") == "running"

    async def cancel(key: Key) -> None:
        raise SwitchError("a chat turn is running in the gateway; wait for it (drain) or stop it there")

    async def release(key: Key) -> None:
        return None

    def open_pane(key: Key, command: str) -> str:
        return board.place([seat_name(key)], command)[0][2]

    def pane_idle(pane: str) -> bool:
        state = TmuxPane(pane, run).state()
        return state.ready and not state.typing

    def interrupt_pane(pane: str) -> None:
        run(("send-keys", "-t", pane, "C-c"))

    def close_pane(key: Key, pane: str) -> None:
        board.remove(seat_name(key))

    return SwitchHooks(native, busy, cancel, release, open_pane, pane_idle, interrupt_pane, close_pane)


__all__ = [
    "TERMINAL_RESUME",
    "InterfaceSwitch",
    "SwitchError",
    "SwitchHooks",
    "Transition",
    "TransitionLog",
    "interface_mode",
    "local_hooks",
    "resume_template",
    "seat_name",
]

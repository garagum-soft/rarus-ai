"""What a scenario script is handed: ``sc``, its way to run agents, talk to them and wait.

A run is one task on one roster agent -- any kind the roster holds: Raven's own
loop, a CLI agent, an ACP agent -- through the same dispatch gate every other
lane waits on. ``await sc.run(...)`` runs and waits; ``sc.start(...)`` returns a
handle to await later, so several agents work at once. Every run of one
scenario is a participant of the scenario's own agent mail under its name, and
so is the script (as ``scenario``): an agent can write to another, or to the
script with ``send_message``, and the script can write to a running agent and
wait for an answer. A run that fails or times out comes back as a reply with
``ok`` false rather than an exception, so a script decides what a failure
means; naming an agent the roster does not have is the script's own mistake
and raises.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven.agent.subagent.mailbox import Mailbox, Message, deliver_live
from raven.scenario.journal import Journal

SCRIPT = "scenario"
"""The script's own name in the scenario's mail."""

_EXCERPT = 4000


class ScenarioError(Exception):
    """A scenario that cannot go on: an agent the roster lacks, a malformed call."""


@dataclass
class Reply:
    agent: str
    name: str
    text: str
    ok: bool
    error: str = ""
    seconds: float = 0.0

    def __str__(self) -> str:
        return self.text


class Run:
    """A run started with ``sc.start``: await it for its :class:`Reply`."""

    def __init__(self, name: str, agent: str, task: asyncio.Task[Reply]) -> None:
        self.name = name
        self.agent = agent
        self.task = task

    def __await__(self):
        return self.task.__await__()

    @property
    def done(self) -> bool:
        return self.task.done()

    def cancel(self) -> None:
        self.task.cancel()


class Scenario:
    def __init__(
        self,
        *,
        name: str,
        run_id: str,
        args: dict[str, str],
        workspace: Path,
        manager: Any,
        journal: Journal,
        mail_session: str,
        echo: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.name = name
        self.run_id = run_id
        self.args = dict(args)
        self.workspace = workspace
        self.mail_session = mail_session
        self._manager = manager
        self._journal = journal
        self._echo = echo
        self._runs: list[Run] = []
        self._consumed: set[str] = set()
        self.mailbox = Mailbox(mail_session)
        self.mailbox.join(SCRIPT, SCRIPT, SCRIPT)

    # -- agents ------------------------------------------------------------------------------

    def agents(self) -> list[str]:
        """The roster agents a run may name."""
        return self._manager.registry.names()

    def _check_agent(self, agent: str) -> None:
        row = self._manager.registry.get(agent)
        if row is None or not row.enabled:
            raise ScenarioError(f"no enabled agent {agent!r}; the roster has: {', '.join(self.agents()) or 'nothing'}")

    def _unique(self, wanted: str) -> str:
        live = {r.name for r in self._runs if not r.done}
        if wanted not in live:
            return wanted
        n = 2
        while f"{wanted}-{n}" in live:
            n += 1
        return f"{wanted}-{n}"

    def start(
        self,
        agent: str,
        task: str,
        *,
        name: str | None = None,
        timeout: float | None = None,
        workspace: Path | str | None = None,
    ) -> Run:
        """Start ``task`` on ``agent`` and return at once; ``await`` the handle for the reply."""
        self._check_agent(agent)
        if not task or not task.strip():
            raise ScenarioError("a run needs a task")
        who = self._unique(name or agent)
        # Joined now, not when the run reaches the gate: the script and the other
        # runs can write to it while it is still queued.
        self.mailbox.join(who, agent, who)
        where = Path(workspace) if workspace else self.workspace
        run = Run(who, agent, asyncio.create_task(self._one(agent, task, who, timeout, where), name=f"scenario:{who}"))
        self._runs.append(run)
        return run

    async def run(self, agent: str, task: str, **kw: Any) -> Reply:
        """Run ``task`` on ``agent`` and wait for its reply."""
        return await self.start(agent, task, **kw)

    async def _one(self, agent: str, task: str, name: str, timeout: float | None, workspace: Path) -> Reply:
        from raven.agent.subagent.backends.base import llm_error_reply

        began = time.monotonic()
        self._journal.event("run.start", name=name, agent=agent, task=task[:_EXCERPT], workspace=str(workspace))
        self._echo(f"→ {name} ({agent}) started")
        call = self._manager.run_in(agent, task, workspace, mail_session=self.mail_session, mail_name=name)
        try:
            text = await (asyncio.wait_for(call, timeout) if timeout else call)
            failure = llm_error_reply(text)
            reply = Reply(agent, name, "" if failure else str(text or ""), ok=failure is None, error=failure or "")
        except TimeoutError:
            reply = Reply(agent, name, "", ok=False, error=f"timed out after {timeout:g}s")
        except asyncio.CancelledError:
            self._journal.event("run.cancelled", name=name, agent=agent)
            raise
        except Exception as exc:  # noqa: BLE001 -- a failed run is a reply the script reads
            reply = Reply(agent, name, "", ok=False, error=f"{type(exc).__name__}: {exc}")
        reply.seconds = round(time.monotonic() - began, 2)
        self._journal.event(
            "run.end",
            name=name,
            agent=agent,
            ok=reply.ok,
            error=reply.error,
            seconds=reply.seconds,
            reply=reply.text[:_EXCERPT],
        )
        self._echo(
            f"{'✓' if reply.ok else '✗'} {name} ({agent}) {reply.seconds:.1f}s"
            + (f": {reply.error}" if reply.error else "")
        )
        return reply

    async def wait(self, *runs: Run, timeout: float | None = None) -> list[Reply]:
        """Every reply, in the order given."""
        gathered = asyncio.gather(*(r.task for r in runs))
        return list(await (asyncio.wait_for(gathered, timeout) if timeout else gathered))

    async def first(self, *runs: Run, timeout: float | None = None, cancel_rest: bool = True) -> Reply:
        """The first reply that is ``ok``; the first failure if none is. The rest are cancelled unless told not to."""
        if not runs:
            raise ScenarioError("first() needs at least one run")
        pending = {r.task for r in runs}
        deadline = None if timeout is None else time.monotonic() + timeout
        failed: Reply | None = None
        try:
            while pending:
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                done, pending = await asyncio.wait(pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError(f"no run finished within {timeout:g}s")
                for task in done:
                    reply = task.result()
                    if reply.ok:
                        return reply
                    failed = failed or reply
            return failed  # type: ignore[return-value] -- every run finished, none ok, so one failed
        finally:
            if cancel_rest:
                for task in pending:
                    task.cancel()

    # -- mail --------------------------------------------------------------------------------

    async def send(self, to: str, body: str, *, kind: str = "note", reply_to: str | None = None) -> Message:
        """Write to a participant -- a run by its name, or an agent by its roster name."""
        message = self.mailbox.send(SCRIPT, to, body, kind=kind, reply_to=reply_to, sender_agent=SCRIPT)
        self._journal.event("mail.out", id=message.id, to=message.to, kind=kind, body=body[:_EXCERPT])
        await deliver_live(self.mailbox, message.to)
        return message

    async def receive(
        self,
        *,
        sender: str | None = None,
        match: str | None = None,
        timeout: float = 300.0,
        every: float = 0.5,
    ) -> Message:
        """The next message to the script, from ``sender`` and matching the ``match`` pattern when given."""
        pattern = re.compile(match, re.IGNORECASE | re.DOTALL) if match else None
        deadline = time.monotonic() + timeout
        while True:
            for message in self.mailbox.history(member=SCRIPT, limit=0):
                if message.to != SCRIPT or message.id in self._consumed:
                    continue
                if sender is not None and message.sender.lower() != sender.lower():
                    continue
                if pattern is not None and not pattern.search(message.body):
                    continue
                self._consumed.add(message.id)
                self._journal.event("mail.in", id=message.id, sender=message.sender, body=message.body[:_EXCERPT])
                return message
            if time.monotonic() >= deadline:
                what = " ".join(
                    x for x in (f"from {sender}" if sender else "", f"matching {match!r}" if match else "") if x
                )
                raise TimeoutError(f"no message{' ' + what if what else ''} within {timeout:g}s")
            await asyncio.sleep(every)

    def messages(self) -> list[Message]:
        """Every message of this scenario so far, oldest first."""
        return self.mailbox.history(limit=0)

    # -- waiting and the record --------------------------------------------------------------

    async def wait_for(
        self,
        check: Callable[[], Any | Awaitable[Any]],
        *,
        timeout: float,
        every: float = 1.0,
        what: str = "the condition",
    ) -> Any:
        """Poll ``check`` until it returns something truthy, and return that."""
        deadline = time.monotonic() + timeout
        while True:
            value = check()
            if inspect.isawaitable(value):
                value = await value
            if value:
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{what} did not hold within {timeout:g}s")
            await asyncio.sleep(every)

    def log(self, text: str) -> None:
        self._journal.event("log", text=str(text)[:_EXCERPT])
        self._echo(f"· {text}")

    async def close(self) -> list[dict[str, Any]]:
        """Cancel what is still running; a summary of every run for the record."""
        live = [r.task for r in self._runs if not r.done]
        for task in live:
            task.cancel()
        if live:
            await asyncio.gather(*live, return_exceptions=True)
        summary = []
        for r in self._runs:
            reply = r.task.result() if r.task.done() and not r.task.cancelled() and r.task.exception() is None else None
            summary.append(
                {
                    "name": r.name,
                    "agent": r.agent,
                    "ok": bool(reply and reply.ok),
                    "cancelled": r.task.cancelled(),
                    "seconds": reply.seconds if reply else None,
                }
            )
        return summary


__all__ = ["SCRIPT", "Reply", "Run", "Scenario", "ScenarioError"]

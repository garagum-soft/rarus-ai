"""Agent mail: sub-agents of one conversation write to each other by name.

Until now every exchange between sub-agents went through the coordinator: a
result came back to the parent as a new turn, a DAG node read its upstream's
output file, a discussion was run round by round by its host. Mail is the
direct line. A run addresses another participant of the same conversation by
name, the message is written down first -- the log is the authority and the
history -- and only then delivered:

- to a built-in run in flight, at its next step (``take`` before each model
  call appends unread mail to the transcript's tail);
- to an agent whose live turn takes steering (an ACP agent that declares it),
  at once;
- to anyone else, at the start of their next turn, or when they read their
  inbox (``read_messages``, ``raven mail inbox``).

Names. A participant is the instance handle it runs under when it has one (a
named or minted instance, a DAG node's id), else its roster agent name. A
recipient is matched by that name exactly, then without case, then by agent
name when exactly one participant runs that agent; anything else is refused
with the names that would have worked -- mail to a typo is never a black hole.
A participant joins when a run starts under its name, and a DAG joins all its
nodes when it starts, so a node can write to one that has not run yet.

One file pair per conversation under ``<RAVEN_HOME>/mailbox``: an append-only
``.jsonl`` log (the history, never consumed) and a ``.json`` state holding the
members, the next sequence number and how far each member has read. Both are
written under the state's lock, so other processes -- an external CLI agent
running ``raven mail send`` -- share it safely.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace, locked_append, write_transaction

MAX_BODY_CHARS = 8000
"""A message is a note between agents, not a payload: files carry payloads."""

KINDS = ("note", "question", "reply")


class MailError(Exception):
    """A message that cannot be sent, with a code a caller can branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Message:
    id: str
    seq: int
    at: float
    sender: str
    to: str
    body: str
    kind: str = "note"
    reply_to: str | None = None

    def render(self) -> str:
        head = f"[{self.id} from {self.sender}"
        if self.kind == "question":
            head += ", expects a reply"
        elif self.kind == "reply" and self.reply_to:
            head += f", reply to {self.reply_to}"
        return f"{head}]\n{self.body}"


@dataclass(frozen=True)
class Identity:
    """Who the code running now speaks as: a member of one conversation's mail."""

    session_key: str
    name: str
    agent: str
    handle: str


_speaker: ContextVar[Identity | None] = ContextVar("raven_mail_speaker", default=None)


def current() -> Identity | None:
    """The participant this run speaks as, if a run is speaking as one."""
    return _speaker.get()


def participant_name(agent: str, handle: str, *, named: bool) -> str:
    return handle if named and handle else agent


@contextmanager
def speaking_as(session_key: str | None, agent: str | None, handle: str, *, named: bool) -> Iterator[None]:
    """Run the block as a mail participant, joining the conversation's mail.

    No conversation, no mail: a one-shot run outside any session has nobody to
    write to. Joining is best effort -- a mail directory that cannot be written
    must not stop the run it was only going to annotate.
    """
    if not session_key or not agent:
        yield
        return
    me = Identity(session_key, participant_name(agent, handle, named=named), agent, handle)
    try:
        Mailbox(session_key).join(me.name, agent, handle)
    except OSError as exc:
        logger.warning("mail: could not join {} to {}: {}", me.name, session_key, exc)
    token = _speaker.set(me)
    try:
        yield
    finally:
        _speaker.reset(token)


def default_root() -> Path:
    from raven.home import raven_home

    return raven_home() / "mailbox"


def _slug(session_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key)[:48].strip("._") or "session"
    return f"{safe}-{hashlib.sha1(session_key.encode('utf-8')).hexdigest()[:10]}"


class Mailbox:
    """One conversation's mail: members, the message log and read cursors."""

    def __init__(self, session_key: str, root: Path | None = None) -> None:
        self.session_key = session_key
        base = root or default_root()
        slug = _slug(session_key)
        self.state_path = base / f"{slug}.json"
        self.log_path = base / f"{slug}.jsonl"

    # -- state -----------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        raw.setdefault("session", self.session_key)
        raw.setdefault("members", {})
        raw.setdefault("next_seq", 1)
        raw.setdefault("read", {})
        return raw

    def _save(self, state: dict[str, Any]) -> None:
        atomic_replace(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))

    def members(self) -> dict[str, dict[str, Any]]:
        return dict(self._state()["members"])

    def join(self, name: str, agent: str, handle: str | None = None) -> None:
        self.join_many([(name, agent, handle)])

    def join_many(self, entries: Iterable[tuple[str, str, str | None]]) -> None:
        entries = [(n, a, h) for n, a, h in entries if n]
        if not entries:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.state_path):
            state = self._state()
            changed = False
            for name, agent, handle in entries:
                row = {"agent": agent, "handle": handle or name}
                old = state["members"].get(name)
                if old is None or {k: old.get(k) for k in row} != row:
                    state["members"][name] = {**(old or {"joined": time.time()}), **row}
                    changed = True
            if changed or not self.state_path.exists():
                self._save(state)

    # -- addressing ------------------------------------------------------------

    def resolve(self, target: str, *, sender: str | None = None) -> str:
        """The member ``target`` names, or a MailError saying what would."""
        wanted = (target or "").strip()
        if not wanted:
            raise MailError("recipient_missing", "Name the agent to write to.")
        members = self._state()["members"]
        others = sorted(n for n in members if n != sender)
        name: str | None = None
        if wanted in members:
            name = wanted
        else:
            folded = [n for n in members if n.lower() == wanted.lower()]
            if len(folded) == 1:
                name = folded[0]
            else:
                by_agent = sorted(
                    n for n, row in members.items() if str(row.get("agent", "")).lower() == wanted.lower()
                )
                by_agent = [n for n in by_agent if n != sender] or by_agent
                if len(by_agent) == 1:
                    name = by_agent[0]
                elif len(by_agent) > 1:
                    raise MailError(
                        "recipient_ambiguous",
                        f"More than one participant runs {wanted!r}: {', '.join(by_agent)}. Write to one by name.",
                    )
        if name is None:
            known = ", ".join(others[:20]) or "nobody yet"
            raise MailError(
                "recipient_unknown", f"No participant of this conversation is called {wanted!r}. Known: {known}."
            )
        if sender is not None and name == sender:
            raise MailError("self_send", "That is you; write to another participant.")
        return name

    # -- messages --------------------------------------------------------------

    def send(
        self,
        sender: str,
        to: str,
        body: str,
        *,
        kind: str = "note",
        reply_to: str | None = None,
        sender_agent: str | None = None,
    ) -> Message:
        """Write one message down and return it; delivery is the caller's next step."""
        text = (body or "").strip()
        if not text:
            raise MailError("body_missing", "The message is empty.")
        if len(text) > MAX_BODY_CHARS:
            raise MailError(
                "body_too_long",
                f"The message is {len(text)} characters; the limit is {MAX_BODY_CHARS}. "
                "Put the material in a file and send its path.",
            )
        if kind not in KINDS:
            raise MailError("kind_invalid", f"kind must be one of {', '.join(KINDS)}.")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.state_path):
            state = self._state()
            if sender not in state["members"]:
                state["members"][sender] = {"agent": sender_agent or sender, "handle": sender, "joined": time.time()}
            recipient = self.resolve(to, sender=sender)
            if reply_to is not None and not any(m.id == reply_to for m in self._log()):
                raise MailError("reply_to_unknown", f"There is no message {reply_to!r} to reply to.")
            seq = int(state["next_seq"])
            message = Message(
                id=f"m{seq}",
                seq=seq,
                at=time.time(),
                sender=sender,
                to=recipient,
                body=text,
                kind=kind,
                reply_to=reply_to,
            )
            locked_append(self.log_path, [json.dumps(asdict(message), ensure_ascii=False)])
            state["next_seq"] = seq + 1
            self._save(state)
        return message

    def _log(self) -> list[Message]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[Message] = []
        for line in lines:
            try:
                out.append(Message(**json.loads(line)))
            except (ValueError, TypeError):
                continue
        return out

    def unread(self, name: str) -> list[Message]:
        upto = int(self._state()["read"].get(name, 0))
        return [m for m in self._log() if m.to == name and m.seq > upto]

    def take(self, name: str) -> list[Message]:
        """The unread mail of ``name``, now marked read."""
        if not self.log_path.exists():
            return []
        with write_transaction(self.state_path):
            state = self._state()
            upto = int(state["read"].get(name, 0))
            mail = [m for m in self._log() if m.to == name and m.seq > upto]
            if mail:
                state["read"][name] = max(m.seq for m in mail)
                self._save(state)
        return mail

    def mark_read(self, name: str, upto: int) -> None:
        with write_transaction(self.state_path):
            state = self._state()
            if upto > int(state["read"].get(name, 0)):
                state["read"][name] = upto
                self._save(state)

    def history(self, *, member: str | None = None, peer: str | None = None, limit: int = 50) -> list[Message]:
        """Messages oldest first: all of them, or those ``member`` sent or got, or between two."""
        out = self._log()
        if member is not None:
            out = [m for m in out if member in (m.sender, m.to)]
        if peer is not None:
            out = [m for m in out if peer in (m.sender, m.to)]
        return out[-limit:] if limit > 0 else out


# -- delivery ------------------------------------------------------------------------


def render_inbox(mail: list[Message]) -> str:
    """Mail as a note for the reader's transcript, fenced as the untrusted data it is."""
    from raven.security.trust import wrap_untrusted

    body = "\n\n".join(m.render() for m in mail)
    return (
        "[Agent mail: messages other agents of this conversation sent you. Act on them where they bear on "
        "your task; answer with send_message (kind=reply, reply_to=<id>) when one expects a reply.]\n"
        + wrap_untrusted(body, source="agent_mail")
    )


def render_history(mail: list[Message], me: str | None = None) -> str:
    """Messages as one line each, oldest first; fenced, since the bodies are other agents' words."""
    from raven.security.trust import wrap_untrusted

    lines = []
    for m in mail:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.at))
        arrow = f"{m.sender} -> {m.to}" if me is None else (f"to {m.to}" if m.sender == me else f"from {m.sender}")
        extra = f" (reply to {m.reply_to})" if m.reply_to else (" (question)" if m.kind == "question" else "")
        lines.append(f"{m.id} {stamp} {arrow}{extra}: {m.body}")
    return wrap_untrusted("\n".join(lines), source="agent_mail")


def take_for_current() -> str | None:
    """Unread mail of the participant running now, rendered, or None."""
    me = current()
    if me is None:
        return None
    try:
        mail = Mailbox(me.session_key).take(me.name)
    except OSError as exc:
        logger.warning("mail: could not read the inbox of {}: {}", me.name, exc)
        return None
    return render_inbox(mail) if mail else None


async def deliver_live(box: Mailbox, recipient: str) -> bool:
    """Push the recipient's unread mail into its live turn when that turn takes steering.

    All of it, oldest first, and marked read only once the turn took it: the
    cursor is a high-water mark, so marking the newest alone would skip an
    older message still waiting.
    """
    from raven.agent.subagent import activity

    row = box.members().get(recipient) or {}
    agent, handle = row.get("agent"), row.get("handle")
    if not agent or not handle:
        return False
    run = activity.live_instance(box.session_key, agent, handle)
    steer = getattr(run, "steer", None)
    mail = box.unread(recipient) if steer is not None else []
    if not mail:
        return False
    try:
        outcome = await steer(render_inbox(mail))
    except Exception as exc:  # noqa: BLE001 - a failed push leaves the mail waiting, which is correct
        logger.warning("mail: steering {} failed: {}", recipient, exc)
        return False
    if outcome == "injected":
        box.mark_read(recipient, max(m.seq for m in mail))
        return True
    return False


def env_for_current() -> dict[str, str]:
    """Environment naming the current participant, for a child that runs ``raven mail``."""
    me = current()
    if me is None:
        return {}
    return {"RAVEN_MAIL_SESSION": me.session_key, "RAVEN_MAIL_AS": me.name}


__all__ = [
    "KINDS",
    "MAX_BODY_CHARS",
    "Identity",
    "MailError",
    "Mailbox",
    "Message",
    "current",
    "deliver_live",
    "env_for_current",
    "participant_name",
    "render_history",
    "render_inbox",
    "speaking_as",
    "take_for_current",
]

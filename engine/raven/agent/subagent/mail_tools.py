"""``send_message`` and ``read_messages``: the agent-mail tools of a sub-agent run.

Both speak as the participant the run was started under (``mailbox.speaking_as``),
so the sender is never something the model writes: a run cannot sign another
agent's name.
"""

from __future__ import annotations

from typing import Any

from raven.agent.subagent import mailbox
from raven.contracts.tool import Tool

_NO_MAIL = "Error: agent mail needs a run inside a conversation; this one has none."


class SendMessageTool(Tool):
    """Write to another agent of this conversation by name."""

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def description(self) -> str:
        return (
            "Send a message straight to another agent working in this conversation, by its name (an "
            "instance or DAG node name, or its agent name). It is kept in the mail history and reaches "
            "the recipient at its next step if it is running, else when it next starts. Use kind=question "
            "when you need an answer, and kind=reply with reply_to=<message id> to answer one."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "minLength": 1, "description": "The recipient's name."},
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": mailbox.MAX_BODY_CHARS,
                    "description": "What to tell it. Send file paths, not file contents.",
                },
                "kind": {"type": "string", "enum": list(mailbox.KINDS), "description": "Default note."},
                "reply_to": {"type": "string", "description": "The id of the message this answers (m12)."},
            },
            "required": ["to", "message"],
        }

    async def execute(self, **kwargs: Any) -> str:
        me = mailbox.current()
        if me is None:
            return _NO_MAIL
        box = mailbox.Mailbox(me.session_key)
        try:
            sent = box.send(
                me.name,
                str(kwargs.get("to") or ""),
                str(kwargs.get("message") or ""),
                kind=str(kwargs.get("kind") or "note"),
                reply_to=(str(kwargs["reply_to"]) if kwargs.get("reply_to") else None),
                sender_agent=me.agent,
            )
        except mailbox.MailError as exc:
            return f"Error: {exc}"
        now = await mailbox.deliver_live(box, sent.to)
        where = "delivered into its running turn" if now else "waiting in its inbox"
        return f"Sent {sent.id} to {sent.to} ({where})."


class ReadMessagesTool(Tool):
    """Read this agent's mail: what is unread, or the history with one peer."""

    @property
    def name(self) -> str:
        return "read_messages"

    @property
    def description(self) -> str:
        return (
            "Read your agent mail. By default returns your unread messages and marks them read; with "
            "history=true returns earlier messages you sent or got (optionally only those with one agent). "
            "New mail also arrives on its own at your next step."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "history": {"type": "boolean", "description": "Read the history instead of the unread mail."},
                "with_agent": {"type": "string", "description": "Only the messages with this agent (history)."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "History length, default 20.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        me = mailbox.current()
        if me is None:
            return _NO_MAIL
        box = mailbox.Mailbox(me.session_key)
        limit = int(kwargs.get("limit") or 20)
        if kwargs.get("history"):
            peer = None
            if kwargs.get("with_agent"):
                try:
                    peer = box.resolve(str(kwargs["with_agent"]))
                except mailbox.MailError as exc:
                    return f"Error: {exc}"
            mail = box.history(member=me.name, peer=peer, limit=limit)
            if not mail:
                return "No messages yet."
            return mailbox.render_history(mail, me.name)
        mail = box.take(me.name)
        if not mail:
            return "No unread messages."
        return mailbox.render_inbox(mail)


__all__ = ["ReadMessagesTool", "SendMessageTool"]

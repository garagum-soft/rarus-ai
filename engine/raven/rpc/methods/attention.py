"""The operator's attention queue over RPC: what is waiting on a person, most pressing first.

``attention.list`` reads the queue (``raven.proactive_engine.attention``) the
brokers and the host guard write to; ``attention.resolve`` dismisses an item
by hand. The queue is an overview across conversations, so it is not filtered
by which connection owns a conversation: answering still happens where the
request lives (``approval.respond``, ``clarify.respond``, ``credential.submit``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _wire(item: Any, now: float) -> dict[str, Any]:
    row = item.to_json(now)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "priority": row["priority"],
        "effective_priority": row["effective_priority"],
        "title": row["title"],
        "detail": row["detail"],
        "session_key": row["session_key"],
        "source": row["source"],
        "created": row["created"],
        "updated": row["updated"],
        "status": row["status"],
        "resolution": row["resolution"],
    }


async def attention_list(params: dict[str, Any]) -> dict[str, Any]:
    import time

    from raven.proactive_engine.attention import queue

    now = time.time()
    session_key = params.get("session_key") or None
    include_resolved = bool(params.get("include_resolved"))
    q = queue()
    items = await asyncio.to_thread(q.items, session_key=session_key, include_resolved=include_resolved, now=now)
    summary = await asyncio.to_thread(q.summary, now=now)
    return {"items": [_wire(i, now) for i in items], "summary": summary}


async def attention_resolve(params: dict[str, Any]) -> dict[str, Any]:
    from raven.proactive_engine.attention import queue

    item_id = str(params.get("id") or "")
    if not item_id:
        return {"resolved": False}
    resolution = str(params.get("resolution") or "dismissed")
    return {"resolved": await asyncio.to_thread(queue().resolve, item_id, resolution)}


def register_attention_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("attention.list", attention_list)
    dispatcher.register("attention.resolve", attention_resolve)


__all__ = ["attention_list", "attention_resolve", "register_attention_methods"]

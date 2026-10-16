"""One workflow over a list of inputs: concurrent runs, an event stream, a result per input.

Each input is one run of the same workflow. Runs go concurrently up to a limit,
a failing input is recorded as that input's failure rather than ending the
batch, and the results come back in input order whatever order they finished
in. Progress is a stream of events -- the batch starting, each input starting
and finishing, a running count -- handed to a callback as they happen, so a
caller can print them as lines, put them on a socket, or ignore them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Event = dict[str, Any]
OnEvent = Callable[[Event], Awaitable[None] | None]


@dataclass
class BatchItem:
    index: int
    input: Mapping[str, Any]
    ok: bool = False
    result: Any = None
    error: str = ""
    seconds: float = 0.0


async def _emit(on_event: OnEvent | None, event: Event) -> None:
    if on_event is None:
        return
    outcome = on_event(event)
    if asyncio.iscoroutine(outcome):
        await outcome


async def run_batch(
    inputs: Sequence[Mapping[str, Any]],
    run_one: Callable[[int, Mapping[str, Any]], Awaitable[Any]],
    *,
    max_concurrency: int = 4,
    on_event: OnEvent | None = None,
) -> list[BatchItem]:
    """Run ``run_one`` once per input; every input gets a ``BatchItem``, in input order."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    items = [BatchItem(index=i, input=dict(inp)) for i, inp in enumerate(inputs)]
    total = len(items)
    gate = asyncio.Semaphore(max_concurrency)
    done = 0
    await _emit(on_event, {"event": "batch_started", "total": total})

    async def one(item: BatchItem) -> None:
        nonlocal done
        async with gate:
            await _emit(on_event, {"event": "item_started", "index": item.index, "input": item.input})
            started = time.monotonic()
            try:
                item.result = await run_one(item.index, item.input)
                item.ok = True
            except Exception as exc:  # noqa: BLE001 - one input's failure is that input's result
                item.error = f"{type(exc).__name__}: {exc}"
            item.seconds = round(time.monotonic() - started, 3)
            done += 1
            finished: Event = {"event": "item_finished", "index": item.index, "ok": item.ok, "seconds": item.seconds}
            finished.update({"result": item.result} if item.ok else {"error": item.error})
            await _emit(on_event, finished)
            await _emit(on_event, {"event": "progress", "done": done, "total": total})

    await asyncio.gather(*(one(item) for item in items))
    ok = sum(1 for item in items if item.ok)
    await _emit(on_event, {"event": "batch_finished", "ok": ok, "failed": total - ok})
    return items


__all__ = ["BatchItem", "run_batch"]

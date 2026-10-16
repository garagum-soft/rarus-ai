"""A hook a graph run calls with its result when it finishes, scoped to whoever dispatched it.

A ``ContextVar`` rather than a parameter, for the reason ``run_mcp_scope`` is
one: a backgrounded run keeps the context its task copied when it was created,
so a hook bound around the dispatch reaches the finish of exactly that run and
of no other. The hook may return a different result (to append what it did);
returning ``None`` keeps the original.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

RunFinished = Callable[[str, Any, bool], Awaitable[Any]]

_HOOK: ContextVar[RunFinished | None] = ContextVar("dag_run_finished_hook", default=None)


@contextmanager
def on_run_finished(hook: RunFinished | None) -> Iterator[None]:
    """Bind ``hook`` for runs dispatched inside this block."""
    token = _HOOK.set(hook)
    try:
        yield
    finally:
        _HOOK.reset(token)


def run_finished_hook() -> RunFinished | None:
    return _HOOK.get()


__all__ = ["RunFinished", "on_run_finished", "run_finished_hook"]

"""``instructions.*`` -- the web editor for the agent's instruction and memory files.

The files are a fixed list (``raven.agent.editable_files``); a caller names one
by key, never by path. A save carries the hash of the text it started from and
is refused with ``revision_changed`` when the file moved on, so the page can
show the newer text instead of overwriting it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent import editable_files
from raven.rpc.errors import ConfigValidationError, RevisionChangedRpcError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _agent_home() -> Path:
    from raven.config.loader import load_config

    return Path(load_config().workspace_path).expanduser().resolve()


def _str(params: dict, name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigValidationError(f"{name} is required")
    return value


def _int(params: dict, name: str, *, required: bool = True) -> int | None:
    value = params.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigValidationError(f"{name} must be a revision number (1 or more)")
    return value


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except editable_files.EditConflictError as exc:
        raise RevisionChangedRpcError(str(exc), data={"current_sha256": exc.current_sha256}) from None
    except editable_files.EditError as exc:
        raise ConfigValidationError(str(exc)) from None


async def instructions_list(params: dict) -> dict:
    del params
    return {"files": await _call(editable_files.describe, _agent_home())}


async def instructions_get(params: dict) -> dict:
    return await _call(editable_files.read, _agent_home(), _str(params, "key"))


async def instructions_save(params: dict) -> dict:
    text = params.get("text")
    if not isinstance(text, str):
        raise ConfigValidationError("text is required")
    note = params.get("note") or ""
    return await _call(
        editable_files.save,
        _agent_home(),
        _str(params, "key"),
        text,
        expected_sha256=_str(params, "expected_sha256"),
        author="web",
        note=str(note)[:200],
    )


async def instructions_revision(params: dict) -> dict:
    key, rev = _str(params, "key"), _int(params, "rev")
    text = await _call(editable_files.revision_text, _agent_home(), key, rev)
    return {"key": key, "rev": rev, "text": text}


async def instructions_diff(params: dict) -> dict:
    key = _str(params, "key")
    text = await _call(editable_files.diff, _agent_home(), key, _int(params, "a"), _int(params, "b", required=False))
    return {"key": key, "diff": text}


async def instructions_rollback(params: dict) -> dict:
    return await _call(
        editable_files.rollback,
        _agent_home(),
        _str(params, "key"),
        _int(params, "rev"),
        expected_sha256=_str(params, "expected_sha256"),
        author="web",
    )


def register_instructions_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("instructions.list", instructions_list)
    dispatcher.register("instructions.get", instructions_get)
    dispatcher.register("instructions.save", instructions_save)
    dispatcher.register("instructions.revision", instructions_revision)
    dispatcher.register("instructions.diff", instructions_diff)
    dispatcher.register("instructions.rollback", instructions_rollback)


__all__ = ["register_instructions_methods"]

"""Whether an account's engine may start new work: written by the gateway, read by the engine.

The account gateway launches each engine with ``RAVEN_ACCOUNT`` set, and keeps
``<engine home>/account_gate.json`` saying whether the account is blocked (no
credits left, or over its disk allowance) and why. The engine reads it where
new work begins -- a turn of any origin, and a sub-agent started outside a turn
(a watch, the sentinel) -- and refuses there; work already running is never
touched, so a turn that was going finishes, with its sub-agents.

Without ``RAVEN_ACCOUNT`` nothing here is read: a Raven that is not an account's
engine runs exactly as it always has.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from raven.home import raven_home

ENV_ACCOUNT = "RAVEN_ACCOUNT"
GATE_FILE = "account_gate.json"

_cache: dict[str, Any] = {"path": None, "mtime": None, "value": None}


def account_id() -> str | None:
    """The account this engine serves, or None outside accounts mode."""
    return os.environ.get(ENV_ACCOUNT, "").strip() or None


def gate_path(home: Path | None = None) -> Path:
    return (home or raven_home()) / GATE_FILE


def blocked_reason() -> str | None:
    """Why this engine starts no new work, or None when it may (and always outside accounts mode)."""
    if account_id() is None:
        return None
    path = gate_path()
    try:
        stat = path.stat()
    except OSError:
        return None
    # Not the modification time alone: Windows stamps it from a clock that ticks every
    # ~16 ms, so a rewrite inside one tick keeps it. Every write is a replace, which
    # gives the file a new identity as well.
    mtime = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["value"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = (
        str(raw.get("reason") or "this account is blocked") if isinstance(raw, dict) and raw.get("blocked") else None
    )
    _cache.update(path=str(path), mtime=mtime, value=value)
    return value


def refusal_for_turn(req: Any) -> str | None:
    """The refusal for a turn that would start new work on a blocked account; None to let it run.

    A sub-agent's result coming back is the end of work already running, and a
    text delivered as is makes no model call: both pass.
    """
    reason = blocked_reason()
    if reason is None:
        return None
    origin = str(getattr(req, "origin", "") or "")
    if origin == "subagent" or getattr(req, "deliver_text", None) is not None:
        return None
    return reason


def refusal_for_spawn() -> str | None:
    """The refusal for a sub-agent started outside any turn (a watch, the sentinel) on a blocked account.

    Inside a turn the spawn is part of work already running, and finishes with it.
    """
    reason = blocked_reason()
    if reason is None:
        return None
    from raven.token_wise import usage_context

    return reason if usage_context.session_key() is None else None


def write_gate(home: Path, *, blocked: bool, reason: str = "", detail: dict[str, Any] | None = None) -> bool:
    """The gateway's side: record the account's state in its engine home; True when it changed."""
    path = gate_path(home)
    payload = {"blocked": bool(blocked), "reason": reason if blocked else "", **(detail or {})}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if (
        isinstance(current, dict)
        and current.get("blocked") == payload["blocked"]
        and current.get("reason") == payload["reason"]
    ):
        return False
    from raven.utils.atomic_io import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, json.dumps({**payload, "updated": time.time()}, ensure_ascii=False))
    return True


NO_CREDITS = (
    "This account has no credits left, so no new work starts until an administrator tops it up. "
    "Work that was already running finishes."
)


def over_disk(used_gb: float, quota_gb: float) -> str:
    return (
        f"This account uses {used_gb:.1f} GB of its {quota_gb:g} GB disk allowance, so no new work starts "
        "until files are removed or the allowance is raised. Work that was already running finishes."
    )


__all__ = [
    "ENV_ACCOUNT",
    "GATE_FILE",
    "NO_CREDITS",
    "account_id",
    "blocked_reason",
    "gate_path",
    "over_disk",
    "refusal_for_spawn",
    "refusal_for_turn",
    "write_gate",
]

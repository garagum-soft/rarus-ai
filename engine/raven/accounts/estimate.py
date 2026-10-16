"""What an external agent spent, for an account to be charged by: as it reported it, or estimated.

Raven's own lanes record every model call they make (the usage file), and so do
the ACP workers Raven itself runs -- folder agents write their rows into the
host's usage file. An external agent does not: a CLI agent (Claude Code, Codex
CLI, Gemini CLI...) runs its model calls out of Raven's sight and reports
nothing, and an external ACP agent at most reports token counts on its frames.

So one row per run is written to ``telemetry/external-usage-<date>.jsonl``,
beside the usage file and never into it (the cost reports read the usage file
alone): the tokens the agent reported when it reported any, otherwise an
estimate from the text that went in and came out -- the tokenizer when it is at
hand, else characters and words -- marked ``estimated``. Only an account's
engine writes these (``RAVEN_ACCOUNT``); a Raven that is not one never does.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.accounts.gate import account_id
from raven.home import raven_home

_WORD = re.compile(r"\w+", re.UNICODE)
# A folder agent's launcher (`run.py --acp`) or Raven's own ACP server: a worker
# that writes its usage into the host's usage file itself.
_RAVEN_WORKER = re.compile(r"run\.py\b.*--acp|-m\s+raven\s+acp|\braven(\.exe)?\s+acp\b", re.I)


def count_tokens(text: str) -> tuple[int, str]:
    """``(tokens, method)``: the cl100k tokenizer, else the larger of characters/4 and words*4/3."""
    text = text or ""
    if not text:
        return 0, "empty"
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=())), "tokenizer"
    except Exception:  # noqa: BLE001 - no tokenizer (offline, no data file): count by hand
        words = len(_WORD.findall(text))
        return max(round(len(text) / 4), round(words * 4 / 3)), "chars-words"


def records_its_own_usage(backend: Any) -> bool:
    """Whether a run on ``backend`` already lands in the usage file, so nothing is written here."""
    kind = str(getattr(backend, "kind", "") or "")
    if kind in ("raven-loop", "openai"):
        return True
    return kind == "acp" and bool(_RAVEN_WORKER.search(str(getattr(backend, "command", "") or "")))


def usage_row(
    agent: str,
    task: str,
    output: str,
    *,
    session_key: str | None,
    kind: str,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> dict[str, Any]:
    reported = tokens_in is not None or tokens_out is not None
    row: dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(),
        "model": f"cli:{agent}",
        "agent": agent,
        "kind": kind,
        "session_key": session_key or "",
        "chars_in": len(task or ""),
        "chars_out": len(output or ""),
        "words_in": len(_WORD.findall(task or "")),
        "words_out": len(_WORD.findall(output or "")),
    }
    if reported:
        row.update(
            input_tokens=int(tokens_in or 0), output_tokens=int(tokens_out or 0), estimated=False, method="reported"
        )
    else:
        count_in, method_in = count_tokens(task)
        count_out, method_out = count_tokens(output)
        method = method_in if method_out in (method_in, "empty") else f"{method_in}/{method_out}"
        row.update(input_tokens=count_in, output_tokens=count_out, estimated=True, method=method)
    return row


def external_usage_path(home: Path | None = None, *, now: float | None = None) -> Path:
    day = time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))
    return (home or raven_home()) / "telemetry" / f"external-usage-{day}.jsonl"


def note_external_usage(
    backend: Any, agent: str, task: str, output: str, *, session_key: str | None, activity: Any = None
) -> None:
    """Write one run's row for an agent whose usage nothing else records; only in an account's engine."""
    if account_id() is None or records_its_own_usage(backend):
        return
    try:
        from raven.utils.atomic_io import locked_append

        row = usage_row(
            agent,
            task,
            output if isinstance(output, str) else str(output or ""),
            session_key=session_key,
            kind=str(getattr(backend, "kind", "") or ""),
            tokens_in=getattr(activity, "tokens_in", None),
            tokens_out=getattr(activity, "tokens_out", None),
        )
        path = external_usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_append(path, [json.dumps(row, ensure_ascii=False)])
    except Exception as exc:  # noqa: BLE001 - accounting is never worth failing the run it accounts
        logger.warning("could not record the usage of {}: {}", agent, exc)


__all__ = ["count_tokens", "external_usage_path", "note_external_usage", "records_its_own_usage", "usage_row"]

"""Watching one file for an open preview: yields a change each time it is rewritten.

A preview holds one file open while an agent edits it. Polling that one file's
modification time and size twice a second is cheap and behaves the same on every
platform and filesystem, which a directory watch does not; ``None`` comes out
now and then as a heartbeat so the stream above can keep a proxy from closing it.
A file that disappears and comes back is reported when it is back.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

#: The most previews one gateway follows at once.
MAX_WATCHES = 32
POLL_S = 0.5
HEARTBEAT_S = 15.0


def _stamp(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime, stat.st_size


async def file_changes(
    path: Path, *, poll_s: float = POLL_S, heartbeat_s: float = HEARTBEAT_S
) -> AsyncIterator[dict[str, Any] | None]:
    """Yield ``{"mtime", "size"}`` on every rewrite of ``path``, ``None`` on each heartbeat; runs until cancelled."""
    last = _stamp(path)
    quiet = 0.0
    while True:
        await asyncio.sleep(poll_s)
        now = _stamp(path)
        if now is not None and now != last:
            last = now
            quiet = 0.0
            yield {"mtime": now[0], "size": now[1]}
            continue
        if now is None:
            last = None
        quiet += poll_s
        if quiet >= heartbeat_s:
            quiet = 0.0
            yield None


__all__ = ["MAX_WATCHES", "file_changes"]

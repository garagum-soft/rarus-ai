"""Reading a source's rows: CSV or JSON, from a file or an http(s) URL."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from raven.tables.store import Source, TableError


async def _read(location: str) -> str:
    if location.startswith(("http://", "https://")):
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as http, http.get(location) as resp:
            if resp.status >= 400:
                raise TableError(f"{location} answered HTTP {resp.status}")
            return await resp.text()
    try:
        return Path(location).expanduser().read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TableError(f"cannot read {location}: {exc}") from None


def _dig(data: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            raise TableError(f"the JSON has nothing at {path!r}")
    return data


def parse(source: Source, text: str) -> list[dict[str, Any]]:
    if source.kind == "csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    try:
        data = _dig(json.loads(text), source.path)
    except json.JSONDecodeError as exc:
        raise TableError(f"{source.location} is not JSON: {exc}") from None
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise TableError(f"{source.location}: expected a list of objects at {source.path or 'the top'}")
    return data


async def fetch_rows(source: Source) -> list[dict[str, Any]]:
    return parse(source, await _read(source.location))


__all__ = ["fetch_rows", "parse"]

"""``code_map``: find one's way in a large code base without reading it.

Answers from the project's code index (:mod:`raven.codemap`), refreshed
incrementally on every call: a ranked map of the files that matter for a
question, each with the outline of what it defines, or where a name is defined.
Reads only what ``read_file`` may read -- the same path fence, resolved the same
way -- and writes nothing but the index under ``<RAVEN_HOME>/codemap/``.

Registered only when ``tools.codeMap`` is on (see ``ToolsConfig.code_map``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from raven.agent.tools.file_search import _denied_traversal_root
from raven.agent.tools.filesystem import _FsTool


class CodeMapTool(_FsTool):
    """A ranked outline of a code base, or a symbol's definitions, from an incremental index."""

    @property
    def name(self) -> str:
        return "code_map"

    @property
    def description(self) -> str:
        return (
            "Map a code base too large to read: the files that matter for a question, each with an outline "
            "of what it defines (classes, functions, methods, signatures, line numbers), ranked by how well "
            "paths and names match `query` and by how many files import them. With `symbol`, where a name is "
            "defined (`Class.method` narrows by owner). With neither, the most depended-on files. Built from "
            "an index kept up to date on each call; on a very large tree the first call may say it is partial "
            "-- call again. Use grep for usages and text, read_file for bodies."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words for what you are looking for, e.g. 'permission rules for tools'",
                },
                "symbol": {
                    "type": "string",
                    "description": "A name to find the definition of, e.g. 'AgentLoop' or 'ToolRegistry.register'",
                },
                "path": {
                    "type": "string",
                    "description": "Project directory or a subtree of it to map (default: workspace root)",
                },
                "focus": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files you are working on: the files they import and that import them rank higher",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Size of the answer in characters (default 6000)",
                    "minimum": 500,
                    "maximum": 40000,
                },
            },
        }

    async def execute(
        self,
        query: str = "",
        symbol: str = "",
        path: str = ".",
        focus: list[str] | None = None,
        max_chars: int | None = None,
        **kwargs: Any,
    ) -> str:
        from raven.codemap.index import REFRESH_DEADLINE_S, CodeIndex, project_root
        from raven.codemap.query import DEFAULT_BUDGET, render_find, render_map

        try:
            base = self._resolve(path)
        except PermissionError as e:
            return f"Error: {e}"
        if not base.exists():
            return f"Error: path not found: {path}"
        if base.is_file():
            focus = [*(focus or []), str(base)]
            base = base.parent
        if _denied_traversal_root(base) or base.resolve() == Path.home().resolve():
            return f"Error: refusing to map '{path}' ({base.resolve()}); name the project directory instead."

        root = await asyncio.to_thread(project_root, base)
        try:
            # The repository may reach above the fence (a workspace inside a bigger
            # checkout); then the fenced directory is the project.
            self._resolve(str(root))
        except PermissionError:
            root = base.resolve()
        under = base.resolve().relative_to(root).as_posix() if base.resolve() != root else ""
        under = "" if under == "." else under
        index = CodeIndex(root)
        try:
            stats = await asyncio.to_thread(index.refresh, deadline_s=REFRESH_DEADLINE_S)
            if symbol.strip():
                text = await asyncio.to_thread(render_find, index, symbol.strip(), under=under)
            else:
                rel_focus = [r for r in (self._relative(root, f) for f in focus or []) if r]
                text = await asyncio.to_thread(
                    render_map, index, query=query, focus=rel_focus, under=under, budget=max_chars or DEFAULT_BUDGET
                )
        except Exception as e:  # noqa: BLE001 -- an index that cannot be read is a tool error, not a crash
            return f"Error building the code map: {e}"
        if stats.partial:
            text = (
                f"(index partial: {stats.parsed + stats.unchanged} of {stats.listed} files read so far; "
                "call again to continue)\n" + text
            )
        return text

    def _relative(self, root: Path, item: str) -> str:
        try:
            resolved = self._resolve(item)
            return resolved.resolve().relative_to(root).as_posix()
        except (PermissionError, ValueError, OSError):
            return ""


__all__ = ["CodeMapTool"]

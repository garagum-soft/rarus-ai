"""Editing a playbook's graph as written: read it, check it, save it back.

What the workflow editor works on is the graph *as the file has it* -- typed
nodes (``forEach`` / ``repeat`` / ``branches`` / ``collect``) included -- and
not what loading makes of it, or a save would flatten every type into its
copies. So this module reads and writes the ``yaml playbook-spec`` block
itself, touching only its node list: the frontmatter, the prose around the
block and every other key in it (params, triggers, servers...) are left as they
were.

``check`` answers what the editor shows beside the graph: the plain nodes it
expands to and everything the library would refuse about it, through the same
expansion and the same validator a load runs. ``save`` writes to the user layer
only (a builtin is shadowed, never edited), checks the result by loading it the
way the library will, and puts the previous file back when that load fails.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from raven.playbook.node_types import NodeTypeError, expand_nodes
from raven.playbook.store import _BLOCK_RE, _FRONTMATTER_RE, PlaybookStore
from raven.playbook.types import NAME_RE, PlaybookSpec
from raven.playbook.validate import validate_structure


class GraphEditError(ValueError):
    """A graph that cannot be read or saved as asked."""


def _split(text: str, name: str) -> tuple[dict[str, Any], dict[str, Any], re.Match[str]]:
    front = _FRONTMATTER_RE.match(text)
    block = _BLOCK_RE.search(text)
    if front is None or block is None:
        raise GraphEditError(f"playbook {name!r} has no frontmatter or no playbook-spec block")
    return yaml.safe_load(front.group(1)) or {}, yaml.safe_load(block.group(1)) or {}, block


def _nodes_of(data: dict[str, Any], name: str) -> list[Any]:
    workflow = data.get("workflow")
    if isinstance(workflow, dict):
        return list(workflow.get("nodes") or [])
    if (data.get("mode") or "dag") != "dag":
        raise GraphEditError(f"playbook {name!r} is a {data.get('mode')} playbook, not a graph")
    return list(data.get("nodes") or [])


def read_graph(store: PlaybookStore, name: str) -> dict[str, Any]:
    """The playbook's graph as written, with what it expands to and what is wrong with it."""
    if store.origin_of(name) is None:
        raise GraphEditError(f"no playbook {name!r}")
    front, data, _ = _split(store.path_for(name).read_text(encoding="utf-8"), name)
    nodes = _nodes_of(data, name)
    return {
        "name": name,
        "description": str(front.get("description") or ""),
        "origin": store.origin_of(name),
        "nodes": nodes,
        **check_graph(nodes, params=data.get("params") or {}),
    }


def check_graph(
    nodes: list[Any], *, params: dict[str, Any] | None = None, known_agents: list[str] | None = None
) -> dict[str, Any]:
    """``{"expanded": [...], "errors": [...]}`` for a node list as an editor holds it."""
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        return {"expanded": [], "errors": ["the graph is a list of steps"]}
    try:
        expanded = expand_nodes(nodes)
    except NodeTypeError as exc:
        return {"expanded": [], "errors": [str(exc)]}
    try:
        spec = PlaybookSpec.model_validate(
            {
                "name": "draft",
                "description": "draft",
                "task_summary": "draft",
                "triggers": {"keywords": ["draft"]},
                "mode": "dag",
                "nodes": expanded,
                "params": params or {},
            }
        )
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return {"expanded": expanded, "errors": errors}
    errors = validate_structure(spec, known_agents=known_agents, allow_blank_fillable=True)
    # Field names as the graph tool's wire spells them (depends_on, node_summary).
    return {"expanded": [n.model_dump(exclude_none=True) for n in spec.nodes], "errors": errors}


def _dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip("\n")


def _new_file(name: str, description: str, nodes: list[Any]) -> str:
    """A whole playbook around a graph: its title and its trigger are its name in words.

    Both are what a person would write first and edit later; the file is plain
    text in the user layer, and an editor of the graph does not own them.
    """
    front = _dump({"name": name, "description": description})
    words = name.replace("-", " ")
    block = {"taskSummary": words, "mode": "dag", "triggers": {"keywords": [words]}, "nodes": nodes}
    return f"---\n{front}\n---\n\n# {name}\n\n{description}\n\n```yaml playbook-spec\n{_dump(block)}\n```\n"


def _rewrite(text: str, name: str, description: str | None, nodes: list[Any]) -> str:
    front, data, block = _split(text, name)
    _nodes_of(data, name)  # refuses a playbook that is not a graph
    if isinstance(data.get("workflow"), dict):
        data["workflow"]["nodes"] = nodes
    else:
        data["nodes"] = nodes
    for key in ("name", "description"):
        data.pop(key, None)
    out = text[: block.start(1)] + _dump(data) + text[block.end(1) :]
    if description is not None and description != front.get("description"):
        front["description"] = description
        fm = _FRONTMATTER_RE.match(out)
        if fm is not None:  # always, as _split checked; kept for the type checker
            out = f"---\n{_dump(front)}\n---\n" + out[fm.end() :]
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def save_graph(
    store: PlaybookStore,
    name: str,
    nodes: list[Any],
    *,
    description: str | None = None,
    overwrite: bool = False,
    known_agents: list[str] | None = None,
) -> dict[str, Any]:
    """Write ``nodes`` as the playbook's graph in the user layer; the checked result, or why not."""
    if not re.fullmatch(NAME_RE, name or ""):
        raise GraphEditError(f"playbook name {name!r} must match {NAME_RE}")
    origin = store.origin_of(name)
    if origin is not None and not overwrite:
        raise GraphEditError(f"playbook {name!r} exists; save over it explicitly")
    if origin is None and not (description or "").strip():
        raise GraphEditError("a new playbook needs a description: it is what matches it to a request")
    checked = check_graph(nodes, known_agents=known_agents)
    if checked["errors"]:
        raise GraphEditError("; ".join(checked["errors"]))
    path = store.root / name / "playbook.md"
    before = path.read_text(encoding="utf-8") if path.exists() else None
    source = before if before is not None else (store.path_for(name).read_text(encoding="utf-8") if origin else None)
    text = (
        _rewrite(source, name, description, nodes) if source is not None else _new_file(name, description or "", nodes)
    )
    _write(path, text)
    try:
        store.load(name)
    except (ValueError, ValidationError) as exc:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _write(path, before)
        raise GraphEditError(f"the saved playbook would not load: {exc}") from exc
    return {"name": name, "path": str(path), "origin": "user", "shadows_builtin": origin == "builtin", **checked}


__all__ = ["GraphEditError", "check_graph", "read_graph", "save_graph"]

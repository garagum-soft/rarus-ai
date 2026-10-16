"""Node types a playbook graph may declare: a fan-out over items, a bounded loop, parallel branches.

The DAG a playbook runs on stays what it is -- plain nodes and ``dependsOn``,
validated and executed by the graph tool unchanged. These types are written in
the playbook file and *expanded* into plain nodes when it is loaded, so every
rule the graph already enforces (ids, references, cycles, agents) applies to
what they produce:

``forEach``
    The node runs once per item -- ``forEach: [a, b, c]`` -- each copy with
    ``{{ item }}`` (and ``{{ item_index }}``) written into its template. The
    copies are ``<id>-1 .. <id>-N`` and run in parallel.

``repeat``
    The node runs N times in sequence -- ``repeat: 3`` -- each pass handed the
    one before it through ``{{ previous }}``: a draft, then its revision, then
    the revision of that. The passes are ``<id>-1 .. <id>-N``.

``branches``
    Different steps at once -- ``branches: [{...}, {...}]`` -- each a node of
    its own (fields it leaves out are the parent's), ``<id>-1 .. <id>-N``.

``collect``
    With any of the three: one more node, under the original id, that waits for
    every copy and receives their results through ``{{ results }}``. A node that
    depends on ``<id>`` then waits for the collected answer. Without ``collect``,
    depending on ``<id>`` waits for every copy (the last pass of a ``repeat``),
    and only a ``repeat``'s ``{{ <id>.output }}`` -- its last pass -- may be read.

Items are written in the file: a list known when the playbook is loaded. A list
that only exists at run time is a batch (``raven playbook batch``) instead.
"""

from __future__ import annotations

import copy
import re
from typing import Any

TYPE_KEYS = ("forEach", "for_each", "repeat", "branches")
MAX_ITEMS = 20
MAX_REPEAT = 10
MAX_BRANCHES = 10
_ITEM = re.compile(r"\{\{\s*item\s*\}\}")
_ITEM_INDEX = re.compile(r"\{\{\s*item_index\s*\}\}")
_PREVIOUS = re.compile(r"\{\{\s*previous\s*\}\}")
_RESULTS = re.compile(r"\{\{\s*results\s*\}\}")
NO_PREVIOUS = "(nothing yet: this is the first pass)"


class NodeTypeError(ValueError):
    """A typed node that cannot be expanded as written."""


def _get(node: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in node:
            return node[name]
    return None


def _deps(node: dict[str, Any]) -> list[str]:
    raw = _get(node, "dependsOn", "depends_on") or []
    return [str(d) for d in raw] if isinstance(raw, list) else [str(raw)]


def _set_deps(node: dict[str, Any], deps: list[str]) -> None:
    for key in ("dependsOn", "depends_on"):
        node.pop(key, None)
    if deps:
        node["dependsOn"] = deps


def _template_key(node: dict[str, Any]) -> str:
    return "prompt_template" if "prompt_template" in node else "promptTemplate"


def _summary_key(node: dict[str, Any]) -> str:
    return "node_summary" if "node_summary" in node else "nodeSummary"


def is_typed(node: Any) -> bool:
    return isinstance(node, dict) and any(key in node for key in TYPE_KEYS)


def _strip(node: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(node)
    for key in (*TYPE_KEYS, "collect"):
        out.pop(key, None)
    return out


def _fan_out_items(node: dict[str, Any], nid: str) -> list[str]:
    raw = _get(node, "forEach", "for_each")
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise NodeTypeError(f"node {nid!r}: forEach takes a non-empty list of items")
    if len(items) > MAX_ITEMS:
        raise NodeTypeError(f"node {nid!r}: forEach runs at most {MAX_ITEMS} items (got {len(items)})")
    if any(isinstance(i, (dict, list)) for i in items):
        raise NodeTypeError(f"node {nid!r}: forEach items are plain values (text or numbers)")
    return [str(i) for i in items]


def _expand_one(node: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """The plain nodes one typed node becomes, the ids that stand for it, and what reads its output.

    Returns ``(nodes, exits, output_of)``: ``exits`` are the ids a node that
    depended on the typed one must now wait for; ``output_of`` is the id whose
    output ``{{ <id>.output }}`` now means, or ``None`` when it means nothing.
    """
    nid = str(node.get("id") or "")
    if not nid:
        raise NodeTypeError("a typed node needs an id")
    kinds = [k for k in TYPE_KEYS if k in node]
    if len({k.replace("_", "").lower() for k in kinds}) > 1:
        raise NodeTypeError(f"node {nid!r}: one of forEach, repeat or branches, not several")
    base = _strip(node)
    deps = _deps(node)
    tkey = _template_key(node)
    skey = _summary_key(node)
    summary = str(base.get(skey) or nid)
    made: list[dict[str, Any]] = []

    if "repeat" in node:
        times = node["repeat"]
        if not isinstance(times, int) or isinstance(times, bool) or not 2 <= times <= MAX_REPEAT:
            raise NodeTypeError(f"node {nid!r}: repeat is a number of passes from 2 to {MAX_REPEAT}")
        for k in range(1, times + 1):
            step = copy.deepcopy(base)
            step["id"] = f"{nid}-{k}"
            prev = f"{{{{ {nid}-{k - 1}.output }}}}" if k > 1 else NO_PREVIOUS
            step[tkey] = _PREVIOUS.sub(lambda _m, p=prev: p, str(base.get(tkey) or ""))
            step[skey] = f"{summary} (pass {k}/{times})"
            _set_deps(step, deps + ([f"{nid}-{k - 1}"] if k > 1 else []))
            made.append(step)
        last = made[-1]["id"]
        exits, output_of = [last], last
    elif "branches" in node:
        rows = node["branches"]
        if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_BRANCHES:
            raise NodeTypeError(f"node {nid!r}: branches is a list of 2 to {MAX_BRANCHES} steps")
        for k, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise NodeTypeError(f"node {nid!r}: branch {k} is not a step")
            step = {**copy.deepcopy(base), **copy.deepcopy(row)}
            step["id"] = f"{nid}-{k}"
            step.setdefault(skey, f"{summary} ({k}/{len(rows)})")
            _set_deps(step, list(dict.fromkeys(deps + _deps(row))))
            made.append(step)
        exits, output_of = [s["id"] for s in made], None
    else:
        items = _fan_out_items(node, nid)
        for k, item in enumerate(items, 1):
            step = copy.deepcopy(base)
            step["id"] = f"{nid}-{k}"
            text = _ITEM.sub(lambda _m, i=item: i, str(base.get(tkey) or ""))
            step[tkey] = _ITEM_INDEX.sub(str(k), text)
            step[skey] = f"{summary} ({k}/{len(items)}: {item[:40]})"
            _set_deps(step, deps)
            made.append(step)
        exits, output_of = [s["id"] for s in made], None

    collect = node.get("collect")
    if collect is not None:
        if not isinstance(collect, dict):
            raise NodeTypeError(f"node {nid!r}: collect is a step (a mapping)")
        joined = {k: copy.deepcopy(v) for k, v in base.items() if k in ("subagent", "agent", "skills", "mcps")}
        joined.update(copy.deepcopy(collect))
        joined["id"] = nid
        ckey = _template_key(joined)
        blocks = "\n\n".join(
            f"Result {k} -- {s.get(skey, s['id'])}:\n{{{{ {s['id']}.output }}}}" for k, s in enumerate(made, 1)
        )
        joined[ckey] = _RESULTS.sub(lambda _m: blocks, str(joined.get(ckey) or "{{ results }}"))
        joined.setdefault(_summary_key(joined), f"{summary} (collected)")
        _set_deps(joined, [s["id"] for s in made])
        made.append(joined)
        exits, output_of = [nid], nid
    return made, exits, output_of


def expand_nodes(nodes: list[Any]) -> list[Any]:
    """Every typed node replaced by the plain nodes it stands for; plain nodes rewired to them.

    Nodes without a type pass through untouched (the same objects when nothing
    in the graph is typed), so a playbook that uses none of this loads exactly
    as before.
    """
    if not any(is_typed(n) for n in nodes or []):
        return nodes
    out: list[Any] = []
    exits: dict[str, list[str]] = {}
    outputs: dict[str, str | None] = {}
    for node in nodes:
        if is_typed(node):
            made, ex, read = _expand_one(node)
            exits[str(node["id"])] = ex
            outputs[str(node["id"])] = read
            out.extend(made)
        else:
            out.append(copy.deepcopy(node))
    for node in out:
        if not isinstance(node, dict):
            continue
        # A typed node with a collect step keeps its id (exits == [id]), so a
        # dependency on it is unchanged; without one it waits for every copy.
        rewired = [d for dep in _deps(node) for d in exits.get(dep, [dep])]
        if rewired != _deps(node):
            _set_deps(node, list(dict.fromkeys(rewired)))
        reader = str(node.get("id"))
        tkey = _template_key(node)
        if isinstance(node.get(tkey), str):
            node[tkey] = _rewrite_reads(node[tkey], outputs, reader)
        for value in (node.get("inputs") or {}).values() if isinstance(node.get("inputs"), dict) else ():
            if isinstance(value, dict) and str(value.get("node")) in outputs:
                value["node"] = _read_target(str(value["node"]), outputs, reader)
    return out


def _read_target(ref: str, outputs: dict[str, str | None], reader: str) -> str:
    target = outputs[ref]
    if target is None:
        raise NodeTypeError(
            f"node {reader!r} reads the output of {ref!r}, which runs several times; "
            f"give {ref!r} a collect step and read that"
        )
    return target


def _rewrite_reads(text: str, outputs: dict[str, str | None], reader: str) -> str:
    def swap(match: re.Match[str]) -> str:
        ref, rest = match.group(1), match.group(2)
        if ref not in outputs:
            return match.group(0)
        return f"{{{{ {_read_target(ref, outputs, reader)}.{rest} }}}}"

    return re.sub(r"\{\{\s*([A-Za-z0-9_-]+)\.(output(?:_path)?)\s*\}\}", swap, text)


def expand_spec_data(data: dict[str, Any]) -> dict[str, Any]:
    """A raw playbook block with its typed nodes expanded, wherever its nodes live (v1 or v2)."""
    if isinstance(data.get("nodes"), list):
        data["nodes"] = expand_nodes(data["nodes"])
    workflow = data.get("workflow")
    if isinstance(workflow, dict) and isinstance(workflow.get("nodes"), list):
        workflow["nodes"] = expand_nodes(workflow["nodes"])
    return data


__all__ = [
    "NO_PREVIOUS",
    "MAX_BRANCHES",
    "MAX_ITEMS",
    "MAX_REPEAT",
    "NodeTypeError",
    "expand_nodes",
    "expand_spec_data",
    "is_typed",
]

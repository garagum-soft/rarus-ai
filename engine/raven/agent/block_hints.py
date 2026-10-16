"""Block explanations: a refusal turned into the concrete step that lifts it, with the config value behind it.

A refusal already says what was refused; what it rarely says is which setting
decided it and what that setting is set to right now. Each rule here pairs a
recognisable refusal (a pattern over its text) with the config keys behind it
and a hint naming them with their current values, so the reader -- the model
reading a tool result, or a person reading the CLI -- learns both why and
what to change. Values are read from the config the caller hands over, and a
key the file does not set reads as the schema's default, the value actually in
force. A refusal no rule recognises gets no hint: a hint naming the wrong
setting is worse than none.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic.alias_generators import to_camel

_UNSET = object()
_ITEM = re.compile(r"^(?P<key>[A-Za-z_]+)\[(?P<field>[A-Za-z_]+)=(?P<value>[^\]]+)\]$")


@dataclass(frozen=True)
class BlockRule:
    """One recognisable refusal: ``settings`` maps a template field to a dotted config key."""

    id: str
    pattern: re.Pattern[str]
    settings: Mapping[str, str]
    template: str


@dataclass(frozen=True)
class BlockHint:
    rule: str
    text: str
    settings: dict[str, Any] = field(default_factory=dict)


def _default(dotted: str) -> Any:
    """The schema's own default for a dotted camelCase key, or ``None`` past a list item."""
    from raven.config.schema import Config

    node: Any = Config.model_construct()
    for part in dotted.split("."):
        if _ITEM.match(part) or not hasattr(type(node), "model_fields"):
            return None
        name = next(
            (n for n, f in type(node).model_fields.items() if part in {n, f.alias, to_camel(n)}),
            None,
        )
        if name is None:
            return None
        node = getattr(node, name)
    return node


def setting(raw: Mapping[str, Any], dotted: str) -> Any:
    """A dotted camelCase key read from a raw config, falling back to the schema default.

    A segment ``teams[name=docs]`` picks the list item whose ``name`` is ``docs``.
    """
    node: Any = raw
    for part in dotted.split("."):
        item = _ITEM.match(part)
        if item and isinstance(node, Mapping):
            entries = node.get(item["key"]) or []
            node = next(
                (e for e in entries if isinstance(e, Mapping) and str(e.get(item["field"])) == item["value"]),
                _UNSET,
            )
        elif isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            node = _UNSET
        if node is _UNSET:
            return _default(dotted)
    return node


def _shown(value: Any) -> str:
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]" if value else "[] (empty)"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}" if value else "{} (empty)"
    return str(value)


RULES: tuple[BlockRule, ...] = (
    BlockRule(
        "team_dispatch_limit",
        re.compile(r"team '(?P<team>[^']+)' hit its sub-agent dispatch limit"),
        {"cap": "subagents.teams[name={team}].maxSpawnsPerHour"},
        "team {team!r} may dispatch {cap} sub-agents per hour "
        "(subagents.teams[name={team}].maxSpawnsPerHour); wait for earlier dispatches to age out, "
        "or raise that team's cap in config.json.",
    ),
    BlockRule(
        "subagent_rate_limit",
        re.compile(r"hit its sub-?agent (?:spawn|dispatch) rate limit"),
        {"cap": "agents.defaults.maxSubagentSpawnsPerHour"},
        "a session may dispatch {cap} sub-agents per hour (agents.defaults.maxSubagentSpawnsPerHour = {cap}); "
        "wait for earlier dispatches to age out, or raise the cap in config.json if the work needs more.",
    ),
    BlockRule(
        "max_tool_iterations",
        re.compile(r"maximum number of tool call iterations"),
        {"cap": "agents.defaults.maxToolIterations"},
        "a turn may make {cap} tool calls (agents.defaults.maxToolIterations = {cap}); "
        "split the task into smaller steps, or raise the cap in config.json.",
    ),
    BlockRule(
        "exec_deny_pattern",
        re.compile(r"matches a denied pattern"),
        {"extra": "tools.exec.extraDenyPatterns"},
        "the command matched a deny pattern; besides the built-in list, tools.exec.extraDenyPatterns is "
        "{extra}. Run a command that avoids the pattern, or ask the operator to edit that list.",
    ),
    BlockRule(
        "permission_deny_rule",
        re.compile(r"blocked by a deny rule in your permissions config"),
        {"tools": "permissions.tools"},
        "permissions.tools is {tools}; the call's tool is set to deny there. "
        "Use another tool, or ask the operator to change that entry.",
    ),
    BlockRule(
        "approval_unattended",
        re.compile(r"requires user approval.*not interactive", re.DOTALL),
        {"mode": "permissions.mode"},
        "permissions.mode is {mode} and nobody is present to approve; run the turn interactively, "
        "or allow the tool for unattended turns in permissions.tools.",
    ),
    BlockRule(
        "workspace_restriction",
        re.compile(r"outside (?:the )?(?:working dir|allowed directories|workspace)"),
        {"restrict": "tools.restrictToWorkspace", "workspace": "agents.defaults.workspace"},
        "tools.restrictToWorkspace is {restrict}, so tools stay inside the workspace "
        "(agents.defaults.workspace = {workspace}); work on a path inside it, "
        "or turn tools.restrictToWorkspace off in config.json.",
    ),
    BlockRule(
        "host_guard",
        re.compile(r"Host resources are past their limits"),
        {
            "disk": "gateway.hostGuard.minDiskFreePercent",
            "disk_gb": "gateway.hostGuard.minDiskFreeGb",
            "memory": "gateway.hostGuard.maxMemoryPercent",
            "cpu": "gateway.hostGuard.maxCpuPercent",
        },
        "the host guard pauses delegation past its limits (gateway.hostGuard: minDiskFreePercent = {disk}, "
        "minDiskFreeGb = {disk_gb}, maxMemoryPercent = {memory}, maxCpuPercent = {cpu}); free the resource, "
        "then resume delegation, or adjust those limits in config.json.",
    ),
)


def explain(text: str, raw: Mapping[str, Any], *, rules: tuple[BlockRule, ...] = RULES) -> BlockHint | None:
    """The hint for the first rule that recognises ``text``, with the current config values filled in."""
    if not text:
        return None
    for rule in rules:
        match = rule.pattern.search(text)
        if match is None:
            continue
        groups = {k: v for k, v in match.groupdict().items() if v is not None}
        values = {name: setting(raw, key.format(**groups)) for name, key in rule.settings.items()}
        shown = {name: _shown(value) for name, value in values.items()}
        return BlockHint(rule.id, rule.template.format(**groups, **shown), values)
    return None


def annotate(text: str, raw: Mapping[str, Any]) -> str:
    """``text`` with a "How to unblock" line appended when a rule recognises it."""
    hint = explain(text, raw)
    return f"{text}\n\nHow to unblock: {hint.text}" if hint else text


def explainer(raw_source: Callable[[], Mapping[str, Any]]) -> Callable[[str], str | None]:
    """A hint function over a late-bound config, for surfaces that hold no config of their own.

    A config that cannot be read gives no hint rather than failing the caller:
    the refusal it would annotate is the result that matters.
    """

    def hint_for(text: str) -> str | None:
        try:
            hint = explain(text, raw_source())
        except Exception:
            return None
        return hint.text if hint else None

    return hint_for


__all__ = ["RULES", "BlockHint", "BlockRule", "annotate", "explain", "explainer", "setting"]

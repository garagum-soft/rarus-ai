"""Where scenarios live and what each one declares, read without running it.

A scenario is a Python file with ``async def main(sc)``. Three places hold them,
the most specific winning a name: the workspace's ``.raven/scenarios/``, the
user's ``<RAVEN_HOME>/scenarios/``, and the ones shipped in
``raven/scenario/builtin/``. Listing parses the file (its docstring and a
literal ``PARAMS`` mapping) and never imports it: a scenario that came with a
cloned repository runs only when someone runs it by name.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "builtin"


def default_home_dir() -> Path:
    from raven.home import raven_home

    return raven_home() / "scenarios"


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    path: Path
    source: str
    """``workspace``, ``home`` or ``builtin``."""
    summary: str = ""
    doc: str = ""
    params: dict[str, str | None] = field(default_factory=dict)
    """Parameter name -> its default; ``None`` marks one the run must be given."""
    problem: str = ""
    """Why this file cannot run, when it cannot (no ``main``, unreadable ``PARAMS``)."""


def name_of(path: Path) -> str:
    return path.stem.replace("_", "-")


def read_spec(path: Path, source: str) -> ScenarioSpec:
    name = name_of(path)
    try:
        # utf-8-sig: an editor on Windows may save the file with a byte-order mark,
        # which the import that runs it accepts and a plain decode would refuse.
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, ValueError) as exc:
        return ScenarioSpec(name, path, source, problem=f"cannot be read: {exc}")
    doc = ast.get_docstring(tree) or ""
    params: dict[str, str | None] = {}
    problem = ""
    has_main = False
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            has_main = True
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
            try:
                raw = ast.literal_eval(node.value)
            except ValueError:
                problem = "PARAMS must be a literal mapping of names to defaults"
                continue
            if not isinstance(raw, dict) or not all(isinstance(k, str) for k in raw):
                problem = "PARAMS must map parameter names to defaults"
                continue
            params = {k: (None if v is None else str(v)) for k, v in raw.items()}
    if not has_main:
        problem = problem or "no `async def main(sc)`"
    summary = doc.strip().splitlines()[0] if doc.strip() else ""
    return ScenarioSpec(name, path, source, summary, doc, params, problem)


def scenario_dirs(workspace: Path | None) -> list[tuple[str, Path]]:
    dirs = []
    if workspace is not None:
        dirs.append(("workspace", workspace / ".raven" / "scenarios"))
    dirs.append(("home", default_home_dir()))
    dirs.append(("builtin", BUILTIN_DIR))
    return dirs


def discover(workspace: Path | None = None) -> dict[str, ScenarioSpec]:
    """Every scenario by name; where two places hold one name, the more specific wins."""
    found: dict[str, ScenarioSpec] = {}
    for source, directory in scenario_dirs(workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            spec = read_spec(path, source)
            found.setdefault(spec.name, spec)
    return dict(sorted(found.items()))


def bind_args(spec: ScenarioSpec, given: dict[str, str]) -> dict[str, str]:
    """The run's arguments: defaults filled in; an unknown or a missing one is a ValueError naming them."""
    unknown = sorted(set(given) - set(spec.params))
    if unknown:
        known = ", ".join(spec.params) or "none"
        raise ValueError(f"{spec.name} takes no {', '.join(unknown)}; its parameters: {known}")
    args = {k: v for k, v in spec.params.items() if v is not None}
    args.update(given)
    missing = [k for k, v in spec.params.items() if v is None and k not in args]
    if missing:
        raise ValueError(f"{spec.name} needs {', '.join(missing)} (--arg {missing[0]}=...)")
    return args


__all__ = [
    "BUILTIN_DIR",
    "ScenarioSpec",
    "bind_args",
    "default_home_dir",
    "discover",
    "name_of",
    "read_spec",
    "scenario_dirs",
]

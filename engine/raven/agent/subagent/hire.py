"""Hiring an employee: an agent of one's own with a name, a role, a model and its own instructions.

One step writes a folder agent under ``<RARUS_HOME>/employees/<id>`` -- the
same shape ``raven agents new`` scaffolds, without the plugin harness -- so the
roster discovers it like any product and the main agent can delegate to it:

- ``subagent.json``: the name, the role the dispatching model reads to decide
  when to pick it (``description``), what it owns, and its model;
- ``soul.md``: its system prompt, seeded into its own agent home on first
  launch, where its instructions live from then on;
- ``config.json`` and ``run.py``: its model on the host's keys, or on a key of
  its own kept in ``.env`` (never in a file that is shown or published);
- ``employee.json``: what the page lists about it.

Each employee runs as its own Raven over ACP, with its own home and therefore
its own sessions, starting from none.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from raven.agent.subagent.vendored_agents import discover_product_rows, employees_root, env_prefix

MAX_NAME = 48
MAX_ROLE = 2000
MAX_PROMPT = 60000


class HireError(ValueError):
    """A hire that cannot be made, in words a person can act on."""


@dataclass
class Hire:
    name: str
    role: str
    system_prompt: str
    owns: str = ""
    provider: str = ""
    """A provider of the host's config, or the one the own ``api_key`` is for; empty follows the host."""
    model: str = ""
    api_key: str = ""
    api_base: str = ""


def folder_id(name: str) -> str:
    """``anna-k`` for ``Anna K.``; latin letters, digits and dashes, starting with a letter."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"employee-{slug}" if slug else "employee"
    return slug[:40].rstrip("-")


def _known_providers() -> set[str]:
    from raven.config.schema import Config

    return set(type(Config().providers).model_fields)


def check(spec: Hire) -> None:
    name = (spec.name or "").strip()
    if not name or len(name) > MAX_NAME:
        raise HireError(f"an employee's name is 1 to {MAX_NAME} characters")
    if not re.search(r"[A-Za-z0-9]", name):
        raise HireError("an employee's name needs at least one latin letter or digit, for its folder")
    if not (spec.role or "").strip():
        raise HireError("say what the employee does: the main agent reads it to decide when to hand work over")
    if len(spec.role) > MAX_ROLE:
        raise HireError(f"the role is at most {MAX_ROLE} characters")
    if not (spec.system_prompt or "").strip():
        raise HireError("give the employee a system prompt: who it is and how it works")
    if len(spec.system_prompt) > MAX_PROMPT:
        raise HireError(f"the system prompt is at most {MAX_PROMPT} characters")
    if spec.provider and spec.provider not in _known_providers():
        raise HireError(f"unknown provider {spec.provider!r}")
    if spec.api_key and not (spec.provider and spec.model):
        raise HireError("a key of the employee's own needs the provider it is for and the model to use")
    if spec.api_base and not spec.api_key:
        raise HireError("an endpoint address goes with a key of the employee's own")


def _taken_names() -> set[str]:
    from raven.agent.subagent.builtin_agents import BUILTIN_AGENT_NAMES, LEGACY_AGENT_ALIASES

    names = {n.casefold() for n in BUILTIN_AGENT_NAMES} | {a.casefold() for a in LEGACY_AGENT_ALIASES}
    for row in discover_product_rows():
        names.add(str(row.name).casefold())
    try:
        from raven.config.update_subagents import get_agents

        names |= {str(r.get("name") or "").casefold() for r in get_agents() if isinstance(r, dict)}
    except Exception:  # noqa: BLE001 - an unreadable roster refuses nothing here; the folders still decide
        pass
    return names


_RUN_PY = '''#!/usr/bin/env python
"""Launcher of the employee {display}, written by ``raven agents hire``.

Renders its config -- its own key from ``.env`` when it has one, else the host's
keys with its own model -- seeds its system prompt into its agent home on the
first launch, and execs ``raven acp``. stdout belongs to the protocol.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from raven.config import product_render as render

HERE = Path(__file__).resolve().parent
AGENT = {agent!r}
PREFIX = {prefix!r}
PROVIDER = {provider!r}
MODEL = {model!r}


def env_value(name: str) -> str | None:
    return render.env_value(name, env_file=HERE / ".env")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def render_config() -> Path:
    config = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    host = render.host_config()
    defaults = config.setdefault("agents", {{}}).setdefault("defaults", {{}})
    key = env_value(PREFIX + "_API_KEY")
    if key and PROVIDER:
        render.put(config, ("providers", PROVIDER, "apiKey"), key)
        base = env_value(PREFIX + "_API_BASE")
        if base:
            render.put(config, ("providers", PROVIDER, "apiBase"), base)
        defaults["provider"], defaults["model"] = PROVIDER, MODEL
        log(f"[run] llm: own key (provider={{PROVIDER}} model={{MODEL}})")
    else:
        taken = render.inherit_llm(config, host)
        if not taken:
            raise SystemExit(
                f"error: {{PREFIX}}_API_KEY is not set and the host's model cannot be inherited "
                f"({{render.inherit_refusal(config, host)}}); configure a provider in the host raven, "
                f"or put a key in {{HERE / '.env'}}"
            )
        if MODEL:
            defaults["model"] = MODEL
            if PROVIDER:
                defaults["provider"] = PROVIDER
        log(f"[run] llm: the host's keys ({{taken}}); model {{defaults.get('model')}}")
    workspace = render.product_acp_home(AGENT, override=env_value(PREFIX + "_ACP_HOME"))
    defaults["workspace"] = str(workspace)
    render.seed_once(
        workspace / "agent_memory" / "profile" / "soul.md",
        lambda: (HERE / "soul.md").read_text(encoding="utf-8"),
    )
    root = render.product_state_root(AGENT, override=env_value(PREFIX + "_STATE_ROOT"))
    root.mkdir(parents=True, exist_ok=True)
    render.sweep_stale_renders(root)
    return render.write_rendered(config, root)


def main() -> int:
    rendered = render_config()
    log(f"[run] exec {{sys.executable}} -m raven acp (config {{rendered}})")
    # Windows has no exec: os.execv there ends this process, which the client reads as the
    # agent gone, so the library serves from a child on the same stdio (exec_raven_acp).
    exec_acp = getattr(render, "exec_raven_acp", None)
    if exec_acp is not None:
        exec_acp(rendered)
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
'''


def hire(spec: Hire, *, root: Path | None = None) -> Path:
    """Write the employee's folder; its path. Refused for a name already on the roster or a folder that exists."""
    check(spec)
    display = spec.name.strip()
    agent = folder_id(display)
    if display.casefold() in _taken_names() or agent.casefold() in _taken_names():
        raise HireError(f"an agent named {display!r} is already on the roster; choose another name")
    root = root or employees_root()
    target = root / agent
    if target.exists():
        raise HireError(f"an employee folder {agent!r} already exists; choose another name")
    prefix = "EMP_" + env_prefix(agent)
    manifest: dict[str, Any] = {
        "name": display,
        "kind": "acp",
        "description": spec.role.strip(),
        "owns": spec.owns.strip() or None,
        "enabled": True,
        "command": "{PYTHON} {SUBAGENT_DIR}/run.py --acp",
        "cwd": "{SUBAGENT_DIR}",
        "readyTimeoutMs": 120000,
        "timeout": None,
        "maxOutputChars": 60000,
        "memory": {"userId": agent, "agentId": agent},
    }
    if spec.model:
        manifest["model"] = spec.model.strip()
    config: dict[str, Any] = {}
    if spec.api_key:
        config = {"agents": {"defaults": {"provider": spec.provider, "model": spec.model.strip()}}}
    record = {
        "hiredAt": time.time(),
        "provider": spec.provider,
        "model": spec.model.strip(),
        "ownKey": bool(spec.api_key),
        "keyVariable": f"{prefix}_API_KEY",
    }
    files = {
        "subagent.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        "config.json": json.dumps(config, indent=2),
        "run.py": _RUN_PY.format(
            display=display, agent=agent, prefix=prefix, provider=spec.provider, model=spec.model.strip()
        ),
        "soul.md": spec.system_prompt.strip() + "\n",
        "employee.json": json.dumps(record, indent=2),
    }
    if spec.api_key:
        lines = [f"{prefix}_API_KEY={spec.api_key.strip()}"]
        if spec.api_base:
            lines.append(f"{prefix}_API_BASE={spec.api_base.strip()}")
        files[".env"] = "\n".join(lines) + "\n"
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{agent}.partial-", dir=root))
    try:
        for name, text in files.items():
            (staging / name).write_text(text, encoding="utf-8")
        if (staging / ".env").exists():
            (staging / ".env").chmod(0o600)
        staging.chmod(0o755)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


@dataclass
class Employee:
    name: str
    folder: str
    role: str
    owns: str
    provider: str
    model: str
    own_key: bool
    hired_at: float

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


def employees(root: Path | None = None) -> list[Employee]:
    """Everyone hired, name-sorted."""
    root = root or employees_root()
    found: list[Employee] = []
    if not root.is_dir():
        return found
    for manifest in sorted(root.glob("*/subagent.json")):
        folder = manifest.parent
        if folder.name.startswith("."):
            continue
        try:
            row = json.loads(manifest.read_text(encoding="utf-8"))
            record = json.loads((folder / "employee.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found.append(
            Employee(
                name=str(row.get("name") or folder.name),
                folder=folder.name,
                role=str(row.get("description") or ""),
                owns=str(row.get("owns") or ""),
                provider=str(record.get("provider") or ""),
                model=str(row.get("model") or record.get("model") or ""),
                own_key=bool(record.get("ownKey")),
                hired_at=float(record.get("hiredAt") or 0),
            )
        )
    return sorted(found, key=lambda e: e.name.casefold())


def _folder_of(name: str, root: Path | None) -> Path:
    root = root or employees_root()
    for employee in employees(root):
        if employee.name.casefold() == name.casefold() or employee.folder == name:
            return root / employee.folder
    raise HireError(f"no employee named {name!r}")


def system_prompt(name: str, *, root: Path | None = None) -> str:
    """The employee's instructions as it reads them: its agent home's copy once it has run, else the folder's."""
    from raven.config import product_render as render

    folder = _folder_of(name, root)
    live = render.product_acp_home(folder.name) / "agent_memory" / "profile" / "soul.md"
    source = live if live.is_file() else folder / "soul.md"
    return source.read_text(encoding="utf-8")


def set_system_prompt(name: str, prompt: str, *, root: Path | None = None, author: str = "user") -> None:
    """Replace the employee's system prompt, in its folder and, once it has run, in its agent home (with history)."""
    from raven.config import product_render as render

    if not (prompt or "").strip() or len(prompt) > MAX_PROMPT:
        raise HireError(f"a system prompt is 1 to {MAX_PROMPT} characters")
    folder = _folder_of(name, root)
    (folder / "soul.md").write_text(prompt.strip() + "\n", encoding="utf-8")
    home = render.product_acp_home(folder.name)
    if (home / "agent_memory" / "profile" / "soul.md").is_file():
        from raven.agent.instructions import InstructionHistory

        InstructionHistory(home).save(
            "agent_memory/profile/soul.md", prompt.strip() + "\n", author=author, note="set from the employee's card"
        )


def retire(name: str, *, root: Path | None = None) -> Path:
    """Take an employee off the roster; the folder is kept under ``.retired`` rather than deleted."""
    root = root or employees_root()
    folder = _folder_of(name, root)
    retired = root / ".retired" / f"{folder.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    retired.parent.mkdir(parents=True, exist_ok=True)
    folder.rename(retired)
    return retired


__all__ = [
    "Employee",
    "Hire",
    "HireError",
    "check",
    "employees",
    "folder_id",
    "hire",
    "retire",
    "set_system_prompt",
    "system_prompt",
]

"""Getting a fresh checkout ready to run beside others: its own ports and the files git leaves out.

Several checkouts of one project run at once -- a stint's tree, best-of
attempts, a multi-repository run -- and two of them starting the same dev server
on the same port collide. Each checkout is handed ports of its own from one
registry under the raven home (free when handed out, never handed to two
checkouts at once), written to ``.raven-env`` in the checkout as ``PORT``,
``PORT_1``... A fresh checkout also lacks every file git ignores, which is where
local configuration and caches live (``.env``, a build cache); the ones the
project lists in ``.worktreeinclude`` (one glob per line, ``#`` for comments)
are copied in from the project. Releasing a checkout gives its ports back.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from raven.utils.atomic_io import atomic_update

INCLUDE_FILE = ".worktreeinclude"
ENV_FILE = ".raven-env"
PORT_RANGE = (20000, 29999)


@dataclass
class PreparedEnv:
    ports: list[int] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_ports(registry: Path, owner: str, count: int, *, port_range: tuple[int, int] = PORT_RANGE) -> list[int]:
    """``count`` ports for ``owner``: free now and held by no other owner in ``registry``."""
    low, high = port_range

    def update(raw: str | None) -> tuple[str, list[int]]:
        held: dict[str, list[int]] = json.loads(raw) if raw else {}
        taken = {p for name, ports in held.items() if name != owner for p in ports}
        mine = [p for p in held.get(owner, []) if p not in taken]
        candidate = low
        while len(mine) < count:
            if candidate > high:
                raise RuntimeError(f"no free port left in {low}-{high}")
            if candidate not in taken and candidate not in mine and _free(candidate):
                mine.append(candidate)
            candidate += 1
        held[owner] = mine[:count]
        return json.dumps(held, indent=2), held[owner]

    registry.parent.mkdir(parents=True, exist_ok=True)
    return atomic_update(registry, update)


def release_ports(registry: Path, owner: str) -> None:
    def update(raw: str | None) -> tuple[str, None]:
        held: dict[str, list[int]] = json.loads(raw) if raw else {}
        held.pop(owner, None)
        return json.dumps(held, indent=2), None

    if registry.is_file():
        atomic_update(registry, update)


def _include_patterns(project: Path) -> list[str]:
    path = project / INCLUDE_FILE
    if not path.is_file():
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _ignored_files(project: Path) -> list[str]:
    done = subprocess.run(  # noqa: S603
        [  # noqa: S607 -- git location varies; PATH lookup intended
            "git",
            "-c",
            "core.quotePath=false",
            "-C",
            str(project),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
        ],
        capture_output=True,
        text=True,
    )
    return [line for line in done.stdout.splitlines() if line] if done.returncode == 0 else []


def copy_ignored(project: Path, checkout: Path) -> list[str]:
    """Copy the git-ignored files ``.worktreeinclude`` names from the project into the checkout."""
    patterns = _include_patterns(project)
    if not patterns:
        return []
    copied: list[str] = []
    for rel in _ignored_files(project):
        if not any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(Path(rel).name, pattern) for pattern in patterns):
            continue
        target = checkout / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project / rel, target)
        copied.append(rel)
    return copied


def _exclude_env_file(checkout: Path) -> None:
    """Keep ``.raven-env`` out of every commit: listed in the repository's own exclude file, which no one pushes."""
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(checkout), "rev-parse", "--git-common-dir"],  # noqa: S607 -- PATH lookup intended
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        return
    common = Path(done.stdout.strip())
    if not common.is_absolute():
        common = Path(checkout) / common
    exclude = common / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if ENV_FILE not in existing.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + separator + ENV_FILE + "\n", encoding="utf-8")


def prepare_checkout(project: Path, checkout: Path, *, owner: str, registry: Path, ports: int = 1) -> PreparedEnv:
    """Ports of its own and the ignored files it needs, for a checkout just made from ``project``."""
    env = PreparedEnv(ports=allocate_ports(registry, owner, ports) if ports else [])
    env.copied = copy_ignored(Path(project), Path(checkout))
    if env.ports:
        _exclude_env_file(Path(checkout))
        lines = [f"PORT={env.ports[0]}", *(f"PORT_{i}={p}" for i, p in enumerate(env.ports[1:], 1))]
        (Path(checkout) / ENV_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env


def default_registry() -> Path:
    from raven.home import raven_home

    return raven_home() / "workspaces" / "ports.json"


__all__ = [
    "ENV_FILE",
    "INCLUDE_FILE",
    "PreparedEnv",
    "allocate_ports",
    "copy_ignored",
    "default_registry",
    "prepare_checkout",
    "release_ports",
]

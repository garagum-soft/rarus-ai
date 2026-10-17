"""``rarus migrate-raven``: bring a Raven home (``RAVEN_HOME`` or ``~/.raven``) into this one.

Sessions, settings, skills, memory and knowledge live in files under the home, so the move is a
copy. It never overwrites a file that already exists here, never touches the source, and leaves
behind what is machine- or session-bound or can be rebuilt: caches, logs, sandboxes, browser
profiles, lock and socket files, the running gateway's ``serve.json``, and sign-in tokens
(a Raven sign-in is not a Rarus sign-in).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import typer

from raven.home import raven_home

SKIP_DIRS = frozenset({"cache", "logs", "sandbox", "render", "browser-profile", "oauth", "bridge", "telemetry"})
SKIP_FILES = frozenset({"serve.json", "web.log", "gateway.lock", "app.lock"})
SKIP_SUFFIXES = (".lock", ".sock", ".pid", ".tmp", ".part")


@dataclass
class MigrationReport:
    copied: int = 0
    kept: int = 0  # already present here, left as it was
    skipped: int = 0  # not meant to move
    bytes: int = 0
    by_top: dict[str, int] = field(default_factory=dict)


def default_source() -> Path:
    override = os.environ.get("RAVEN_HOME", "").strip()
    return Path(override) if override else Path.home() / ".raven"


def _skipped(rel: Path) -> bool:
    if rel.parts and rel.parts[0] in SKIP_DIRS:
        return True
    return rel.name in SKIP_FILES or rel.name.endswith(SKIP_SUFFIXES)


def migrate(source: Path, target: Path, *, dry_run: bool = False) -> MigrationReport:
    """Copy what moves from ``source`` to ``target``; ``dry_run`` only counts."""
    report = MigrationReport()
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError("the source and the target are the same folder")
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(source)
        if _skipped(rel):
            report.skipped += 1
            continue
        destination = target / rel
        if destination.exists():
            report.kept += 1
            continue
        report.copied += 1
        report.bytes += path.stat().st_size
        top = rel.parts[0] if len(rel.parts) > 1 else "(files)"
        report.by_top[top] = report.by_top.get(top, 0) + 1
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    return report


def migrate_raven(
    source: Path = typer.Option(None, "--from", help="Raven home to read (default: RAVEN_HOME or ~/.raven)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be copied and copy nothing."),
) -> None:
    """Copy sessions, settings, skills, memory and knowledge from a Raven home into this one."""
    src = source or default_source()
    dst = raven_home()
    try:
        report = migrate(src, dst, dry_run=dry_run)
    except FileNotFoundError:
        typer.echo(f"No Raven home found at {src}.", err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    verb = "Would copy" if dry_run else "Copied"
    typer.echo(f"{verb} {report.copied} files ({report.bytes / 1_048_576:.1f} MB) from {src} to {dst}.")
    typer.echo(f"Left as they were: {report.kept} files already here. Not moved: {report.skipped}.")
    for name, count in sorted(report.by_top.items()):
        typer.echo(f"  {name}: {count}")
    typer.echo("Sign-ins are not copied; run `rarus provider login rarus` to sign in.")

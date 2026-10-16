"""Verifiable receipts for graph runs: what a run produced, hashed, and checkable afterwards.

When a run finishes, its receipt lists every artifact it left -- the run's
``graph.json`` and ``manifest.json``, and each node's rendered prompt and output
-- with the SHA-256 of each, and a root hash over that list and the run id. The
receipt is written beside the run as ``receipt.json`` and its root is quoted in
the run's result, which is what publishing it means here: a reader holding the
root can recompute everything from the files and see whether they are the ones
the run reported. Any edit, removal or substitution of a covered file, or of the
receipt's own list, makes verification fail and names what changed.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from raven.utils.atomic_io import atomic_replace

RECEIPT_FILE = "receipt.json"
RECEIPT_VERSION = 1
NODE_FILES = ("{node}.prompt.md", "{node}.out.md")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Entry:
    path: str
    sha256: str
    size: int


@dataclass
class Receipt:
    run_id: str
    entries: list[Entry]
    root: str
    version: int = RECEIPT_VERSION
    issued_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    ok: bool
    root: str
    problems: list[str] = field(default_factory=list)


def _root_of(run_id: str, entries: list[Entry]) -> str:
    return _sha(_canonical({"run_id": run_id, "version": RECEIPT_VERSION, "entries": [asdict(e) for e in entries]}))


def _covered(run_root: Path, nodes_root: Path) -> list[tuple[str, Path]]:
    """Every artifact a receipt covers, as (receipt path, file), in a fixed order."""
    files: list[tuple[str, Path]] = []
    for name in ("graph.json", "manifest.json"):
        if (run_root / name).is_file():
            files.append((f"run/{name}", run_root / name))
    try:
        graph = json.loads((run_root / "graph.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        graph = {}
    node_ids = sorted(
        str(node["id"])
        for node in graph.get("nodes") or []
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    )
    for node in node_ids:
        for pattern in NODE_FILES:
            name = pattern.format(node=node)
            if (nodes_root / name).is_file():
                files.append((f"nodes/{name}", nodes_root / name))
    return files


def issue_receipt(run_root: Path, nodes_root: Path, run_id: str) -> Receipt:
    """Hash what the run left and write ``receipt.json`` beside it."""
    entries = []
    for label, path in _covered(Path(run_root), Path(nodes_root)):
        data = path.read_bytes()
        entries.append(Entry(path=label, sha256=_sha(data), size=len(data)))
    receipt = Receipt(run_id=run_id, entries=entries, root=_root_of(run_id, entries))
    atomic_replace(Path(run_root) / RECEIPT_FILE, json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False))
    return receipt


def load_receipt(run_root: Path) -> Receipt:
    data = json.loads((Path(run_root) / RECEIPT_FILE).read_text(encoding="utf-8"))
    data["entries"] = [Entry(**e) for e in data["entries"]]
    return Receipt(**data)


def verify_receipt(run_root: Path, nodes_root: Path, *, expected_root: str | None = None) -> Verdict:
    """Recompute every hash; ``expected_root`` is the published root to hold the receipt to."""
    try:
        receipt = load_receipt(run_root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Verdict(ok=False, root="", problems=[f"no readable receipt: {exc}"])
    problems: list[str] = []
    recomputed = _root_of(receipt.run_id, receipt.entries)
    if recomputed != receipt.root:
        problems.append("the receipt's own list was altered (its root does not match its entries)")
    if expected_root and expected_root != receipt.root:
        problems.append(f"the receipt's root {receipt.root} is not the published {expected_root}")
    covered = {label: path for label, path in _covered(Path(run_root), Path(nodes_root))}
    for entry in receipt.entries:
        path = covered.get(entry.path)
        if path is None:
            problems.append(f"{entry.path} is missing")
            continue
        if _sha(path.read_bytes()) != entry.sha256:
            problems.append(f"{entry.path} changed since the receipt was issued")
    for label in sorted(set(covered) - {e.path for e in receipt.entries}):
        problems.append(f"{label} is not on the receipt")
    return Verdict(ok=not problems, root=receipt.root, problems=problems)


__all__ = ["RECEIPT_FILE", "Entry", "Receipt", "Verdict", "issue_receipt", "load_receipt", "verify_receipt"]

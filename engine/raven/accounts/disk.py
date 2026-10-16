"""How much disk an account's engine home takes, against its allowance."""

from __future__ import annotations

import os
from pathlib import Path

GB = 1024**3


def tree_size(root: Path) -> int:
    """Bytes under ``root``, links not followed; unreadable entries count as nothing."""
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


__all__ = ["GB", "tree_size"]

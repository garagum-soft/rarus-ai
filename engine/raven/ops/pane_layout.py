"""Laying agents out over terminal windows and panes.

A window keeps its first pane, the main one (the orchestrator, or the person),
on the left at a fixed share of the width; agents tile the rest as a grid. How
many fit is the window's capacity: as many columns and rows as leave every pane
at least its minimum size, capped so a pane stays readable. The grid for N agents
is the smallest one that holds them, fewer rows first, filled column by column;
a column with fewer agents gives each of them more height. Agents past a
window's capacity go to the next window. Adding or removing an agent re-plans
the window it is in, which is what rebalances the panes.

The plan is pure arithmetic. ``tmux_layout`` turns one window's plan into the
layout string ``tmux select-layout`` takes, checksum included, so the panes land
exactly where the plan put them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

DIVIDER = 1
MIN_PANE_WIDTH = 40
MIN_PANE_HEIGHT = 8
MAX_GRID = 4
MAIN_SHARE = 0.5


@dataclass(frozen=True)
class Capacity:
    cols: int
    rows: int

    @property
    def total(self) -> int:
        return self.cols * self.rows


@dataclass(frozen=True)
class Grid:
    cols: int
    rows: int
    #: Agents per column, left to right; the last columns may hold fewer.
    per_col: tuple[int, ...]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


def main_width(width: int, share: float = MAIN_SHARE) -> int:
    return max(1, int(width * share))


def capacity(
    width: int,
    height: int,
    *,
    share: float = MAIN_SHARE,
    min_w: int = MIN_PANE_WIDTH,
    min_h: int = MIN_PANE_HEIGHT,
    max_grid: int = MAX_GRID,
) -> Capacity:
    """How many agent panes fit beside the main pane: columns by rows."""
    area = max(0, width - main_width(width, share) - DIVIDER)
    cols = min(max_grid, max(0, (area + DIVIDER) // (min_w + DIVIDER)))
    rows = min(max_grid, max(0, (height + DIVIDER) // (min_h + DIVIDER)))
    return Capacity(cols, rows)


def grid(count: int, cap: Capacity) -> Grid:
    """The smallest grid within ``cap`` that holds ``count`` agents, fewer rows first."""
    if count <= 0:
        return Grid(0, 0, ())
    if count > cap.total:
        raise ValueError(f"{count} agents do not fit a window that holds {cap.total}")
    best: tuple[int, int, int] | None = None
    for rows in range(1, cap.rows + 1):
        for cols in range(1, cap.cols + 1):
            if cols * rows < count:
                continue
            key = (cols * rows, rows, cols)
            if best is None or key < best:
                best = key
    if best is None:  # unreachable: count <= cap.total leaves the full grid
        raise ValueError(f"no grid holds {count} agents")
    _, rows, cols = best
    per_col = [count // cols + (1 if i < count % cols else 0) for i in range(cols)]
    return Grid(cols, rows, tuple(n for n in per_col if n))


def split(names: Sequence[str], cap: Capacity) -> list[list[str]]:
    """Agents in order, cut into windows of at most the capacity each."""
    size = max(1, cap.total)
    return [list(names[i : i + size]) for i in range(0, len(names), size)] or [[]]


def _spans(total: int, parts: int) -> list[tuple[int, int]]:
    """``parts`` (offset, size) spans covering ``total`` with a divider between each."""
    room = total - DIVIDER * (parts - 1)
    base, extra = divmod(room, parts)
    spans, at = [], 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        spans.append((at, size))
        at += size + DIVIDER
    return spans


def rects(width: int, height: int, count: int, cap: Capacity, *, share: float = MAIN_SHARE) -> tuple[Rect, list[Rect]]:
    """Where the main pane and each agent pane go, agents in column order."""
    if count == 0:
        return Rect(0, 0, width, height), []
    main_w = main_width(width, share)
    main = Rect(0, 0, main_w, height)
    plan = grid(count, cap)
    x0 = main_w + DIVIDER
    out = []
    for (cx, cw), n in zip(_spans(width - x0, len(plan.per_col)), plan.per_col, strict=True):
        for ry, rh in _spans(height, n):
            out.append(Rect(x0 + cx, ry, cw, rh))
    return main, out


def checksum(body: str) -> str:
    """tmux's layout checksum (layout-custom.c)."""
    csum = 0
    for ch in body:
        csum = (csum >> 1) + ((csum & 1) << 15)
        csum = (csum + ord(ch)) & 0xFFFF
    return f"{csum:04x}"


def _leaf(r: Rect, pane: int) -> str:
    return f"{r.w}x{r.h},{r.x},{r.y},{pane}"


def tmux_layout(
    width: int, height: int, main_pane: int, agent_panes: Sequence[int], cap: Capacity, *, share: float = MAIN_SHARE
) -> str:
    """The ``select-layout`` string that puts the main pane left and the agents in their grid."""
    main, cells = rects(width, height, len(agent_panes), cap, share=share)
    if not agent_panes:
        body = _leaf(main, main_pane)
        return f"{checksum(body)},{body}"
    plan = grid(len(agent_panes), cap)
    columns, i = [], 0
    for n in plan.per_col:
        col_cells = cells[i : i + n]
        panes = agent_panes[i : i + n]
        i += n
        top = col_cells[0]
        if n == 1:
            columns.append(_leaf(top, panes[0]))
            continue
        inner = ",".join(_leaf(c, p) for c, p in zip(col_cells, panes, strict=True))
        columns.append(f"{top.w}x{height},{top.x},0[{inner}]")
    # One left-to-right split at the top: the main pane, then each column.
    body = f"{width}x{height},0,0{{{_leaf(main, main_pane)},{','.join(columns)}}}"
    return f"{checksum(body)},{body}"


__all__ = ["Capacity", "Grid", "Rect", "capacity", "checksum", "grid", "rects", "split", "tmux_layout"]

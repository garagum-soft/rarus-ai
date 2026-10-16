"""Agents laid out over tmux windows and panes, rebalanced as agents are added and removed."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from raven.ops import pane_layout
from raven.ops.pane_board import BoardError, PaneBoard
from raven.ops.pane_layout import Capacity, capacity, checksum, grid, rects, split, tmux_layout

_LEAF = re.compile(r"(\d+)x(\d+),(\d+),(\d+),(\d+)(?=[,\]\}]|$)")


def leaves(layout: str) -> dict[int, tuple[int, int, int, int]]:
    """pane number -> (w, h, x, y), after checking the checksum covers the body."""
    csum, body = layout.split(",", 1)
    assert checksum(body) == csum
    return {int(m[5]): (int(m[1]), int(m[2]), int(m[3]), int(m[4])) for m in _LEAF.finditer(body)}


# -- the plan -------------------------------------------------------------------


def test_the_checksum_is_tmuxs_own() -> None:
    # The example layout in tmux(1).
    assert checksum("159x48,0,0{79x48,0,0,79x48,80,0}") == "bb62"


def test_capacity_follows_the_window_size() -> None:
    assert capacity(240, 60) == Capacity(cols=2, rows=4)
    assert capacity(120, 30) == Capacity(cols=1, rows=3), "59 columns beside the main pane fit one 40-wide pane"
    assert capacity(80, 30).cols == 0, "39 columns fit none"
    assert capacity(400, 120).total == 16, "capped at a 4x4 grid"


def test_n_agents_get_the_smallest_grid_filled_column_by_column() -> None:
    cap = Capacity(cols=2, rows=4)
    assert grid(1, cap).per_col == (1,)
    assert grid(2, cap).per_col == (1, 1)
    assert grid(3, cap).per_col == (3,)
    assert grid(5, cap).per_col == (3, 2)
    with pytest.raises(ValueError):
        grid(9, cap)


def test_agents_past_a_windows_capacity_go_to_the_next_window() -> None:
    assert split([f"a{i}" for i in range(10)], Capacity(2, 4)) == [[f"a{i}" for i in range(8)], ["a8", "a9"]]


def test_the_panes_cover_the_agent_area_without_overlap() -> None:
    main, cells = rects(240, 60, 5, capacity(240, 60))

    assert main == pane_layout.Rect(0, 0, 120, 60)
    assert {c.x for c in cells} == {121, 181}
    for x in {c.x for c in cells}:
        column = sorted((c for c in cells if c.x == x), key=lambda c: c.y)
        assert sum(c.h for c in column) + len(column) - 1 == 60
    assert all(c.x + c.w <= 240 for c in cells)


def test_the_layout_string_places_every_pane_in_its_slot() -> None:
    layout = tmux_layout(240, 60, 0, [4, 5, 6], capacity(240, 60))
    placed = leaves(layout)

    assert set(placed) == {0, 4, 5, 6}
    assert placed[0] == (120, 60, 0, 0)
    assert [placed[p][3] for p in (4, 5, 6)] == [0, 21, 41]


# -- the board over a simulated tmux --------------------------------------------


class FakeTmux:
    """Enough of tmux for the board: sessions, windows with a size, panes, layouts."""

    def __init__(self, width: int = 240, height: int = 60) -> None:
        self.size = (width, height)
        self.windows: dict[str, list[str]] = {}
        self.session: str | None = None
        self.layouts: dict[str, str] = {}
        self.typed: list[tuple[str, str]] = []
        self._ids = iter(range(100))

    def _pane(self) -> str:
        return f"%{next(self._ids)}"

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        cmd, *rest = args
        if cmd == "has-session":
            return (0 if self.session else 1), ""
        if cmd in {"new-session", "new-window"}:
            if cmd == "new-session":
                self.session = rest[rest.index("-s") + 1]
            window, pane = f"@{len(self.windows)}", self._pane()
            self.windows[window] = [pane]
            return 0, f"{window} {pane}\n"
        if cmd == "split-window":
            target = rest[rest.index("-t") + 1]
            window = next(w for w, panes in self.windows.items() if target in panes)
            pane = self._pane()
            self.windows[window].append(pane)
            return 0, pane + "\n"
        if cmd == "display-message":
            fmt = rest[-1]
            if fmt == "#{pane_active}":
                return 0, "0\n"
            return 0, f"{self.size[0]} {self.size[1]}\n"
        if cmd == "list-panes":
            return 0, "\n".join(p for panes in self.windows.values() for p in panes) + "\n"
        if cmd == "select-layout":
            window, layout = rest[rest.index("-t") + 1], rest[-1]
            assert {f"%{n}" for n in leaves(layout)} == set(self.windows[window]), (
                "every pane of the window, and no other"
            )
            self.layouts[window] = layout
            return 0, ""
        if cmd == "kill-pane":
            pane = rest[-1]
            for panes in self.windows.values():
                if pane in panes:
                    panes.remove(pane)
            return 0, ""
        if cmd == "capture-pane":
            return 0, "$ \n"
        if cmd == "send-keys":
            if "-l" in rest:
                self.typed.append((rest[rest.index("-t") + 1], rest[-1]))
            return 0, ""
        raise AssertionError(f"unexpected tmux {args}")


def _board(tmp_path: Path, tmux: FakeTmux) -> PaneBoard:
    return PaneBoard(tmp_path / "board.json", runner=tmux)


def test_n_agents_are_laid_out_over_panes(tmp_path: Path) -> None:
    tmux = FakeTmux()
    board = _board(tmp_path, tmux)

    placed = board.place(["a", "b", "c"], command="agent --name {name}")

    assert [p[1] for p in placed] == ["@0", "@0", "@0"]
    sizes = leaves(tmux.layouts["@0"])
    agent_panes = [sizes[int(p[2][1:])] for p in placed]
    assert all(w == 119 for w, *_ in agent_panes) and [h for _, h, *_ in agent_panes] == [20, 19, 19]


def test_adding_an_agent_rebalances_the_panes(tmp_path: Path) -> None:
    tmux = FakeTmux()
    board = _board(tmp_path, tmux)
    board.place(["a", "b", "c"])
    before = leaves(tmux.layouts["@0"])

    board.place(["d"])

    after = leaves(tmux.layouts["@0"])
    pane_a = int(board.pane_of("a")[1:])
    assert before[pane_a][:2] == (119, 20) and after[pane_a][:2] == (59, 30), "one column of three became two of two"
    assert len(after) == 5


def test_a_full_window_spills_to_a_new_one_and_removal_rebalances(tmp_path: Path) -> None:
    tmux = FakeTmux()
    board = _board(tmp_path, tmux)
    board.place([f"a{i}" for i in range(8)])

    placed = board.place(["late"])
    board.remove("a0")

    assert placed[0][1] == "@1" and len(tmux.windows) == 2
    assert len(leaves(tmux.layouts["@0"])) == 8, "main plus the seven left"
    assert [w.id for w in board.windows()] == ["@0", "@1"]


def test_a_pane_closed_outside_the_board_is_dropped(tmp_path: Path) -> None:
    tmux = FakeTmux()
    board = _board(tmp_path, tmux)
    board.place(["a", "b"])
    tmux.windows["@0"].remove(board.pane_of("a"))

    board.rebalance()

    assert [s.name for s in board.windows()[0].agents] == ["b"]


def test_text_is_typed_into_an_agents_pane_when_it_waits(tmp_path: Path) -> None:
    tmux = FakeTmux()
    board = _board(tmp_path, tmux)
    board.place(["a"])

    assert board.type("a", "run the tests") == "delivered"
    assert tmux.typed == [(board.pane_of("a"), "run the tests")]


def test_the_same_agent_is_not_placed_twice_and_a_tiny_terminal_is_refused(tmp_path: Path) -> None:
    board = _board(tmp_path, FakeTmux())
    board.place(["a"])
    with pytest.raises(BoardError, match="already has a pane"):
        board.place(["a"])
    with pytest.raises(BoardError, match="too small"):
        _board(tmp_path / "small", FakeTmux(width=80, height=20)).place(["x"])


def test_the_command_line_places_and_shows_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from raven.cli import ops_connection_commands
    from raven.cli.commands import app

    tmux = FakeTmux()
    monkeypatch.setattr(ops_connection_commands, "_board", lambda session="raven": _board(tmp_path, tmux))
    runner = CliRunner()

    placed = runner.invoke(app, ["ops", "pane", "place", "a", "b"])
    shown = runner.invoke(app, ["ops", "pane", "board"])
    typed = runner.invoke(app, ["ops", "pane", "type", "b", "hi"])

    assert placed.exit_code == 0 and "a: window @0" in placed.output
    assert "window @0 (main %0): a=%1, b=%2" in shown.output
    assert typed.output.strip() == "delivered"

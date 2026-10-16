"""The pane board: agents placed in tmux windows and panes, rebalanced as they come and go.

The board keeps which agent sits in which pane of which window
(``<RAVEN_HOME>/panes/board.json``). Placing an agent puts it in the first
window with room -- a new window of the board's tmux session when every window
is full -- by splitting that window's main pane, then re-plans the window
(:mod:`raven.ops.pane_layout`) and applies the plan with ``select-layout``, so
every pane in it is resized to its slot. Removing an agent closes its pane and
rebalances the rest. A pane that died outside the board is dropped the next time
the board looks. Typing into an agent's pane goes through the safe delivery of
:mod:`raven.ops.panes`: only when the agent waits at a prompt and nobody is typing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from raven.ops import pane_layout
from raven.ops.panes import Runner, SafeDelivery, TmuxPane, tmux_runner
from raven.utils.atomic_io import atomic_replace


class BoardError(RuntimeError):
    """A placement the board cannot make."""


@dataclass
class Seat:
    name: str
    pane: str
    created: float


@dataclass
class Window:
    id: str
    main: str
    agents: list[Seat] = field(default_factory=list)


def _num(pane_id: str) -> int:
    return int(pane_id.lstrip("%"))


class PaneBoard:
    def __init__(
        self,
        state: Path,
        *,
        session: str = "raven",
        runner: Runner = tmux_runner,
        share: float = pane_layout.MAIN_SHARE,
        min_w: int = pane_layout.MIN_PANE_WIDTH,
        min_h: int = pane_layout.MIN_PANE_HEIGHT,
        outbox: Path | None = None,
    ) -> None:
        self.state = Path(state)
        self.session = session
        self._run = runner
        self.share, self.min_w, self.min_h = share, min_w, min_h
        self.outbox = outbox or self.state.parent / "outbox.json"

    # -- state ---------------------------------------------------------------

    def windows(self) -> list[Window]:
        if not self.state.is_file():
            return []
        rows = json.loads(self.state.read_text(encoding="utf-8")).get("windows") or []
        return [Window(id=w["id"], main=w["main"], agents=[Seat(**s) for s in w.get("agents") or []]) for w in rows]

    def _save(self, windows: list[Window]) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        payload = {"session": self.session, "windows": [asdict(w) for w in windows]}
        atomic_replace(self.state, json.dumps(payload, ensure_ascii=False, indent=2))

    # -- tmux ----------------------------------------------------------------

    def _tmux(self, *args: str) -> str:
        code, out = self._run(args)
        if code != 0:
            raise BoardError(f"tmux {' '.join(args)} failed")
        return out.strip()

    def _live_panes(self) -> set[str]:
        return set(self._tmux("list-panes", "-a", "-F", "#{pane_id}").split())

    def _size(self, window: Window) -> tuple[int, int]:
        width, height = self._tmux("display-message", "-p", "-t", window.id, "#{window_width} #{window_height}").split()
        return int(width), int(height)

    def _capacity(self, window: Window) -> pane_layout.Capacity:
        width, height = self._size(window)
        return pane_layout.capacity(width, height, share=self.share, min_w=self.min_w, min_h=self.min_h)

    def _new_window(self) -> Window:
        fmt = "#{window_id} #{pane_id}"
        code, _ = self._run(("has-session", "-t", self.session))
        if code == 0:
            out = self._tmux("new-window", "-d", "-t", self.session, "-P", "-F", fmt)
        else:
            out = self._tmux("new-session", "-d", "-s", self.session, "-x", "240", "-y", "60", "-P", "-F", fmt)
        window_id, main = out.split()
        return Window(id=window_id, main=main)

    def _rebalance(self, window: Window) -> str:
        width, height = self._size(window)
        cap = pane_layout.capacity(width, height, share=self.share, min_w=self.min_w, min_h=self.min_h)
        layout = pane_layout.tmux_layout(
            width, height, _num(window.main), [_num(s.pane) for s in window.agents], cap, share=self.share
        )
        self._tmux("select-layout", "-t", window.id, layout)
        return layout

    def _reconciled(self) -> list[Window]:
        """The board without the panes and windows that died outside it."""
        windows = self.windows()
        if not windows:
            return windows
        live = self._live_panes()
        kept = []
        for window in windows:
            if window.main not in live:
                continue
            window.agents = [s for s in window.agents if s.pane in live]
            kept.append(window)
        return kept

    # -- verbs ---------------------------------------------------------------

    def place(self, names: Sequence[str], command: str = "") -> list[tuple[str, str, str]]:
        """Place each agent, rebalancing its window; returns (agent, window, pane) per agent placed."""
        windows = self._reconciled()
        seated = {s.name for w in windows for s in w.agents}
        placed = []
        for name in names:
            if name in seated:
                raise BoardError(f"{name} already has a pane")
            target = next((w for w in windows if len(w.agents) < self._capacity(w).total), None)
            if target is None:
                target = self._new_window()
                if self._capacity(target).total == 0:
                    raise BoardError("the terminal is too small for even one agent pane beside the main one")
                windows.append(target)
            args = ["split-window", "-d", "-t", target.main, "-P", "-F", "#{pane_id}"]
            if command:
                args.append(command.replace("{name}", name))
            pane = self._tmux(*args)
            target.agents.append(Seat(name=name, pane=pane, created=time.time()))
            self._rebalance(target)
            seated.add(name)
            placed.append((name, target.id, pane))
        self._save(windows)
        return placed

    def remove(self, name: str) -> None:
        windows = self._reconciled()
        for window in windows:
            seat = next((s for s in window.agents if s.name == name), None)
            if seat is None:
                continue
            self._run(("kill-pane", "-t", seat.pane))
            window.agents.remove(seat)
            self._rebalance(window)
            self._save(windows)
            return
        raise BoardError(f"{name} has no pane")

    def rebalance(self) -> list[str]:
        windows = self._reconciled()
        layouts = [self._rebalance(w) for w in windows]
        self._save(windows)
        return layouts

    def pane_of(self, name: str) -> str:
        for window in self.windows():
            for seat in window.agents:
                if seat.name == name:
                    return seat.pane
        raise BoardError(f"{name} has no pane")

    def type(self, name: str, text: str) -> str:
        """Type into the agent's pane when it is safe; otherwise held, as ``raven ops pane send`` does."""
        delivery = SafeDelivery(self.outbox, pane=lambda target: TmuxPane(target, self._run))
        return delivery.deliver(self.pane_of(name), text)


__all__ = ["BoardError", "PaneBoard", "Seat", "Window"]

"""Delivering text into a live terminal session without trampling the person using it.

A live session here is a tmux pane running an interactive agent or shell. Text
goes in only when the pane is ready -- its last line is an idle prompt, so the
program is waiting for input rather than working -- and nobody is typing in
it: the pane is not the focused one, and nothing is already sitting on its
input line. Otherwise the message is held in an outbox file, in order, and a
later flush delivers what can go, stopping at the first message per pane that
still cannot, so a pane never receives its messages out of order.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from raven.utils.atomic_io import atomic_replace

Runner = Callable[[Sequence[str]], tuple[int, str]]

#: Last lines that mean "waiting for input": Claude Code's and Codex's prompts, and a shell's.
IDLE_PROMPTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*[│|]?\s*>\s*(?P<typed>.*?)\s*[│|]?\s*$"),
    re.compile(r"^\s*›\s*(?P<typed>.*)$"),
    re.compile(r"^.*?[$#%]\s(?P<typed>.*)$"),
)


def tmux_runner(args: Sequence[str]) -> tuple[int, str]:
    if shutil.which("tmux") is None:
        raise RuntimeError("tmux is not installed here; live-session delivery needs it")
    done = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5)  # noqa: S603,S607
    return done.returncode, done.stdout


@dataclass(frozen=True)
class PaneState:
    ready: bool
    typing: bool
    reason: str = ""


class TmuxPane:
    """One pane, addressed by a tmux target (``session:window.pane`` or ``%id``)."""

    def __init__(self, target: str, runner: Runner = tmux_runner) -> None:
        self.target = target
        self._run = runner

    def _out(self, *args: str) -> str:
        code, out = self._run(args)
        if code != 0:
            raise RuntimeError(f"tmux {' '.join(args)} failed for {self.target}")
        return out

    def state(self) -> PaneState:
        if self._out("display-message", "-p", "-t", self.target, "#{pane_active}").strip() == "1":
            return PaneState(ready=False, typing=True, reason="the pane has focus; a person may be using it")
        lines = [line for line in self._out("capture-pane", "-p", "-t", self.target).splitlines() if line.strip()]
        last = lines[-1] if lines else ""
        for prompt in IDLE_PROMPTS:
            found = prompt.match(last)
            if found:
                if found.group("typed").strip():
                    return PaneState(ready=True, typing=True, reason="text is already on the input line")
                return PaneState(ready=True, typing=False)
        return PaneState(ready=False, typing=False, reason="the program is working (no idle prompt)")

    def send(self, text: str) -> None:
        self._out("send-keys", "-t", self.target, "-l", text)
        self._out("send-keys", "-t", self.target, "Enter")


@dataclass
class HeldMessage:
    target: str
    text: str
    queued_at: float
    reason: str


class SafeDelivery:
    """Delivers to a pane when it is safe and holds the rest in ``outbox`` until it is."""

    def __init__(self, outbox: Path, pane: Callable[[str], TmuxPane] | None = None) -> None:
        self.outbox = Path(outbox)
        self._pane = pane or TmuxPane

    def held(self) -> list[HeldMessage]:
        if not self.outbox.is_file():
            return []
        return [HeldMessage(**row) for row in json.loads(self.outbox.read_text(encoding="utf-8"))]

    def _store(self, rows: list[HeldMessage]) -> None:
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.outbox, json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2))

    def _blocked(self, pane: TmuxPane) -> str:
        state = pane.state()
        if not state.ready or state.typing:
            return state.reason
        return ""

    def deliver(self, target: str, text: str) -> str:
        """``"delivered"`` or ``"held: <why>"``. A pane that already has held messages queues behind them."""
        rows = self.held()
        pane = self._pane(target)
        if any(row.target == target for row in rows):
            why = "earlier messages for this pane are still held"
        else:
            why = self._blocked(pane)
        if not why:
            pane.send(text)
            return "delivered"
        rows.append(HeldMessage(target=target, text=text, queued_at=time.time(), reason=why))
        self._store(rows)
        return f"held: {why}"

    def flush(self) -> list[tuple[str, str]]:
        """Deliver what can go, in order; returns (target, outcome) for every held message."""
        remaining: list[HeldMessage] = []
        outcomes: list[tuple[str, str]] = []
        stuck: set[str] = set()
        for row in self.held():
            if row.target in stuck:
                remaining.append(row)
                outcomes.append((row.target, "held: behind an earlier message"))
                continue
            pane = self._pane(row.target)
            why = self._blocked(pane)
            if why:
                stuck.add(row.target)
                row.reason = why
                remaining.append(row)
                outcomes.append((row.target, f"held: {why}"))
                continue
            pane.send(row.text)
            outcomes.append((row.target, "delivered"))
        self._store(remaining)
        return outcomes


__all__ = ["HeldMessage", "PaneState", "SafeDelivery", "TmuxPane", "tmux_runner"]

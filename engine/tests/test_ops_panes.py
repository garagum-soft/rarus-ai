"""Safe delivery into a live session: held while busy or while someone types, delivered once free."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.ops.panes import SafeDelivery, TmuxPane


class _Tmux:
    """A tmux server with panes whose screen, focus and received keys a test controls."""

    def __init__(self) -> None:
        self.screen: dict[str, str] = {}
        self.focused: set[str] = set()
        self.typed: dict[str, list[str]] = {}

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        verb, target = args[0], args[args.index("-t") + 1]
        if verb == "display-message":
            return 0, "1\n" if target in self.focused else "0\n"
        if verb == "capture-pane":
            return 0, self.screen.get(target, "")
        if verb == "send-keys":
            self.typed.setdefault(target, []).append(args[-1])
            return 0, ""
        return 1, ""


def _delivery(tmp_path: Path, tmux: _Tmux) -> SafeDelivery:
    return SafeDelivery(tmp_path / "outbox.json", pane=lambda target: TmuxPane(target, runner=tmux))


IDLE = "Welcome\n\n> \n"
WORKING = "Thinking about the task...\n  esc to interrupt\n"
TYPING = "Welcome\n\n> fix the login bu\n"


def test_an_idle_pane_nobody_is_using_gets_the_text_and_enter(tmp_path: Path) -> None:
    tmux = _Tmux()
    tmux.screen["work:1.0"] = IDLE

    assert _delivery(tmp_path, tmux).deliver("work:1.0", "run the tests") == "delivered"
    assert tmux.typed["work:1.0"] == ["run the tests", "Enter"]


def test_text_on_the_input_line_holds_the_message_until_the_operator_is_done(tmp_path: Path) -> None:
    tmux = _Tmux()
    tmux.screen["work:1.0"] = TYPING
    delivery = _delivery(tmp_path, tmux)

    outcome = delivery.deliver("work:1.0", "run the tests")

    assert outcome == "held: text is already on the input line"
    assert "work:1.0" not in tmux.typed, "nothing is typed over the operator"
    assert [m.text for m in delivery.held()] == ["run the tests"]

    tmux.screen["work:1.0"] = IDLE
    assert delivery.flush() == [("work:1.0", "delivered")]
    assert tmux.typed["work:1.0"] == ["run the tests", "Enter"]
    assert delivery.held() == []


def test_a_focused_pane_and_a_working_program_both_hold(tmp_path: Path) -> None:
    tmux = _Tmux()
    tmux.screen.update({"a": IDLE, "b": WORKING})
    tmux.focused.add("a")
    delivery = _delivery(tmp_path, tmux)

    assert "focus" in delivery.deliver("a", "one")
    assert "working" in delivery.deliver("b", "two")
    assert tmux.typed == {}


def test_held_messages_go_out_in_order_and_a_new_one_waits_behind_them(tmp_path: Path) -> None:
    tmux = _Tmux()
    tmux.screen["p"] = WORKING
    delivery = _delivery(tmp_path, tmux)
    delivery.deliver("p", "first")
    tmux.screen["p"] = IDLE

    assert delivery.deliver("p", "second") == "held: earlier messages for this pane are still held"
    assert delivery.flush() == [("p", "delivered"), ("p", "delivered")]
    assert tmux.typed["p"] == ["first", "Enter", "second", "Enter"]


def test_a_busy_pane_does_not_hold_up_another(tmp_path: Path) -> None:
    tmux = _Tmux()
    tmux.screen.update({"busy": WORKING, "free": WORKING})
    delivery = _delivery(tmp_path, tmux)
    delivery.deliver("busy", "a1")
    delivery.deliver("busy", "a2")
    delivery.deliver("free", "b1")
    tmux.screen["free"] = IDLE

    outcomes = delivery.flush()

    assert outcomes == [
        ("busy", "held: the program is working (no idle prompt)"),
        ("busy", "held: behind an earlier message"),
        ("free", "delivered"),
    ]
    assert [m.text for m in delivery.held()] == ["a1", "a2"]


def test_the_command_line_holds_then_flushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli import ops_connection_commands as ops
    from raven.cli.commands import app

    tmux = _Tmux()
    tmux.screen["work:1.0"] = TYPING
    monkeypatch.setattr(ops, "_delivery", lambda: _delivery(tmp_path, tmux))
    runner = CliRunner()

    held = runner.invoke(app, ["ops", "pane", "send", "work:1.0", "run the tests"])
    listed = runner.invoke(app, ["ops", "pane", "held"])
    tmux.screen["work:1.0"] = IDLE
    flushed = runner.invoke(app, ["ops", "pane", "flush"])

    assert held.exit_code == 0 and "held: text is already on the input line" in held.output
    assert "run the tests" in listed.output
    assert "work:1.0: delivered" in flushed.output
    assert tmux.typed["work:1.0"] == ["run the tests", "Enter"]

"""Operator notifications: an event a rule routes reaches its channel; a switched-off channel sends nothing."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from raven.proactive_engine import notify
from raven.proactive_engine.notify import Event, Notifier, Settings, webhook_payload

ESCALATION = Event(
    kind="host_guard", priority="critical", title="Disk full; agents stopped", key="host_guard", detail="2% free"
)


class Hook:
    """A real HTTP endpoint on localhost that records what was POSTed to it."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    hook.bodies.append(json.loads(raw))
                except ValueError:
                    hook.bodies.append({"raw": raw.decode("utf-8")})
                hook.headers.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def hook():
    h = Hook()
    yield h
    h.close()


def _settings(channels: dict[str, dict[str, Any]], rules: list[dict[str, Any]], **kw: Any) -> Settings:
    return Settings(enabled=True, channels=channels, rules=rules, **kw)


def test_an_escalation_goes_to_the_webhook_its_rule_names(hook: Hook, tmp_path: Path) -> None:
    notifier = Notifier(
        _settings({"ops": {"type": "webhook", "url": hook.url + "/hook"}}, [{"on": ["escalation"], "channel": "ops"}]),
        log_path=tmp_path / "log.jsonl",
    )

    [delivery] = notifier.dispatch(ESCALATION)

    assert delivery.sent and hook.bodies[0]["kind"] == "host_guard" and hook.bodies[0]["priority"] == "critical"
    assert "Disk full" in hook.bodies[0]["message"]
    logged = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logged[0]["sent"] is True and logged[0]["channel"] == "ops"


def test_a_rule_whose_channel_is_switched_off_sends_nothing(hook: Hook, tmp_path: Path) -> None:
    notifier = Notifier(
        _settings(
            {"ops": {"type": "webhook", "url": hook.url, "enabled": False}},
            [{"on": ["escalation"], "channel": "ops"}],
        ),
        log_path=tmp_path / "log.jsonl",
    )

    [delivery] = notifier.dispatch(ESCALATION)

    assert not delivery.sent and delivery.reason == "channel is switched off" and hook.bodies == []


def test_names_and_min_priority_pick_the_rules() -> None:
    sent: list[str] = []
    notifier = Notifier(
        _settings(
            {"a": {"type": "x"}, "b": {"type": "x"}, "c": {"type": "x"}},
            [
                {"on": ["escalation"], "channel": "a"},
                {"on": ["question"], "channel": "b", "min_priority": "normal"},
                {"on": ["*"], "channel": "c", "min_priority": "critical"},
            ],
        ),
        senders={"x": lambda ch, ev: sent.append(ev.title)},
        log_path=Path("unused"),
    )
    notifier._log = lambda event, delivery: None  # type: ignore[method-assign]

    assert [d.channel for d in notifier.dispatch(Event("question", "normal", "q", "k1"))] == ["b"]
    assert [d.channel for d in notifier.dispatch(Event("question", "low", "q", "k2"))] == []
    assert [d.channel for d in notifier.dispatch(Event("approval", "high", "a", "k3"))] == ["a"]
    assert [d.channel for d in notifier.dispatch(Event("host_guard", "critical", "h", "k4"))] == ["a", "c"]


def _quiet_notifier(sent: list[str], now: list[float], **kw: Any) -> Notifier:
    n = Notifier(
        _settings({"ch": {"type": "x"}}, [{"on": ["*"], "channel": "ch"}], **kw),
        senders={"x": lambda ch, ev: sent.append(ev.key)},
        log_path=Path("unused"),
        clock=lambda: now[0],
    )
    n._log = lambda event, delivery: None  # type: ignore[method-assign]
    return n


def test_the_same_problem_is_not_sent_twice_within_the_window_and_the_daily_cap_holds() -> None:
    sent: list[str] = []
    now = [datetime(2026, 10, 13, 12).timestamp()]
    n = _quiet_notifier(sent, now, repeat_after_s=600, daily_cap=2)

    reasons = [n.dispatch(Event("q", "high", "t", key))[0].reason for key in ("a", "a", "b", "c")]
    now[0] += 601
    later = n.dispatch(Event("q", "high", "t", "a"))[0].reason

    assert sent == ["a", "b"] and reasons == ["", "sent recently", "", "daily cap reached"]
    assert later == "daily cap reached", "the cap is per day, not per window"
    now[0] += 24 * 3600
    assert n.dispatch(Event("q", "high", "t", "c"))[0].sent, "a new day starts a new count"


def test_quiet_hours_hold_everything_but_critical() -> None:
    sent: list[str] = []
    now = [datetime(2026, 10, 13, 23, 30).timestamp()]
    n = _quiet_notifier(sent, now, quiet_start_hour=22, quiet_end_hour=8)

    held = n.dispatch(Event("q", "high", "t", "x"))[0]
    loud = n.dispatch(Event("q", "critical", "t", "y"))[0]

    assert (held.sent, held.reason, loud.sent) == (False, "quiet hours", True)


def test_a_problem_solved_before_delivery_is_not_sent_and_a_failing_channel_does_not_stop_the_rest() -> None:
    sent: list[str] = []

    def boom(ch: Any, ev: Event) -> None:
        raise ConnectionError("unreachable")

    n = Notifier(
        _settings(
            {"bad": {"type": "boom"}, "good": {"type": "x"}},
            [{"on": ["*"], "channel": "bad"}, {"on": ["*"], "channel": "good"}],
        ),
        senders={"boom": boom, "x": lambda ch, ev: sent.append(ev.key)},
        still_open=lambda key: key != "solved",
        log_path=Path("unused"),
    )
    n._log = lambda event, delivery: None  # type: ignore[method-assign]

    first = n.dispatch(Event("q", "high", "t", "open"))
    solved = n.dispatch(Event("q", "high", "t", "solved"))

    assert [(d.channel, d.sent) for d in first] == [("bad", False), ("good", True)] and "unreachable" in first[0].reason
    assert {d.reason for d in solved} == {"already resolved"} and sent == ["open"]


def test_slack_and_discord_get_their_own_shapes_and_ntfy_its_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    slack = webhook_payload({"format": "slack"}, ESCALATION)
    discord = webhook_payload({"format": "discord"}, ESCALATION)
    posted: list[tuple[str, dict[str, str], bytes]] = []
    monkeypatch.setattr(
        notify, "_post", lambda url, data=None, headers=None, json_body=None: posted.append((url, headers, data))
    )

    notify.send_ntfy({"server": "https://ntfy.example/", "topic": "raven-home", "token": "tk"}, ESCALATION)

    assert set(slack) == {"text"} and discord["allowed_mentions"] == {"parse": []}
    url, headers, body = posted[0]
    assert (
        url == "https://ntfy.example/raven-home"
        and headers["Priority"] == "5"
        and headers["Authorization"] == "Bearer tk"
    )
    assert body == b"2% free"


def test_notifications_off_route_nothing() -> None:
    n = Notifier(Settings(enabled=False, channels={"a": {"type": "x"}}, rules=[{"on": ["*"], "channel": "a"}]))
    assert n.dispatch(ESCALATION) == []


def test_a_problem_raised_in_the_attention_queue_reaches_the_webhook_through_the_config(
    hook: Hook, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: config file -> attention queue -> notifier -> real HTTP."""
    from raven.config.loader import set_config_path
    from raven.proactive_engine.attention import AttentionQueue

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "gateway": {
                    "notifications": {
                        "enabled": True,
                        "channels": {"ops": {"type": "webhook", "url": hook.url, "format": "slack"}},
                        "rules": [{"on": ["approval"], "channel": "ops"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    set_config_path(config)
    monkeypatch.setattr(notify, "_notifier", None)
    try:
        q = AttentionQueue(tmp_path / "attention.json")
        q.raise_item("approval:1", kind="approval", priority="high", title="Approve: rm -rf build", session_key="tui:1")
        q.raise_item(
            "approval:1", kind="approval", priority="high", title="Approve: rm -rf build"
        )  # an update: no repeat
        q.raise_item("question:1", kind="question", priority="normal", title="Which branch?")  # no rule for it
        deadline = time.time() + 10
        while not hook.bodies and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    assert [b["text"] for b in hook.bodies] == ["[raven:high] Approve: rm -rf build [tui:1]"]


def test_the_command_line_tests_a_channel(hook: Hook, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from raven.cli.commands import app
    from raven.config.loader import set_config_path

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "gateway": {
                    "notifications": {
                        "channels": {
                            "ops": {"type": "webhook", "url": hook.url},
                            "off": {"type": "webhook", "url": hook.url, "enabled": False},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    set_config_path(config)
    try:
        ok = CliRunner().invoke(app, ["notify", "test", "ops"])
        off = CliRunner().invoke(app, ["notify", "test", "off"])
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    assert ok.exit_code == 0 and len(hook.bodies) == 1 and hook.bodies[0]["kind"] == "test"
    assert off.exit_code == 1 and "switched off" in off.output

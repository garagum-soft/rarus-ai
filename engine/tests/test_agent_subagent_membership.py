"""Joining, stopping and removing roster agents only through the leader's approval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.membership import (
    MembershipError,
    MembershipLedger,
    RosterOps,
    acting_identity,
)

ROW = {"kind": "cli", "command": "codex exec {prompt}"}


def _config(tmp_path: Path, agents: list[dict[str, Any]] | None = None, leader: str = "lead") -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"subagents": {"leader": leader, "agents": agents or []}}), encoding="utf-8")
    return path


def _ledger(tmp_path: Path, agents: list[dict[str, Any]] | None = None) -> tuple[MembershipLedger, Path]:
    cfg = _config(tmp_path, agents)
    return MembershipLedger(tmp_path / "membership.json", RosterOps.from_config(cfg), leader="lead"), cfg


def _rows(cfg: Path) -> list[dict[str, Any]]:
    return json.loads(cfg.read_text(encoding="utf-8"))["subagents"]["agents"]


def test_a_join_does_nothing_until_the_leader_approves_and_then_the_member_is_active(tmp_path: Path) -> None:
    ledger, cfg = _ledger(tmp_path)

    filed = ledger.request("join", "codex", requested_by="codex", entry=ROW)
    assert _rows(cfg) == [], "a request alone changes nothing"
    assert [r.id for r in ledger.requests("pending")] == [filed.id]

    ledger.approve(filed.id, approver="lead")

    rows = _rows(cfg)
    assert [r["name"] for r in rows] == ["codex"] and rows[0]["enabled"] is True
    assert ledger.get(filed.id).status == "approved" and ledger.get(filed.id).decided_by == "lead"


def test_only_the_leader_may_decide(tmp_path: Path) -> None:
    ledger, cfg = _ledger(tmp_path)
    filed = ledger.request("join", "codex", requested_by="codex", entry=ROW)

    with pytest.raises(MembershipError, match="only the leader"):
        ledger.approve(filed.id, approver="codex")
    with pytest.raises(MembershipError, match="only the leader"):
        ledger.reject(filed.id, approver="intruder")

    assert _rows(cfg) == [] and ledger.get(filed.id).status == "pending"


def test_removal_without_approval_is_not_carried_out(tmp_path: Path) -> None:
    ledger, cfg = _ledger(tmp_path, [{"name": "codex", **ROW}])

    filed = ledger.request("remove", "codex", requested_by="someone", reason="unused")
    assert [r["name"] for r in _rows(cfg)] == ["codex"]

    ledger.reject(filed.id, approver="lead", note="still needed")
    assert [r["name"] for r in _rows(cfg)] == ["codex"]
    with pytest.raises(MembershipError, match="already rejected"):
        ledger.approve(filed.id, approver="lead")


def test_an_approved_removal_takes_the_agent_off_the_roster(tmp_path: Path) -> None:
    ledger, cfg = _ledger(tmp_path, [{"name": "codex", **ROW}])
    filed = ledger.request("remove", "codex", requested_by="lead")

    ledger.approve(filed.id, approver="lead")

    assert _rows(cfg) == []


def test_an_approved_shutdown_keeps_the_agent_listed_but_disabled(tmp_path: Path) -> None:
    ledger, cfg = _ledger(tmp_path, [{"name": "codex", **ROW}])
    filed = ledger.request("shutdown", "codex", requested_by="codex", reason="done for today")

    ledger.approve(filed.id, approver="lead")

    assert _rows(cfg)[0]["name"] == "codex" and _rows(cfg)[0]["enabled"] is False


def test_requests_that_make_no_sense_are_refused(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path, [{"name": "codex", **ROW}])

    with pytest.raises(MembershipError, match="already on the roster"):
        ledger.request("join", "codex", requested_by="x", entry=ROW)
    with pytest.raises(MembershipError, match="not on the roster"):
        ledger.request("remove", "ghost", requested_by="x")
    ledger.request("shutdown", "codex", requested_by="x")
    with pytest.raises(MembershipError, match="already pending"):
        ledger.request("shutdown", "codex", requested_by="x")
    with pytest.raises(MembershipError, match="own name"):
        ledger.request("join", "hermes", requested_by="x", entry={"name": "other", **ROW})


def test_the_acting_identity_must_be_stated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAVEN_TEAM_IDENTITY", raising=False)
    with pytest.raises(MembershipError, match="who is acting"):
        acting_identity(None)
    monkeypatch.setenv("RAVEN_TEAM_IDENTITY", "lead")
    assert acting_identity(None) == "lead"
    assert acting_identity("codex") == "codex"


def test_the_command_line_runs_the_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli import team_commands
    from raven.cli.commands import app

    ledger, cfg = _ledger(tmp_path)
    monkeypatch.setattr(team_commands, "_ledger", lambda: ledger)
    runner = CliRunner()

    filed = runner.invoke(app, ["team", "request-join", "codex", "--entry", json.dumps(ROW), "--as", "codex"])
    assert filed.exit_code == 0, filed.output
    request_id = ledger.requests("pending")[0].id

    refused = runner.invoke(app, ["team", "approve", request_id, "--as", "codex"])
    assert refused.exit_code == 1 and "only the leader" in refused.output
    assert _rows(cfg) == []

    approved = runner.invoke(app, ["team", "approve", request_id, "--as", "lead"])
    assert approved.exit_code == 0, approved.output
    assert [r["name"] for r in _rows(cfg)] == ["codex"]
    assert "approved" in runner.invoke(app, ["team", "requests", "--all"]).output

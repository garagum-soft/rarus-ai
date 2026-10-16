"""The decision log: a decision with its assumptions, a review of them, and the outcome."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.stint import decision_log as log


def _record(project: Path, **over) -> log.Decision:
    args = {
        "assumptions": ["traffic stays under 1k rps", "the team knows Postgres"],
        "review_when": "when traffic passes 1k rps",
        "review_after": "2026-12-01",
        "rationale": "one database to run",
    }
    args.update(over)
    return log.record(project, "Pick the queue", "use Postgres as the queue", **args)


def test_a_decision_is_kept_with_its_assumptions_and_review_condition(tmp_path: Path) -> None:
    entry = _record(tmp_path)

    stored = log.load(tmp_path)[0]
    assert entry.id == "D1" and stored.decision == "use Postgres as the queue"
    assert [a.text for a in stored.assumptions] == ["traffic stays under 1k rps", "the team knows Postgres"]
    assert stored.review_when == "when traffic passes 1k rps" and stored.status == "active"


def test_a_review_then_an_outcome_both_show_in_the_log(tmp_path: Path) -> None:
    _record(tmp_path)

    reviewed = log.review(tmp_path, "D1", note="traffic hit 3k rps in October", failed=[1])
    assert reviewed.status == "needs_revision"
    assert reviewed.assumptions[0].holds is False and reviewed.assumptions[1].holds is True

    settled = log.set_outcome(tmp_path, "D1", text="moved jobs to a dedicated queue in November", verdict="mixed")
    text = log.render(log.load(tmp_path)[0])

    assert settled.status == "settled"
    assert "assumption 1: traffic stays under 1k rps (FAILED)" in text
    assert "reviewed" in text and "traffic hit 3k rps in October; failed: traffic stays under 1k rps" in text
    assert "outcome (mixed" in text and "dedicated queue" in text
    assert text.index("decided:") < text.index("reviewed") < text.index("outcome"), (
        "decision, review, outcome, in order"
    )


def test_a_review_where_everything_holds_keeps_the_decision_active(tmp_path: Path) -> None:
    _record(tmp_path)
    assert log.review(tmp_path, "D1", note="still fine").status == "active"


def test_what_is_due_for_review(tmp_path: Path) -> None:
    _record(tmp_path, review_after="2026-11-01")
    _record(tmp_path, review_after="2027-06-01")
    log.review(tmp_path, "D2", note="the team changed", failed=[2])
    entries = log.load(tmp_path)

    assert [e.id for e in entries if e.due(date(2026, 11, 2))] == ["D1", "D2"]
    assert [e.id for e in entries if e.due(date(2026, 10, 1))] == ["D2"], "a failed assumption is due at once"
    log.set_outcome(tmp_path, "D1", text="fine", verdict="good")
    assert not log.load(tmp_path)[0].due(date(2030, 1, 1)), "a settled decision is never due"


def test_a_decision_without_an_assumption_or_a_review_condition_is_refused(tmp_path: Path) -> None:
    with pytest.raises(log.DecisionLogError, match="assumption"):
        _record(tmp_path, assumptions=[])
    with pytest.raises(log.DecisionLogError, match="when to review"):
        _record(tmp_path, review_when="", review_after="")
    _record(tmp_path)
    with pytest.raises(log.DecisionLogError, match="no assumption 5"):
        log.review(tmp_path, "D1", note="x", failed=[5])
    log.set_outcome(tmp_path, "D1", text="done", verdict="good")
    with pytest.raises(log.DecisionLogError, match="settled"):
        log.review(tmp_path, "D1", note="again")


def test_the_command_line_records_reviews_and_settles(tmp_path: Path) -> None:
    from raven.cli.commands import app

    runner = CliRunner()
    base = ["playbook", "stint", "decision"]
    recorded = runner.invoke(
        app,
        [
            *base,
            "record",
            "Pick the queue",
            "-d",
            "use Postgres",
            "-a",
            "traffic stays low",
            "--review-when",
            "traffic grows",
            "-p",
            str(tmp_path),
        ],
    )
    reviewed = runner.invoke(
        app, [*base, "review", "D1", "--note", "traffic grew", "--failed", "1", "-p", str(tmp_path)]
    )
    due = runner.invoke(app, [*base, "list", "--due", "-p", str(tmp_path)])
    settled = runner.invoke(
        app, [*base, "outcome", "D1", "--text", "switched queues", "--verdict", "good", "-p", str(tmp_path)]
    )
    listed = runner.invoke(app, [*base, "list", "-p", str(tmp_path)])

    assert recorded.exit_code == 0, recorded.output
    assert "needs_revision" in reviewed.output and "D1" in due.output
    assert "settled (good)" in settled.output
    assert "traffic stays low (FAILED)" in listed.output and "outcome (good" in listed.output

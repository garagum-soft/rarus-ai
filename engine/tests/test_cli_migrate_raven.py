"""``rarus migrate-raven``: what moves from a Raven home, and what is left alone."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.cli.migrate_commands import migrate

runner = CliRunner()


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    src, dst = tmp_path / "raven-home", tmp_path / "rarus-home"
    monkeypatch.setenv("RARUS_HOME", str(dst))
    monkeypatch.setenv("RAVEN_HOME", str(src))
    _write(src, "config.json", '{"agents": {}}')
    _write(src, "workspace/sessions/a.jsonl", "line")
    _write(src, "workspace/memory/m.md", "memory")
    _write(src, "workspace/skills/s/SKILL.md", "skill")
    _write(src, "cache/big.bin")
    _write(src, "logs/raven.log")
    _write(src, "oauth/openai_codex.json", "token")
    _write(src, "serve.json", "{}")
    _write(src, "sandbox/x/y")
    _write(src, "workspace/state.lock")
    return src, dst


def test_sessions_settings_skills_and_memory_are_copied(homes):
    src, dst = homes
    report = migrate(src, dst)
    assert report.copied == 4
    assert (dst / "config.json").read_text(encoding="utf-8") == '{"agents": {}}'
    assert (dst / "workspace/sessions/a.jsonl").exists()
    assert (dst / "workspace/memory/m.md").exists()
    assert (dst / "workspace/skills/s/SKILL.md").exists()


def test_caches_logs_tokens_and_the_running_gateways_file_stay_behind(homes):
    src, dst = homes
    migrate(src, dst)
    for left in ("cache", "logs", "oauth", "serve.json", "sandbox", "workspace/state.lock"):
        assert not (dst / left).exists(), left


def test_an_existing_file_is_never_overwritten(homes):
    src, dst = homes
    _write(dst, "config.json", "mine")
    report = migrate(src, dst)
    assert (dst / "config.json").read_text(encoding="utf-8") == "mine"
    assert report.kept == 1


def test_the_source_is_not_touched(homes):
    src, dst = homes
    before = sorted(p.relative_to(src).as_posix() for p in src.rglob("*"))
    migrate(src, dst)
    assert sorted(p.relative_to(src).as_posix() for p in src.rglob("*")) == before


def test_a_dry_run_copies_nothing_but_counts(homes):
    src, dst = homes
    report = migrate(src, dst, dry_run=True)
    assert report.copied == 4
    assert not dst.exists()


def test_running_twice_copies_nothing_the_second_time(homes):
    src, dst = homes
    migrate(src, dst)
    assert migrate(src, dst).copied == 0


def test_the_same_folder_is_refused(tmp_path):
    with pytest.raises(ValueError):
        migrate(tmp_path, tmp_path)


def test_the_command_reports_and_says_how_to_sign_in(homes):
    result = runner.invoke(app, ["migrate-raven"])
    assert result.exit_code == 0, result.output
    assert "Copied 4 files" in result.output
    assert "provider login rarus" in result.output


def test_a_missing_raven_home_is_a_clear_error(homes, tmp_path):
    result = runner.invoke(app, ["migrate-raven", "--from", str(tmp_path / "nowhere")])
    assert result.exit_code == 1
    assert "No Raven home" in result.output

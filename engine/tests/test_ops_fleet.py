"""Fleet management: an update rolled out to simulated machines in waves, with reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.ops.fleet import CommandDriver, Desired, DirectoryDriver, FleetError, plan_waves, reconcile, rollout

SPEC = {"version": "0.3.0", "config": {"log_level": "info", "workers": 4}, "secrets": ["API_TOKEN"]}
ENV = {"API_TOKEN": "s3cret-value"}


def _machines(tmp_path: Path, names: tuple[str, ...] = ("alpha", "beta")) -> list[DirectoryDriver]:
    return [DirectoryDriver(name, tmp_path / name) for name in names]


def test_a_wave_passes_on_two_simulated_machines_and_both_reconcile(tmp_path: Path) -> None:
    desired = Desired.from_spec(SPEC, ENV)

    result = rollout(_machines(tmp_path), desired, waves=["1", "100%"])

    assert result.complete
    assert [[(r.machine, r.status) for r in wave] for wave in result.waves] == [
        [("alpha", "in_sync")],
        [("beta", "in_sync")],
    ]
    assert [r.status for r in reconcile(_machines(tmp_path), desired)] == ["in_sync", "in_sync"]


def test_reconciliation_shows_a_drift_when_there_is_one(tmp_path: Path) -> None:
    desired = Desired.from_spec(SPEC, ENV)
    rollout(_machines(tmp_path), desired)
    state = tmp_path / "beta" / ".raven-fleet" / "state.json"
    drifted = json.loads(state.read_text(encoding="utf-8"))
    drifted["version"] = "0.2.9"
    state.write_text(json.dumps(drifted), encoding="utf-8")

    reports = reconcile(_machines(tmp_path), desired)

    assert [(r.machine, r.status) for r in reports] == [("alpha", "in_sync"), ("beta", "drift")]
    assert "version 0.2.9 != 0.3.0" in reports[1].detail


def test_a_rotated_secret_shows_as_drift_without_the_value_appearing(tmp_path: Path) -> None:
    rollout(_machines(tmp_path), Desired.from_spec(SPEC, ENV))

    reports = reconcile(_machines(tmp_path), Desired.from_spec(SPEC, {"API_TOKEN": "rotated"}))

    assert all(r.status == "drift" and r.detail == "secret API_TOKEN differs" for r in reports)
    state = (tmp_path / "alpha" / ".raven-fleet" / "state.json").read_text(encoding="utf-8")
    assert "s3cret-value" not in state and "rotated" not in state, "only hashes are recorded and compared"


def test_a_wave_that_does_not_reconcile_stops_the_next_wave(tmp_path: Path) -> None:
    class _Broken(DirectoryDriver):
        def apply(self, desired: Desired) -> None:
            super().apply(Desired(version="0.0.1", config=desired.config, secrets=desired.secrets))

    machines = [_Broken("canary", tmp_path / "canary"), *_machines(tmp_path)]

    result = rollout(machines, Desired.from_spec(SPEC, ENV), waves=["1", "100%"])

    assert not result.complete and "canary did not reconcile" in result.stopped
    assert not (tmp_path / "alpha").exists(), "the rest of the fleet was never touched"


def test_wave_sizes() -> None:
    assert plan_waves(2, ["1", "50%", "100%"]) == [1, 1]
    assert plan_waves(10, ["1", "50%", "100%"]) == [1, 4, 5]
    assert plan_waves(3, ["1"]) == [1, 2], "whatever the waves leave is one last wave"


def test_a_spec_with_a_secret_missing_from_the_environment_is_refused() -> None:
    with pytest.raises(FleetError, match="API_TOKEN"):
        Desired.from_spec(SPEC, {})


def test_the_command_driver_speaks_shell_to_a_machine_runner() -> None:
    sent: list[str] = []
    stored: dict[str, str] = {}

    def runner(command: str) -> tuple[int, str]:
        sent.append(command)
        if command.startswith("cat "):
            return 0, stored.get("state", "")
        stored["state"] = json.dumps(Desired.from_spec(SPEC, ENV).fingerprint())
        return 0, ""

    driver = CommandDriver("gpu-box", runner, update_command="pip install raven=={version}")
    driver.apply(Desired.from_spec(SPEC, ENV))

    assert "pip install raven==0.3.0" in sent[0] and "chmod 600" in sent[0]
    assert "s3cret-value" not in sent[0], "the secret travels base64-encoded inside the command, not in the clear"
    assert reconcile([driver], Desired.from_spec(SPEC, ENV))[0].status == "in_sync"


def test_the_command_line_rolls_out_and_reports_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli.commands import app

    monkeypatch.setenv("API_TOKEN", "from-env")
    fleet = tmp_path / "fleet"
    for name in ("alpha", "beta"):
        (fleet / name).mkdir(parents=True)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    runner = CliRunner()

    rolled = runner.invoke(app, ["ops", "fleet", "rollout", "--spec", str(spec), "--simulate", str(fleet)])
    status = runner.invoke(app, ["ops", "fleet", "status", "--spec", str(spec), "--simulate", str(fleet)])
    (fleet / "beta" / ".raven-fleet" / "state.json").write_text("{}", encoding="utf-8")
    drifted = runner.invoke(app, ["ops", "fleet", "status", "--spec", str(spec), "--simulate", str(fleet)])

    assert rolled.exit_code == 0 and "wave 1: alpha in_sync" in rolled.output and "rollout complete" in rolled.output
    assert status.exit_code == 0 and "beta: in_sync" in status.output
    assert drifted.exit_code == 1 and "beta: drift" in drifted.output

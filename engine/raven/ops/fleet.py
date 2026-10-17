"""Rolling an update and its configuration out to a fleet of machines, in waves, against observed state.

The desired state is one version, one configuration and a set of named secrets.
A rollout applies it a wave at a time -- a canary first, then larger waves -- and
after every wave reads back what each machine actually holds; a machine that did
not end up in the desired state stops the rollout before the next wave, so a bad
release reaches one machine rather than all of them. Reconciliation is the same
read on its own: for every machine, in sync, drifted (and in what), or not
answering.

Secrets never travel or rest in the clear outside the machine they are for:
their values come from the environment of the machine running the rollout, are
handed to the machine's driver, and only their hashes enter the recorded state
and the comparison, so a drifted secret is found without anyone reading it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

STATE_DIR = ".raven-fleet"


class FleetError(ValueError):
    """A spec or a rollout the fleet refuses."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Desired:
    version: str
    config: Mapping[str, Any]
    secrets: Mapping[str, str] = field(default_factory=dict)

    def fingerprint(self) -> dict[str, Any]:
        """What a machine in this state reports: the version and hashes, never a secret's value."""
        return {
            "version": self.version,
            "config": _digest(json.dumps(self.config, sort_keys=True)),
            "secrets": {name: _digest(value) for name, value in sorted(self.secrets.items())},
        }

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any], env: Mapping[str, str] | None = None) -> "Desired":
        env = os.environ if env is None else env
        names = list(spec.get("secrets") or [])
        missing = [name for name in names if name not in env]
        if missing:
            raise FleetError(f"secrets not set in this environment: {', '.join(missing)}")
        if not spec.get("version"):
            raise FleetError("the spec needs a version")
        return cls(
            version=str(spec["version"]), config=dict(spec.get("config") or {}), secrets={n: env[n] for n in names}
        )


class MachineDriver(Protocol):
    name: str

    def apply(self, desired: Desired) -> None: ...

    def observe(self) -> dict[str, Any] | None: ...


class DirectoryDriver:
    """A machine simulated by a directory: what an install would write, written there."""

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = Path(root)

    def apply(self, desired: Desired) -> None:
        state = self.root / STATE_DIR
        state.mkdir(parents=True, exist_ok=True)
        (state / "config.json").write_text(json.dumps(desired.config, sort_keys=True), encoding="utf-8")
        secrets = state / "secrets.env"
        secrets.write_text("".join(f"{k}={v}\n" for k, v in sorted(desired.secrets.items())), encoding="utf-8")
        os.chmod(secrets, 0o600)
        (state / "state.json").write_text(json.dumps(desired.fingerprint(), sort_keys=True), encoding="utf-8")

    def observe(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.root / STATE_DIR / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class CommandDriver:
    """A registered machine reached through its command runner (``raven.ops.transport``), POSIX shell.

    ``update_command`` installs the version (``{version}`` is filled in); the
    configuration and the secrets are written under ``~/.rarus-fleet`` with the
    secrets file owner-only, and the state file records only the fingerprint.
    """

    def __init__(self, name: str, runner: Any, update_command: str = "") -> None:
        self.name = name
        self._run = runner
        self.update_command = update_command

    def _write(self, rel: str, text: str, mode: str = "644") -> str:
        encoded = base64.b64encode(text.encode("utf-8")).decode()
        target = f"$HOME/{STATE_DIR}/{rel}"
        return f"printf %s {shlex.quote(encoded)} | base64 -d > {target} && chmod {mode} {target}"

    def apply(self, desired: Desired) -> None:
        steps = [f"mkdir -p $HOME/{STATE_DIR}"]
        if self.update_command:
            steps.append(self.update_command.format(version=shlex.quote(desired.version)))
        steps.append(self._write("config.json", json.dumps(desired.config, sort_keys=True)))
        steps.append(
            self._write("secrets.env", "".join(f"{k}={v}\n" for k, v in sorted(desired.secrets.items())), "600")
        )
        steps.append(self._write("state.json", json.dumps(desired.fingerprint(), sort_keys=True)))
        code, out = self._run(" && ".join(steps))
        if code != 0:
            raise FleetError(f"{self.name}: applying failed ({code}): {out.strip()[-300:]}")

    def observe(self) -> dict[str, Any] | None:
        code, out = self._run(f"cat $HOME/{STATE_DIR}/state.json")
        if code != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None


@dataclass
class MachineReport:
    machine: str
    status: str
    detail: str = ""


def reconcile(drivers: Sequence[MachineDriver], desired: Desired) -> list[MachineReport]:
    """Read back every machine and compare it with the desired state."""
    want = desired.fingerprint()
    reports: list[MachineReport] = []
    for driver in drivers:
        try:
            seen = driver.observe()
        except Exception as exc:  # noqa: BLE001 - an unreachable machine is a report, not a crash
            reports.append(MachineReport(driver.name, "unreachable", str(exc)))
            continue
        if seen is None:
            reports.append(MachineReport(driver.name, "unreachable", "no state recorded"))
            continue
        drifted = []
        if seen.get("version") != want["version"]:
            drifted.append(f"version {seen.get('version')} != {want['version']}")
        if seen.get("config") != want["config"]:
            drifted.append("config differs")
        theirs, mine = seen.get("secrets") or {}, want["secrets"]
        drifted += [
            f"secret {name} differs" for name in sorted(set(theirs) | set(mine)) if theirs.get(name) != mine.get(name)
        ]
        reports.append(MachineReport(driver.name, "drift" if drifted else "in_sync", "; ".join(drifted)))
    return reports


def plan_waves(count: int, waves: Sequence[str]) -> list[int]:
    """Wave sizes for ``count`` machines from specs like ``1``, ``50%`` or ``100%`` (cumulative)."""
    sizes: list[int] = []
    done = 0
    for spec in waves:
        target = round(count * float(spec[:-1]) / 100) if spec.endswith("%") else done + int(spec)
        target = max(done + 1, min(count, target)) if done < count else count
        if target > done:
            sizes.append(target - done)
            done = target
    if done < count:
        sizes.append(count - done)
    return sizes


@dataclass
class RolloutResult:
    waves: list[list[MachineReport]]
    stopped: str = ""

    @property
    def complete(self) -> bool:
        return not self.stopped


def rollout(
    drivers: Sequence[MachineDriver], desired: Desired, *, waves: Sequence[str] = ("1", "50%", "100%")
) -> RolloutResult:
    """Apply the desired state wave by wave; stop before the next wave when one does not reconcile."""
    if not drivers:
        raise FleetError("no machines to roll out to")
    result = RolloutResult(waves=[])
    start = 0
    for size in plan_waves(len(drivers), waves):
        wave = list(drivers[start : start + size])
        start += size
        for driver in wave:
            try:
                driver.apply(desired)
            except Exception as exc:  # noqa: BLE001 - recorded, then the wave's reconcile says so
                result.waves.append([MachineReport(driver.name, "failed", str(exc))])
                result.stopped = f"{driver.name} failed to apply; later waves were not started"
                return result
        reports = reconcile(wave, desired)
        result.waves.append(reports)
        bad = [r.machine for r in reports if r.status != "in_sync"]
        if bad:
            result.stopped = f"{', '.join(bad)} did not reconcile after the wave; later waves were not started"
            return result
    return result


__all__ = [
    "CommandDriver",
    "Desired",
    "DirectoryDriver",
    "FleetError",
    "MachineReport",
    "RolloutResult",
    "plan_waves",
    "reconcile",
    "rollout",
]

"""Plugins: activation is role-based (a plugin without the right is not enabled), and scheduled hooks fire on time."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.config.raven import RavenConfig
from raven.core import plugin_stack
from raven.plugins.access import (
    AccessPolicy,
    GrantError,
    GrantStore,
    PluginAccessDeniedError,
    required_permissions,
    role_of,
    role_permissions,
)
from raven.plugins.discover import DiscoveredPlugin, ManifestOrigin
from raven.plugins.manifest import PluginManifest
from raven.plugins.schedule import PluginScheduler, ScheduleBook, ScheduledFire, latest_due, next_after

_MODULES: set[str] = set()


@pytest.fixture(autouse=True)
def _forget_modules():
    yield
    for name in _MODULES:
        sys.modules.pop(name, None)
    _MODULES.clear()


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


HOOK_MODULE = """
import pathlib

class Hook:
    def __init__(self, ctx):
        self.out = pathlib.Path(ctx.config["out"])

    async def run(self, fire):
        with self.out.open("a", encoding="utf-8") as f:
            f.write(f"{fire.key} {fire.planned_ms} {fire.coalesced} {fire.manual}\\n")
        return "wrote a line"

def make(ctx):
    return Hook(ctx)

def make_tool(ctx):
    return None
"""


def _plugin(root: Path, pid: str, *, version: str = "1.0.0", scheduled: bool = True, tools: bool = False) -> Path:
    """A plugin directory on disk: ``<root>/<pid>/raven-plugin.toml`` and its module."""
    mod = f"{pid.replace('-', '_')}_mod"
    _MODULES.add(mod)
    plug = root / pid
    plug.mkdir(parents=True, exist_ok=True)
    body = f'[plugin]\nid = "{pid}"\nversion = "{version}"\n'
    if scheduled:
        body += (
            "[[plugin.contributes.scheduled]]\n"
            f'name = "digest"\nfactory = "{mod}:make"\ncron = "30 9 * * *"\ntimezone = "UTC"\n'
        )
    if tools:
        body += f'[[plugin.contributes.tools]]\nname = "{pid}-tool"\nfactory = "{mod}:make_tool"\n'
    (plug / "raven-plugin.toml").write_text(body, encoding="utf-8")
    (plug / f"{mod}.py").write_text(HOOK_MODULE, encoding="utf-8")
    return plug


@pytest.fixture
def sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    found = {
        "bundled_dir": tmp_path / "none",
        "user_dir": tmp_path / "user_plugins",
        "project_dir": tmp_path / "repo" / ".raven" / "plugins",
        "entry_points_group": None,
    }
    monkeypatch.setattr(plugin_stack, "plugin_discovery_sources", lambda: dict(found))
    return found


def _config(**plugins: Any) -> RavenConfig:
    return RavenConfig.model_validate({"plugins": plugins})


# -- who may activate ---------------------------------------------------------------------------


def test_what_a_plugin_needs_is_read_off_what_it_contributes(tmp_path: Path) -> None:
    mf = PluginManifest.model_validate(
        {
            "id": "p",
            "version": "1",
            "contributes": {
                "tools": [{"name": "t", "factory": "m:f"}],
                "scheduled": [{"name": "s", "factory": "m:f", "cron": "0 * * * *"}],
            },
        }
    )
    assert required_permissions(mf) == {"tools", "schedule"}
    with pytest.raises(ValueError, match="five-field cron"):
        PluginManifest.model_validate(
            {
                "id": "p",
                "version": "1",
                "contributes": {"scheduled": [{"name": "s", "factory": "m:f", "cron": "often"}]},
            }
        )
    with pytest.raises(ValueError, match="unknown timezone"):
        PluginManifest.model_validate(
            {
                "id": "p",
                "version": "1",
                "contributes": {
                    "scheduled": [{"name": "s", "factory": "m:f", "cron": "0 * * * *", "timezone": "Mars/Base"}]
                },
            }
        )

    project = tmp_path / "repo" / ".raven" / "plugins"
    as_found = lambda source, where: DiscoveredPlugin(manifest=mf, source=source, location=where)  # noqa: E731
    assert role_of(as_found(ManifestOrigin.BUNDLED, None)) == "builtin"
    assert role_of(as_found(ManifestOrigin.USER, tmp_path / "u" / "p" / "raven-plugin.toml")) == "installed"
    assert role_of(as_found(ManifestOrigin.CHECKOUT, project / "p" / "raven-plugin.toml")) == "project"
    # A `plugins.dirs` root was named by the operator.
    assert role_of(as_found(ManifestOrigin.PROJECT, tmp_path / "named" / "p" / "raven-plugin.toml")) == "installed"
    # The manifest's own say-so does not make it builtin.
    claimed = PluginManifest.model_validate({"id": "p", "version": "1", "bundled": True})
    assert role_of(DiscoveredPlugin(manifest=claimed, source=ManifestOrigin.USER, location=None)) == "installed"


def test_a_plugin_without_the_right_to_activate_is_not_enabled(sources, tmp_path: Path) -> None:
    """A repository's plugin, and an installed plugin's scheduled hook, wait for a grant; a grant lets them in."""
    _plugin(sources["project_dir"], "repo-tool", scheduled=False, tools=True)
    _plugin(sources["user_dir"], "nightly")
    _plugin(sources["user_dir"], "plain-tool", scheduled=False, tools=True)
    config = _config()

    denied = plugin_stack.build_plugin_registry(config, notify=lambda _m: None)
    failures = {f.plugin_id: f.reason for f in denied.activation_failures()}

    assert denied.activated_ids() == ["plain-tool"]
    assert "needs tools" in failures["repo-tool"] and "role 'project'" in failures["repo-tool"]
    assert "needs schedule" in failures["nightly"] and "rarus plugin grant nightly" in failures["nightly"]
    assert denied.scheduled_keys() == [] and "repo-tool-tool" not in denied.tool_names()
    assert "repo_tool_mod" not in sys.modules, "nothing of a denied plugin is imported"

    found = {d.manifest.id: d for d in plugin_stack.discover_plugins(config)}
    GrantStore().grant(found["nightly"].manifest, ["schedule"], location=found["nightly"].location)
    GrantStore().grant(found["repo-tool"].manifest, ["tools"], location=found["repo-tool"].location)
    allowed = plugin_stack.build_plugin_registry(config, notify=lambda _m: None)

    assert allowed.activated_ids() == ["nightly", "plain-tool", "repo-tool"]
    assert allowed.scheduled_keys() == ["nightly:digest"]


def test_a_grant_is_exact_and_bound_to_the_version(sources) -> None:
    _plugin(sources["user_dir"], "nightly", tools=True)
    config = _config()
    found = {d.manifest.id: d for d in plugin_stack.discover_plugins(config)}["nightly"]
    mf = found.manifest

    with pytest.raises(GrantError, match="missing tools"):
        GrantStore().grant(mf, ["schedule"])
    with pytest.raises(GrantError, match="not requested: services"):
        GrantStore().grant(mf, ["schedule", "tools", "services"])
    GrantStore().grant(mf, ["schedule", "tools"], location=found.location)
    assert plugin_stack.build_plugin_registry(config, notify=lambda _m: None).activated_ids() == ["nightly"]

    _plugin(sources["user_dir"], "nightly", version="1.1.0", tools=True)  # an upgrade is a second consent
    upgraded = plugin_stack.build_plugin_registry(config, notify=lambda _m: None)
    assert upgraded.activated_ids() == []
    assert "the grant is for version 1.0.0, this is 1.1.0" in upgraded.activation_failures()[0].reason

    assert GrantStore().revoke("nightly") and not GrantStore().revoke("nightly")


def test_roles_are_configurable_and_a_bad_override_opens_nothing(sources, tmp_path: Path) -> None:
    _plugin(sources["project_dir"], "repo-tool", scheduled=False, tools=True)
    _plugin(sources["user_dir"], "plain-tool", scheduled=False, tools=True)

    opened = plugin_stack.build_plugin_registry(_config(roles={"project": ["tools"]}), notify=lambda _m: None)
    closed = plugin_stack.build_plugin_registry(_config(roles={"installed": []}), notify=lambda _m: None)
    typo = plugin_stack.build_plugin_registry(_config(roles={"project": ["everything"]}), notify=lambda _m: None)

    assert opened.activated_ids() == ["plain-tool", "repo-tool"]
    assert closed.activated_ids() == []
    assert typo.activated_ids() == ["plain-tool"]
    with pytest.raises(GrantError, match="unknown role"):
        role_permissions({"admins": ["tools"]})


def test_the_policy_raises_the_registry_s_own_error_type(tmp_path: Path) -> None:
    from raven.plugins.registry import PluginError

    mf = PluginManifest.model_validate(
        {"id": "p", "version": "1", "contributes": {"services": [{"name": "s", "factory": "m:f"}]}}
    )
    policy = AccessPolicy(grants=GrantStore(tmp_path / "g.json"))
    found = DiscoveredPlugin(manifest=mf, source=ManifestOrigin.CHECKOUT, location=tmp_path / "proj" / "p" / "x.toml")

    with pytest.raises(PluginAccessDeniedError) as caught:
        policy.check(found)
    assert isinstance(caught.value, PluginError)


# -- when scheduled hooks fire ---------------------------------------------------------------


class _Clock:
    def __init__(self, at: str) -> None:
        self.now = _ms(at)

    def set(self, at: str) -> None:
        self.now = _ms(at)

    def __call__(self) -> int:
        return self.now


class _Recorder:
    def __init__(self, behaviour: str = "ok") -> None:
        self.fires: list[ScheduledFire] = []
        self.behaviour = behaviour

    async def run(self, fire: ScheduledFire) -> str:
        self.fires.append(fire)
        if self.behaviour == "raise":
            raise RuntimeError("the digest service is down")
        if self.behaviour == "hang":
            await asyncio.sleep(10)
        return "done"


def _spec(cron: str = "30 9 * * *", *, missed: str = "latest", timeout_s: float = 5.0):
    from raven.plugins.manifest import ScheduledHookContribution

    return ScheduledHookContribution(
        name="digest", factory="m:f", cron=cron, timezone="UTC", missed=missed, timeout_s=timeout_s
    )


def _scheduler(tmp_path: Path, clock: _Clock, hook: _Recorder, **spec: Any) -> PluginScheduler:
    return PluginScheduler(
        {"nightly:digest": _spec(**spec)},
        lambda key: hook,
        versions={"nightly:digest": ("nightly", "1.0.0")},
        book=ScheduleBook(tmp_path / "schedules.json"),
        now_fn=clock,
    )


async def test_a_scheduled_hook_fires_at_its_time_and_only_once(tmp_path: Path) -> None:
    clock, hook = _Clock("2026-10-13T08:00:00"), _Recorder()
    sched = _scheduler(tmp_path, clock, hook)
    rows = sched.reconcile()
    assert rows["nightly:digest"]["next_run_ms"] == _ms("2026-10-13T09:30:00")

    clock.set("2026-10-13T09:29:59")
    early = await sched.tick()
    clock.set("2026-10-13T09:30:00")
    on_time = await sched.tick()
    clock.set("2026-10-13T09:30:40")
    again = await sched.tick()

    assert early == [] and on_time == [("nightly:digest", "ok")] and again == []
    assert [f.planned_ms for f in hook.fires] == [_ms("2026-10-13T09:30:00")]
    assert hook.fires[0].coalesced == 1 and not hook.fires[0].manual
    row = sched.book.rows()["nightly:digest"]
    assert row["next_run_ms"] == _ms("2026-10-14T09:30:00") and row["last_status"] == "ok"
    events = [r["event"] for r in sched.book.journal()]
    assert events == ["scheduled", "ok"]


async def test_missed_occurrences_coalesce_or_are_skipped(tmp_path: Path) -> None:
    clock, hook = _Clock("2026-10-13T08:00:00"), _Recorder()
    sched = _scheduler(tmp_path, clock, hook)
    sched.reconcile()
    clock.set("2026-10-16T12:00:00")  # down across three 09:30s

    assert await sched.tick() == [("nightly:digest", "ok")]
    assert hook.fires[-1].planned_ms == _ms("2026-10-16T09:30:00") and hook.fires[-1].coalesced == 4

    clock2, hook2 = _Clock("2026-10-13T08:00:00"), _Recorder()
    skipper = _scheduler(tmp_path / "skip", clock2, hook2, missed="skip")
    skipper.reconcile()
    clock2.set("2026-10-13T11:00:00")
    assert await skipper.tick() == [("nightly:digest", "skipped")] and hook2.fires == []
    clock2.set("2026-10-14T09:30:20")
    assert await skipper.tick() == [("nightly:digest", "ok")]


async def test_a_failing_or_hanging_hook_is_journalled_and_the_schedule_goes_on(tmp_path: Path) -> None:
    clock = _Clock("2026-10-13T09:29:00")
    failing = _scheduler(tmp_path / "a", clock, _Recorder("raise"))
    hanging = _scheduler(tmp_path / "b", clock, _Recorder("hang"), timeout_s=0.05)
    failing.reconcile(), hanging.reconcile()
    clock.set("2026-10-13T09:30:01")

    assert await failing.tick() == [("nightly:digest", "error")]
    assert await hanging.tick() == [("nightly:digest", "timeout")]
    assert "the digest service is down" in failing.book.rows()["nightly:digest"]["last_detail"]
    assert failing.book.rows()["nightly:digest"]["next_run_ms"] == _ms("2026-10-14T09:30:00")


def test_reconciling_keeps_an_unchanged_clock_and_restarts_a_changed_one(tmp_path: Path) -> None:
    book = ScheduleBook(tmp_path / "s.json")
    want = {"plugin_id": "nightly", "name": "digest", "version": "1.0.0", "cron": "30 9 * * *", "timezone": "UTC"}
    first = book.reconcile({"nightly:digest": want}, _ms("2026-10-13T08:00:00"))["nightly:digest"]
    upgraded = book.reconcile({"nightly:digest": {**want, "version": "1.0.1"}}, _ms("2026-10-13T08:10:00"))[
        "nightly:digest"
    ]
    moved = book.reconcile({"nightly:digest": {**want, "cron": "0 18 * * *"}}, _ms("2026-10-13T08:20:00"))[
        "nightly:digest"
    ]
    gone = book.reconcile({}, _ms("2026-10-13T08:30:00"))["nightly:digest"]
    back = book.reconcile({"nightly:digest": {**want, "cron": "0 18 * * *"}}, _ms("2026-10-13T08:40:00"))[
        "nightly:digest"
    ]

    assert upgraded["generation"] == first["generation"] and upgraded["version"] == "1.0.1"
    assert moved["generation"] != first["generation"] and moved["next_run_ms"] == _ms("2026-10-13T18:00:00")
    assert gone["enabled"] is False and gone["next_run_ms"] is None
    assert back["enabled"] is True and back["generation"] != moved["generation"]
    assert next_after("30 9 * * *", "Europe/Moscow", _ms("2026-10-13T00:00:00")) == _ms("2026-10-13T06:30:00")
    assert latest_due("0 * * * *", "UTC", _ms("2026-10-13T01:00:00"), _ms("2026-10-13T03:30:00")) == (
        _ms("2026-10-13T03:00:00"),
        3,
    )


async def test_two_schedulers_on_one_home_fire_an_occurrence_once(tmp_path: Path) -> None:
    clock = _Clock("2026-10-13T09:00:00")
    first, second = _Recorder(), _Recorder()
    a, b = _scheduler(tmp_path, clock, first), _scheduler(tmp_path, clock, second)
    a.reconcile(), b.reconcile()
    clock.set("2026-10-13T09:30:05")

    await asyncio.gather(a.tick(), b.tick())

    assert len(first.fires) + len(second.fires) == 1


# -- the main path: a granted plugin on disk, through the runtime and the command line ----------


async def test_a_granted_plugin_s_scheduled_hook_fires_in_a_resident_host(sources, tmp_path: Path, monkeypatch) -> None:
    from raven.config.schema import Config
    from raven.core import runtime
    from raven.providers.base import LLMProvider

    _plugin(sources["user_dir"], "nightly")
    out = tmp_path / "fired.txt"
    ec = _config(config={"nightly": {"out": str(out)}})
    found = {d.manifest.id: d for d in plugin_stack.discover_plugins(ec)}["nightly"]
    GrantStore().grant(found.manifest, ["schedule"], location=found.location)
    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, messages, **_: Any):  # pragma: no cover - no turn is taken
            raise AssertionError

        def get_default_model(self) -> str:
            return "fake/default"

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    rt = runtime.build_runtime(config, ec, provider=_Provider())
    clock = _Clock("2026-10-13T09:29:58")
    try:
        (sched,) = [s for s in rt.loop.plugin_services if isinstance(s, PluginScheduler)]
        sched._now = clock
        await rt.loop.start_plugin_services()
        assert not out.exists()
        clock.set("2026-10-13T09:30:00")
        sched._wake.set()
        for _ in range(200):
            if out.exists():
                break
            await asyncio.sleep(0.02)
    finally:
        await rt.dispose()

    assert out.read_text(encoding="utf-8").split() == ["nightly:digest", str(_ms("2026-10-13T09:30:00")), "1", "False"]


def test_the_command_line_shows_access_grants_and_runs_a_hook(sources, tmp_path: Path) -> None:
    from raven.cli.commands import app

    _plugin(sources["user_dir"], "nightly")
    out = tmp_path / "fired.txt"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"plugins": {"config": {"nightly": {"out": str(out)}}}}), encoding="utf-8")
    runner = CliRunner()
    c = ["--config", str(config_path)]

    before = runner.invoke(app, ["plugin", "access", *c])
    listed = runner.invoke(app, ["plugins", *c])
    preview = runner.invoke(app, ["plugin", "grant", "nightly", *c])
    wrong = runner.invoke(app, ["plugin", "grant", "nightly", "-p", "tools", *c])
    granted = runner.invoke(app, ["plugin", "grant", "nightly", "-p", "schedule", *c])
    after = runner.invoke(app, ["plugin", "access", *c])
    scheduled = runner.invoke(app, ["plugin", "schedule", *c])
    fired = runner.invoke(app, ["plugin", "fire", "nightly:digest", *c])
    runs = runner.invoke(app, ["plugin", "runs"])
    revoked = runner.invoke(app, ["plugin", "revoke", "nightly"])
    gone = runner.invoke(app, ["plugin", "fire", "nightly:digest", *c])

    flat = lambda r: "".join(r.output.split()).replace("│", "")  # noqa: E731 - rich wraps table cells
    assert "denied:needsschedule" in flat(before), before.output
    assert "failed" in listed.output and "needsschedule" in flat(listed)
    assert "ravenplugingrantnightly-pschedule" in flat(preview)
    assert wrong.exit_code == 1 and "missingschedule" in flat(wrong)
    assert granted.exit_code == 0 and "grantedscheduletonightly1.0.0" in flat(granted)
    assert "allowed" in after.output
    assert "nightly:digest" in flat(scheduled) and "309***" in flat(scheduled)
    assert fired.exit_code == 0 and "nightly:digest:ok" in flat(fired), fired.output
    assert out.read_text(encoding="utf-8").split()[-1] == "True"
    assert "manual" in runs.output and "scheduled" in runs.output
    assert revoked.exit_code == 0 and gone.exit_code == 1

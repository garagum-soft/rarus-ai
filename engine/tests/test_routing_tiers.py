"""Routing by complexity: a request's text picks a tier's model, and a failure escalates to a stronger one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.config.schema import Config, RoutingConfig
from raven.routing.complexity import assess
from raven.routing.tier_router import TierRouter

STACK = 'Traceback (most recent call last):\n  File "app/models.py", line 12, in save\n    raise ValueError("bad")\nValueError: bad\n'


@pytest.mark.parametrize(
    ("prompt", "tier"),
    [
        ("hi!", "simple"),
        ("Привет, спасибо!", "simple"),
        ("你好", "simple"),
        ("Translate 'good morning' into Russian", "simple"),
        ("переведи на английский: доброе утро", "simple"),
        ("Fix the failing test in raven/a.py", "standard"),
        ("Summarise what the release notes say about the new scheduler and who it affects", "standard"),
        (
            "Refactor the auth module across api.py, models.py and views.py, then update the tests and the docs",
            "complex",
        ),
        ("Design a migration plan from MySQL to Postgres for billing; think carefully about downtime", "complex"),
        ("Спроектируй архитектуру сервиса уведомлений и подумай хорошо о нагрузке", "complex"),
        (
            f"Why does this fail?\n```\n{STACK}```\nIt started after the last deploy, and nothing in models changed.",
            "standard",
        ),
    ],
)
def test_requests_land_in_the_tier_their_text_calls_for(prompt: str, tier: str) -> None:
    found = assess(prompt)

    assert found.tier == tier, found.summary()


def test_the_reasons_say_what_moved_the_score() -> None:
    found = assess("Refactor api.py, models.py and views.py step by step")

    assert {"names 3 files", "asks for careful thought"} <= set(found.reasons)
    assert any(r.startswith("asks to refactor") for r in found.reasons)


def test_where_the_tiers_split_is_a_setting() -> None:
    assert assess("Fix the failing test in raven/a.py", simple_below=0.5).tier == "simple"
    assert assess("Fix the failing test in raven/a.py", complex_from=0.35).tier == "complex"


def _routing(**tiers: str) -> RoutingConfig:
    return RoutingConfig(enabled=True, backend="tiers", tiers=tiers)


async def test_each_tier_gets_its_model_and_escalates_to_the_stronger_ones() -> None:
    router = TierRouter(_routing(simple="local/small", standard="cloud/mid", complex="cloud/large"), "cloud/default")

    easy = await router.select_model_chain("hi")
    usual = await router.select_model_chain("Fix the failing test in raven/a.py")
    hard = await router.select_model_chain("Design the sharding scheme and think carefully about rebalancing")

    assert easy == ("local/small", ["cloud/mid", "cloud/large", "cloud/default"])
    assert usual == ("cloud/mid", ["cloud/large", "cloud/default"])
    assert hard == ("cloud/large", ["cloud/default"])


async def test_an_empty_tier_borrows_the_next_stronger_model_or_the_default() -> None:
    only_cheap = TierRouter(_routing(simple="local/small"), "cloud/default")
    nothing = TierRouter(_routing(), "cloud/default")

    assert await only_cheap.select_model_chain("hi") == ("local/small", ["cloud/default"])
    assert await only_cheap.select_model_chain("Fix the failing test in raven/a.py") == ("cloud/default", [])
    assert await nothing.select_model_chain("hi") == ("cloud/default", [])


async def test_a_live_edit_routes_the_next_request() -> None:
    live = {"settings": _routing(simple="local/small")}
    router = TierRouter(_routing(simple="local/old"), "cloud/default", settings_source=lambda: live["settings"])

    before = await router.select_model_chain("hi")
    live["settings"] = _routing(simple="local/new")
    after = await router.select_model_chain("hi")
    live["settings"] = None  # the section left the file: the boot settings answer
    gone = await router.select_model_chain("hi")

    assert (before[0], after[0], gone[0]) == ("local/small", "local/new", "local/old")


def test_the_routing_section_is_read_live_and_a_bad_edit_keeps_the_last_good_one(tmp_path: Path) -> None:
    from raven.config.live import LiveConfig, routing_settings

    path = tmp_path / "config.json"
    live = LiveConfig(path)
    path.write_text(json.dumps({"routing": {"backend": "tiers", "tiers": {"simple": "a"}}}), encoding="utf-8")
    first = routing_settings(live)
    path.write_text(json.dumps({"routing": {"backend": "tiers", "simpleBelow": 7}}), encoding="utf-8")
    kept = routing_settings(live)
    path.write_text(json.dumps({}), encoding="utf-8")

    assert first.tiers.simple == "a" and kept is first and routing_settings(live) is None


def test_the_tiers_backend_is_built_only_when_on_and_given_a_model() -> None:
    from raven.core.provider_stack import build_model_routing

    provider = object()
    on = Config.model_validate({"routing": {"enabled": True, "backend": "tiers", "tiers": {"simple": "local/small"}}})
    empty = Config.model_validate({"routing": {"enabled": True, "backend": "tiers"}})
    off = Config.model_validate({"routing": {"backend": "tiers", "tiers": {"simple": "local/small"}}})

    router, same = build_model_routing(on, provider)

    assert isinstance(router, TierRouter) and same is provider
    assert build_model_routing(empty, provider) == (None, provider)
    assert build_model_routing(off, provider) == (None, provider)
    assert RoutingConfig().enabled is False, "routing stays off unless asked for"


async def test_a_failing_cheap_model_escalates_within_the_same_turn(tmp_path: Path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.contracts.llm_provider import LLMResponse
    from raven.providers.base import LLMProvider
    from tests.test_agent_loop_model_selection import _req, _stub_edges

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="k")
            self.models: list[str] = []

        async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
            self.models.append(model)
            if model == "local/small":
                return LLMResponse(content="Error: 429 rate limit exceeded", finish_reason="error")
            return LLMResponse(content=f"answered by {model}")

        def get_default_model(self) -> str:
            return "cloud/default"

        @property
        def name(self) -> str:
            return "stub"

    provider = _Provider()
    router = TierRouter(_routing(simple="local/small", standard="cloud/mid"), "cloud/default")
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="cloud/default", router=router)
    _stub_edges(loop)

    reply = await loop._process_message(_req("hi there", conversation="web:tiers"))

    assert provider.models[0] == "local/small" and provider.models[-1] == "cloud/mid"
    assert "answered by cloud/mid" in str(getattr(reply, "content", reply))


def test_the_command_line_explains_a_route_and_the_switch_to_turn_it_on(tmp_path: Path) -> None:
    from raven.cli.commands import app
    from raven.config.loader import set_config_path

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"routing": {"backend": "tiers", "tiers": {"simple": "local/small", "complex": "cloud/large"}}}),
        encoding="utf-8",
    )
    set_config_path(path)
    try:
        runner = CliRunner()
        status = runner.invoke(app, ["routing", "status"])
        easy = runner.invoke(app, ["routing", "explain", "hi", "--json"])
        hard = runner.invoke(app, ["routing", "explain", "Refactor a.py, b.py and c.py; think hard"])
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    assert "routing: off (backend tiers)" in status.output and "routing.enabled = true" in status.output
    shown = json.loads(easy.output)
    assert shown["tier"] == "simple" and shown["model"] == "local/small" and shown["routing_on"] is False
    assert "tier: complex" in hard.output and "model: cloud/large" in hard.output
    assert "not in force now" in hard.output

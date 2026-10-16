"""``employees.*`` over RPC: the page's hire, card and retire, and the roster pushed live after each."""

from __future__ import annotations

import json

import pytest

from raven.rpc.errors import InvalidParamsError
from raven.rpc.methods import employees as rpc


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    home = tmp_path / "raven-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"openai": {"api_key": "host-key"}},
                "agents": {"defaults": {"provider": "openai", "model": "gpt-4o"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RAVEN_HOME", str(home))
    from raven.config.loader import set_config_path

    set_config_path(home / "config.json")
    yield home
    set_config_path(None)  # type: ignore[arg-type]


class _Loop:
    def __init__(self) -> None:
        self.applied: list[list] = []

    def apply_agents(self, configs) -> None:
        self.applied.append(list(configs))


async def test_a_hire_from_the_page_lands_on_the_live_roster_and_its_key_never_comes_back() -> None:
    loop = _Loop()

    hired = await rpc.employees_hire(
        {
            "name": "Anna",
            "role": "Writes copy.",
            "system_prompt": "You are Anna.",
            "provider": "openrouter",
            "model": "m/x",
            "api_key": "sk-secret-777",
        },
        agent_loop_factory=lambda: loop,
    )
    listed = await rpc.employees_list({})

    assert hired["employee"]["name"] == "Anna" and hired["employee"]["own_key"] is True
    assert "sk-secret-777" not in json.dumps(hired) and "sk-secret-777" not in json.dumps(listed)
    assert [e["name"] for e in listed["employees"]] == ["Anna"]
    # providers.auth's verdict: a key in the config, or a subscription this machine is signed in to.
    assert "openai" in listed["providers"] and "groq" not in listed["providers"]
    assert "anthropic" in listed["all_providers"]
    assert len(loop.applied) == 1, "the roster is pushed into the running loop"


async def test_the_card_reads_and_replaces_the_prompt_and_retires() -> None:
    loop = _Loop()
    await rpc.employees_hire(
        {"name": "Anna", "role": "r", "system_prompt": "You are Anna."}, agent_loop_factory=lambda: loop
    )

    before = await rpc.employees_prompt_get({"name": "Anna"})
    await rpc.employees_prompt_set({"name": "Anna", "prompt": "You are Anna. Be brief."})
    after = await rpc.employees_prompt_get({"name": "Anna"})
    retired = await rpc.employees_retire({"name": "Anna"}, agent_loop_factory=lambda: loop)

    assert before["prompt"].strip() == "You are Anna." and after["prompt"].strip() == "You are Anna. Be brief."
    assert ".retired" in retired["kept"] and (await rpc.employees_list({}))["employees"] == []
    assert len(loop.applied) == 2


async def test_a_hire_that_cannot_be_made_is_an_invalid_params_error() -> None:
    with pytest.raises(InvalidParamsError, match="say what the employee does"):
        await rpc.employees_hire({"name": "Anna", "role": "", "system_prompt": "p"})
    with pytest.raises(InvalidParamsError, match="no employee"):
        await rpc.employees_prompt_get({"name": "Nobody"})


def test_the_methods_are_registered_and_in_the_contract() -> None:
    from raven.rpc.models import METHOD_MODELS

    names = {"employees.list", "employees.hire", "employees.prompt_get", "employees.prompt_set", "employees.retire"}
    registered: dict = {}

    class _Dispatcher:
        def register(self, name, handler) -> None:
            registered[name] = handler

    rpc.register_employees_methods(_Dispatcher())

    assert set(registered) == names and names <= set(METHOD_MODELS)

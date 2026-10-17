"""``org.get`` / ``org.set``: the agent canvas's reading and writing of ``subagents.org``."""

from __future__ import annotations

import json

import pytest

from raven.agent.subagent import org_store
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods.org import org_get, org_set, register_org_methods


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _rows(*pairs):
    return [{"name": name, "reports_to": boss} for name, boss in pairs]


async def test_an_empty_chart_is_a_flat_roster():
    assert await org_get({}) == {"members": [], "max_depth": org_store.MAX_DEPTH}


async def test_a_saved_chart_is_read_back_and_written_the_way_the_config_spells_it(tmp_path):
    await org_set({"members": _rows(("lead", None), ("dev", "lead"), ("qa", "dev"))})

    got = await org_get({})
    assert [(m["name"], m["reports_to"]) for m in got["members"]] == [("lead", None), ("dev", "lead"), ("qa", "dev")]
    raw = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
    assert raw["subagents"]["org"][1] == {"name": "dev", "reportsTo": "lead", "department": "", "title": ""}


async def test_a_hand_written_camel_case_row_is_read_too(tmp_path):
    (tmp_path / "home" / "config.json").write_text(
        json.dumps({"subagents": {"org": [{"name": "a"}, {"name": "b", "reportsTo": "a", "title": "Dev"}]}}),
        encoding="utf-8",
    )
    members = (await org_get({}))["members"]
    assert members[1] == {"name": "b", "reports_to": "a", "department": "", "title": "Dev"}


async def test_the_rest_of_the_config_is_left_alone(tmp_path):
    (tmp_path / "home" / "config.json").write_text(json.dumps({"language": "ru"}), encoding="utf-8")
    await org_set({"members": _rows(("a", None))})
    assert json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))["language"] == "ru"


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        (_rows(("a", "b"), ("b", "a")), "cycle"),
        (_rows(("a", "a")), "itself"),
        (_rows(("a", "ghost")), "not in the chart"),
        (_rows(("a", None), ("a", None)), "twice"),
        ([{"name": " "}], "needs a name"),
    ],
)
async def test_a_chart_that_is_not_a_forest_is_refused_and_nothing_is_written(rows, why, tmp_path):
    with pytest.raises(ConfigValidationError) as caught:
        await org_set({"members": rows})
    assert why in str(caught.value)
    assert not (tmp_path / "home" / "config.json").exists()


async def test_six_levels_are_allowed_and_a_seventh_is_not():
    names = [f"n{i}" for i in range(7)]
    chain = [(names[0], None)] + [(names[i], names[i - 1]) for i in range(1, 6)]
    await org_set({"members": _rows(*chain)})

    with pytest.raises(ConfigValidationError) as caught:
        await org_set({"members": _rows(*chain, (names[6], names[5]))})
    assert "7 levels" in str(caught.value)
    assert len((await org_get({}))["members"]) == 6


async def test_members_must_be_a_list():
    with pytest.raises(ConfigValidationError):
        await org_set({"members": "nope"})


async def test_the_methods_are_registered():
    dispatcher = Dispatcher()
    register_org_methods(dispatcher)
    reply = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "org.get", "params": {}})
    assert reply["result"]["members"] == []

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
    assert await org_get({}) == {"members": [], "companies": [], "max_depth": org_store.MAX_DEPTH}


async def test_a_saved_chart_is_read_back_and_written_the_way_the_config_spells_it(tmp_path):
    await org_set({"members": _rows(("lead", None), ("dev", "lead"), ("qa", "dev"))})

    got = await org_get({})
    assert [(m["name"], m["reports_to"]) for m in got["members"]] == [("lead", None), ("dev", "lead"), ("qa", "dev")]
    raw = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
    assert raw["subagents"]["org"][1] == {
        "name": "dev", "reportsTo": "lead", "department": "", "title": "", "company": "", "avatar": ""
    }


async def test_a_hand_written_camel_case_row_is_read_too(tmp_path):
    (tmp_path / "home" / "config.json").write_text(
        json.dumps({"subagents": {"org": [{"name": "a"}, {"name": "b", "reportsTo": "a", "title": "Dev"}]}}),
        encoding="utf-8",
    )
    members = (await org_get({}))["members"]
    assert members[1] == {"name": "b", "reports_to": "a", "department": "", "title": "Dev", "company": "", "avatar": ""}


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


# -- companies and departments (the agent canvas, 2026-10-19) -----------------------------------

def _company(**over):
    base = {"id": "c1", "name": "Acme", "departments": [{"name": "Dev", "head": ""}]}
    base.update(over)
    return base


async def test_companies_are_saved_with_the_chart_and_read_back(tmp_path):
    rows = [
        {"name": "boss", "company": "c1"},
        {"name": "lead", "company": "c1", "department": "Dev", "reports_to": "boss", "avatar": "a03"},
        {"name": "dev", "company": "c1", "department": "Dev", "reports_to": "lead"},
    ]
    company = _company(head="boss", departments=[{"name": "Dev", "head": "lead", "background": "wood"}], frozen=True)
    got = await org_set({"members": rows, "companies": [company]})
    assert got["companies"][0]["departments"] == [{"name": "Dev", "head": "lead", "background": "wood"}]
    back = await org_get({})
    assert back["companies"][0]["head"] == "boss" and back["companies"][0]["frozen"] is True
    assert back["members"][1]["avatar"] == "a03" and back["members"][1]["company"] == "c1"
    raw = json.loads((tmp_path / "home" / "config.json").read_text(encoding="utf-8"))
    assert raw["subagents"]["companies"][0]["id"] == "c1"


async def test_a_chart_sent_without_companies_keeps_the_ones_on_file():
    await org_set({"members": [{"name": "a", "company": "c1"}], "companies": [_company()]})
    await org_set({"members": [{"name": "a", "company": "c1"}, {"name": "b"}]})
    assert [c["id"] for c in (await org_get({}))["companies"]] == ["c1"]


@pytest.mark.parametrize(
    ("rows", "companies", "why"),
    [
        ([{"name": "a", "company": "ghost"}], [], "not there"),
        ([{"name": "a", "company": "c1", "department": "Ops"}], [_company()], "has not got"),
        ([{"name": "a"}], [_company(head="a")], "must work in it"),
        ([{"name": "a", "company": "c1"}], [_company(departments=[{"name": "Dev", "head": "a"}])], "that department"),
        ([], [_company(), _company()], "share the id"),
        ([], [_company(departments=[{"name": "Dev"}, {"name": "Dev"}])], "two departments"),
        ([], [_company(logo="https://example.com/x.png")], "small picture"),
        ([], [_company(name="")], "characters"),
    ],
)
async def test_a_membership_that_does_not_add_up_is_refused_and_nothing_is_written(rows, companies, why):
    with pytest.raises(ConfigValidationError, match=why):
        await org_set({"members": rows, "companies": companies})
    assert (await org_get({}))["companies"] == []


async def test_a_saved_chart_reaches_the_running_agent():
    seen = []

    class _Manager:
        def apply_org(self, members, companies):
            seen.append(([m["name"] for m in members], [c["id"] for c in companies]))

    class _Loop:
        subagents = _Manager()

    await org_set({"members": [{"name": "a", "company": "c1"}], "companies": [_company()]}, agent_loop_factory=_Loop)
    assert seen == [(["a"], ["c1"])]

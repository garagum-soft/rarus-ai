"""The roster's reporting structure: managers, departments, and assignment from above."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.manager import SubagentManager
from raven.agent.subagent.org import OrgChart, OrgChartError, OrgMember
from raven.agent.subagent.spawn_tool import SpawnTool
from raven.config.schema import OrgMemberConfig, SubagentsConfig, ThirdPartyCliSubagentConfig


def _chart() -> OrgChart:
    return OrgChart(
        [
            OrgMember("Lead", None, "Engineering", "Head of engineering"),
            OrgMember("Dev", "Lead", "Engineering"),
            OrgMember("Intern", "Dev", "Engineering"),
            OrgMember("Designer", "Lead", "Design"),
            OrgMember("Outsider", None, "Sales"),
        ]
    )


class TestTheChart:
    def test_it_answers_who_reports_to_whom(self) -> None:
        chart = _chart()

        assert chart.manager_of("Intern") == "Dev"
        assert chart.direct_reports("Lead") == ["Dev", "Designer"] or chart.direct_reports("Lead") == [
            "Designer",
            "Dev",
        ]
        assert chart.subordinates("Lead") == ["Designer", "Dev", "Intern"]
        assert chart.chain_of_command("Intern") == ["Dev", "Lead"]
        assert chart.subordinates("Intern") == []

    def test_departments_group_members(self) -> None:
        chart = _chart()

        assert chart.departments() == ["Design", "Engineering", "Sales"]
        assert chart.department_members("Engineering") == ["Dev", "Intern", "Lead"]
        assert chart.department_of("Designer") == "Design"

    def test_a_manager_assigns_down_at_any_depth_and_only_down(self) -> None:
        chart = _chart()

        assert chart.refusal_for_assign("Lead", "Dev") is None
        assert chart.refusal_for_assign("Lead", "Intern") is None
        assert "assigned from above" in (chart.refusal_for_assign("Intern", "Lead") or "")
        assert "not below" in (chart.refusal_for_assign("Dev", "Designer") or "")
        assert "not below" in (chart.refusal_for_assign("Lead", "Outsider") or "")
        assert "itself" in (chart.refusal_for_assign("Dev", "Dev") or "")

    def test_what_a_member_may_see_is_its_line_of_command_and_everything_below(self) -> None:
        chart = _chart()

        assert chart.visible_to("Dev") == ["Dev", "Intern", "Lead"]
        assert chart.visible_to("Outsider") == ["Outsider"]
        assert chart.visible_to("Nobody") == []

    def test_a_chart_that_is_not_a_forest_is_refused(self) -> None:
        with pytest.raises(OrgChartError, match="cycle"):
            OrgChart([OrgMember("a", "b"), OrgMember("b", "a")])
        with pytest.raises(OrgChartError, match="itself"):
            OrgChart([OrgMember("a", "a")])
        with pytest.raises(OrgChartError, match="not in the chart"):
            OrgChart([OrgMember("a", "ghost")])
        with pytest.raises(OrgChartError, match="twice"):
            OrgChart([OrgMember("a"), OrgMember("a")])

    def test_an_empty_chart_assigns_nothing(self) -> None:
        assert "no org chart" in (OrgChart().refusal_for_assign("a", "b") or "")


class TestTheConfig:
    def test_rows_load_with_their_camel_case_spelling(self) -> None:
        cfg = SubagentsConfig.model_validate(
            {"org": [{"name": "A"}, {"name": "B", "reportsTo": "A", "department": "X"}]}
        )

        chart = OrgChart.from_rows(cfg.org)
        assert chart.manager_of("B") == "A" and chart.department_of("B") == "X"

    def test_bad_rows_are_repaired_not_fatal(self) -> None:
        cfg = SubagentsConfig(
            org=[
                OrgMemberConfig(name="A", reports_to="B"),
                OrgMemberConfig(name="B", reports_to="A"),
                OrgMemberConfig(name="C", reports_to="ghost"),
                OrgMemberConfig(name="D", reports_to="D"),
                OrgMemberConfig(name="A", reports_to="C"),
            ]
        )

        chart = OrgChart.from_rows(cfg.org)
        assert [m.name for m in chart.members()] == ["A", "B", "C", "D"]
        assert chart.manager_of("C") is None and chart.manager_of("D") is None
        assert chart.manager_of("A") is None or chart.manager_of("B") is None, "one edge of the cycle is cut"


class _Recorder:
    streams = False

    def __init__(self, seen: list[tuple[str, str]], name: str) -> None:
        self.seen, self.name = seen, name

    async def run(self, task: str, **kwargs: Any) -> str:
        self.seen.append((self.name, task))
        return "done"


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chart: OrgChart) -> tuple[SpawnTool, list[tuple[str, str]]]:
    from raven.session.manager import SessionManager

    home = tmp_path / "home"
    names = ["Lead", "Dev", "Intern", "Designer", "Outsider"]
    mgr = SubagentManager(
        provider=_StubProvider(),  # type: ignore[arg-type]
        workspace=home,
        session_dir=lambda key: SessionManager(home).session_dir(key),
        agents=[ThirdPartyCliSubagentConfig(name=n, command="cat {prompt}") for n in names],
    )
    mgr.set_submit(lambda _s: None)
    seen: list[tuple[str, str]] = []
    for n in names:
        mgr.registry._backends[n] = _Recorder(seen, n)
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())
    monkeypatch.setattr(mgr, "org_chart", lambda: chart)
    tool = SpawnTool(manager=mgr)
    tool.set_context("cli", "direct", "cli:direct")
    return tool, seen


async def _spawn(tool: SpawnTool, node: str, agent: str, **extra: Any) -> str:
    return await tool.execute(
        task_summary="do it", node_id=node, prompt_template="write the module", subagent=agent, **extra
    )


async def _settle(seen: list[tuple[str, str]], count: int) -> None:
    for _ in range(400):
        if len(seen) >= count:
            return
        await asyncio.sleep(0.01)


async def test_a_manager_assigns_a_task_to_a_subordinate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, seen = _rig(tmp_path, monkeypatch, _chart())

    out = await _spawn(tool, "n1", "Intern", assigned_by="Lead")
    await _settle(seen, 1)

    assert not out.startswith("Error:"), out
    assert seen and seen[0][0] == "Intern"
    assert seen[0][1].startswith("[This task was assigned to you by your manager, Lead (Engineering).]")
    assert "write the module" in seen[0][1]


async def test_a_subordinate_cannot_assign_upward_or_across_the_hierarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, seen = _rig(tmp_path, monkeypatch, _chart())

    upward = await _spawn(tool, "n1", "Lead", assigned_by="Intern")
    across = await _spawn(tool, "n2", "Designer", assigned_by="Dev")
    outside = await _spawn(tool, "n3", "Outsider", assigned_by="Lead")
    unknown = await _spawn(tool, "n4", "Dev", assigned_by="Ghost")
    await asyncio.sleep(0.05)

    assert "assigned from above" in upward
    assert "not below" in across and "not below" in outside
    assert "not in the org chart" in unknown
    assert seen == [], "a refused assignment dispatches nothing"


async def test_a_spawn_without_a_manager_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, seen = _rig(tmp_path, monkeypatch, _chart())

    out = await _spawn(tool, "n1", "Outsider")
    await _settle(seen, 1)

    assert not out.startswith("Error:") and seen[0][1].startswith("write the module")


def test_the_schema_offers_assigned_by_only_when_a_chart_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_chart, _ = _rig(tmp_path, monkeypatch, _chart())
    without, _ = _rig(tmp_path / "b", monkeypatch, OrgChart())

    assert "assigned_by" in with_chart.parameters["properties"]
    assert "assigned_by" not in without.parameters["properties"]


def test_the_manager_takes_its_chart_from_the_config_rows(tmp_path: Path) -> None:
    rows = [OrgMemberConfig(name="Lead"), OrgMemberConfig(name="Dev", reports_to="Lead", department="Engineering")]
    mgr = SubagentManager(provider=_StubProvider(), workspace=tmp_path, org=rows)  # type: ignore[arg-type]

    assert mgr.org_chart().manager_of("Dev") == "Lead"
    mgr.apply_org([OrgMember("a", "b"), OrgMember("b", "a")])
    assert not mgr.org_chart(), "a set of rows that is not a forest leaves the roster flat"


class TestCompanies:
    """Who may hand work to whom inside a company (the agent canvas, 2026-10-19)."""

    def _chart(self, frozen=False):
        rows = [
            {"name": "ceo", "company": "c1"},
            {"name": "pm", "company": "c1", "reports_to": "ceo"},
            {"name": "devlead", "company": "c1", "department": "Dev", "reports_to": "ceo"},
            {"name": "dev", "company": "c1", "department": "Dev", "reports_to": "devlead"},
            {"name": "qa", "company": "c1", "department": "QA"},
            {"name": "other", "company": "c2"},
            {"name": "loner"},
        ]
        companies = [
            {"id": "c1", "name": "Acme", "head": "ceo", "frozen": frozen,
             "departments": [{"name": "Dev", "head": "devlead"}, {"name": "QA", "head": ""}]},
            {"id": "c2", "name": "Other"},
        ]
        return OrgChart.from_rows(rows, companies)

    def test_a_department_talks_inside_itself(self) -> None:
        chart = self._chart()
        assert chart.may_talk("dev", "devlead") and chart.may_talk("devlead", "dev")

    def test_the_company_level_the_head_and_department_heads_talk_to_each_other(self) -> None:
        chart = self._chart()
        assert chart.may_talk("ceo", "pm") and chart.may_talk("pm", "devlead") and chart.may_talk("devlead", "ceo")

    def test_a_department_member_does_not_talk_past_its_head(self) -> None:
        chart = self._chart()
        assert not chart.may_talk("dev", "pm") and not chart.may_talk("ceo", "dev") and not chart.may_talk("dev", "qa")
        assert "devlead" in chart.refusal_for_assign("pm", "dev")

    def test_work_does_not_cross_companies_or_leave_one(self) -> None:
        chart = self._chart()
        assert "one company" in chart.refusal_for_assign("ceo", "other")
        assert "one company" in chart.refusal_for_assign("loner", "pm")

    def test_assigning_inside_a_company_follows_who_talks_to_whom(self) -> None:
        chart = self._chart()
        assert chart.refusal_for_assign("pm", "devlead") is None
        assert chart.refusal_for_assign("devlead", "dev") is None
        assert chart.talks_with("devlead") == ["ceo", "dev", "pm"]

    def test_a_frozen_company_takes_no_work(self) -> None:
        chart = self._chart(frozen=True)
        assert "frozen" in chart.refusal_for_target("dev")
        assert "frozen" in chart.refusal_for_assign("devlead", "dev")
        assert chart.refusal_for_target("other") is None and chart.refusal_for_target("loner") is None

    def test_members_outside_companies_keep_the_reporting_rule(self) -> None:
        chart = OrgChart.from_rows([{"name": "a"}, {"name": "b", "reports_to": "a"}])
        assert chart.refusal_for_assign("a", "b") is None
        assert chart.refusal_for_assign("b", "a") is not None


class TestCompaniesInTheConfig:
    def test_a_membership_in_a_missing_company_or_department_is_dropped_not_fatal(self) -> None:
        cfg = SubagentsConfig.model_validate({
            "org": [
                {"name": "a", "company": "ghost"},
                {"name": "b", "company": "c1", "department": "Nope"},
                {"name": "c", "company": "c1", "department": "Dev"},
            ],
            "companies": [{"id": "c1", "name": "Acme", "head": "zed", "departments": [{"name": "Dev", "head": "c"}]}],
        })
        rows = {r.name: r for r in cfg.org}
        assert (rows["a"].company, rows["b"].department, rows["c"].department) == ("", "", "Dev")
        assert cfg.companies[0].head == "" and cfg.companies[0].departments[0].head == "c"

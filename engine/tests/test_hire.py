"""Hiring an employee: one step from a name, a role, a model and a prompt to an agent on the roster."""

from __future__ import annotations

import importlib.util
import json
import py_compile
from pathlib import Path

import pytest

from raven.agent.subagent.hire import (
    Hire,
    HireError,
    employees,
    folder_id,
    hire,
    retire,
    set_system_prompt,
    system_prompt,
)
from raven.agent.subagent.vendored_agents import discover_product_rows, employees_root


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    home = tmp_path / "raven-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"openai": {"apiKey": "host-openai-key"}},
                "agents": {"defaults": {"provider": "openai", "model": "gpt-4o"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RAVEN_HOME", str(home))
    for name in ("RAVEN_PARENT_MODEL", "RAVEN_PARENT_PROVIDER", "RAVEN_PARENT_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def _products(tmp_path, monkeypatch):
    """A products tree of two folders, which the suite otherwise hides (conftest's agents_root)."""
    tree = tmp_path / "products"
    for folder, name in (("raven-code", "Raven-Code"), ("raven-research", "Raven-Research")):
        (tree / folder).mkdir(parents=True)
        (tree / folder / "run.py").write_text("print('product')\n", encoding="utf-8")
        manifest = {
            "name": name,
            "kind": "acp",
            "description": f"{name} product",
            "command": "{PYTHON} {SUBAGENT_DIR}/run.py --acp",
            "cwd": "{SUBAGENT_DIR}",
        }
        (tree / folder / "subagent.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr("raven.agent.subagent.vendored_agents.agents_root", lambda: tree)
    return tree


def _spec(**overrides) -> Hire:
    values = {
        "name": "Anna K.",
        "role": "Writes and edits marketing copy; pick her for landing pages and announcements.",
        "system_prompt": "You are Anna, a careful copywriter. Short sentences, no jargon.",
        "model": "gpt-4o-mini",
        "provider": "openai",
    }
    values.update(overrides)
    return Hire(**values)


def test_a_folder_id_is_safe_and_readable() -> None:
    assert folder_id("Anna K.") == "anna-k"
    assert folder_id("  Data   Wizard 2 ") == "data-wizard-2"
    assert folder_id("42") == "employee-42"


def test_hiring_writes_an_agent_the_roster_discovers_beside_the_products() -> None:
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    folder = hire(_spec())

    assert folder == employees_root() / "anna-k"
    manifest = json.loads((folder / "subagent.json").read_text(encoding="utf-8"))
    row = ThirdPartyAcpSubagentConfig.model_validate(manifest)
    assert row.name == "Anna K." and row.model == "gpt-4o-mini" and "landing pages" in row.description
    assert (folder / "soul.md").read_text(encoding="utf-8").startswith("You are Anna")
    assert not (folder / ".env").exists()
    py_compile.compile(str(folder / "run.py"), doraise=True, cfile=str(folder.parent / "run.pyc"))
    assert 'getattr(render, "exec_raven_acp", None)' in (folder / "run.py").read_text(encoding="utf-8")
    discovered = {str(r.name): r for r in discover_product_rows()}
    assert discovered["Anna K."].enabled and str(folder) in str(discovered["Anna K."].command)
    shipped = {"Raven-Code", "Raven-Research"}
    assert shipped <= set(discovered), "a hire hides none of the products"


def test_a_key_of_its_own_stays_in_its_env_file() -> None:
    folder = hire(_spec(name="Bo", api_key="sk-own-123", provider="openrouter", model="anthropic/claude-sonnet-4.5"))

    env = (folder / ".env").read_text(encoding="utf-8")
    assert "EMP_BO_API_KEY=sk-own-123" in env
    for name in ("subagent.json", "config.json", "employee.json", "run.py", "soul.md"):
        assert "sk-own-123" not in (folder / name).read_text(encoding="utf-8"), name
    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"] == {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"}
    (listed,) = [e for e in employees() if e.name == "Bo"]
    assert listed.own_key and listed.model == "anthropic/claude-sonnet-4.5"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"role": " "}, "say what the employee does"),
        ({"system_prompt": ""}, "system prompt"),
        ({"api_key": "k", "model": ""}, "needs the provider"),
        ({"provider": "no-such-vendor"}, "unknown provider"),
        ({"name": "Raven"}, "already on the roster"),
        ({"name": "Raven-Code"}, "already on the roster"),
        ({"name": "!!!"}, "latin letter"),
    ],
)
def test_a_hire_that_cannot_be_made_says_why(overrides, message) -> None:
    with pytest.raises(HireError, match=message):
        hire(_spec(**overrides))


def test_one_name_is_hired_once() -> None:
    hire(_spec())

    with pytest.raises(HireError, match="already on the roster"):
        hire(_spec())


def _load_launcher(folder: Path):
    spec = importlib.util.spec_from_file_location(f"launcher_{folder.name.replace('-', '_')}", folder / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launcher_runs_its_own_model_on_the_hosts_keys_and_seeds_its_prompt() -> None:
    folder = hire(_spec())
    launcher = _load_launcher(folder)

    rendered = json.loads(Path(launcher.render_config()).read_text(encoding="utf-8"))

    defaults = rendered["agents"]["defaults"]
    assert defaults["model"] == "gpt-4o-mini" and defaults["provider"] == "openai"
    assert rendered["providers"]["openai"]["apiKey"] == "host-openai-key"
    soul = Path(defaults["workspace"]) / "agent_memory" / "profile" / "soul.md"
    assert soul.read_text(encoding="utf-8").startswith("You are Anna")


def test_the_launcher_uses_the_employees_own_key_when_it_has_one() -> None:
    folder = hire(_spec(name="Bo", api_key="sk-own-123", provider="openrouter", model="m/x"))
    launcher = _load_launcher(folder)

    rendered = json.loads(Path(launcher.render_config()).read_text(encoding="utf-8"))

    assert rendered["providers"]["openrouter"]["apiKey"] == "sk-own-123"
    assert rendered["agents"]["defaults"]["model"] == "m/x"
    assert "openai" not in rendered.get("providers", {}), "the host's keys are not taken with an own key"


def test_the_prompt_is_read_and_replaced_with_history_once_it_has_run() -> None:
    folder = hire(_spec())
    assert system_prompt("Anna K.").startswith("You are Anna")
    launcher = _load_launcher(folder)
    rendered = json.loads(Path(launcher.render_config()).read_text(encoding="utf-8"))
    live = Path(rendered["agents"]["defaults"]["workspace"]) / "agent_memory" / "profile" / "soul.md"

    set_system_prompt("anna-k", "You are Anna. Be brief.")

    assert (folder / "soul.md").read_text(encoding="utf-8").strip() == "You are Anna. Be brief."
    assert live.read_text(encoding="utf-8").strip() == "You are Anna. Be brief."
    assert system_prompt("Anna K.").strip() == "You are Anna. Be brief."


def test_retiring_takes_the_employee_off_the_roster_and_keeps_the_folder() -> None:
    hire(_spec())

    kept = retire("Anna K.")

    assert kept.is_dir() and (kept / "soul.md").is_file()
    assert "Anna K." not in {str(r.name) for r in discover_product_rows()}
    assert employees() == []
    with pytest.raises(HireError, match="no employee"):
        retire("Anna K.")


def test_with_no_hire_discovery_is_what_it_was(tmp_path) -> None:
    assert not employees_root().exists()
    names = [str(r.name) for r in discover_product_rows()]

    assert "Raven-Code" in names and len(names) == len(set(names))

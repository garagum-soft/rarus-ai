"""``employees.*``: hiring an employee from the page, and its card afterwards.

The same one step as ``rarus agents hire`` (:mod:`raven.agent.subagent.hire`):
a name, a role, a model on the host's keys or on a key of its own, and a system
prompt. The roster is pushed into the live runtime after each change, so the
main agent can hand work to a new hire in its next turn. A key is written into
the employee's ``.env`` and never sent back.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from raven.rpc.errors import InvalidParamsError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


def _configured_providers() -> list[str]:
    from raven.config.loader import load_config

    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - an unreadable config offers no providers; hiring on an own key still works
        return []
    from raven.providers.auth import credential_status

    names = []
    for name in type(config.providers).model_fields:
        section = config.providers.get(name)
        # Whether a provider is set up is providers.auth's to say, a subscription's login included.
        if section is not None and credential_status(name, section).ok:
            names.append(name)
    return sorted(names)


def _all_providers() -> list[str]:
    from raven.config.schema import Config

    return sorted(type(Config().providers).model_fields)


def _wire(employee: Any) -> dict[str, Any]:
    return employee.to_wire()


async def employees_list(params: dict) -> dict:
    from raven.agent.subagent.hire import employees

    rows = await asyncio.to_thread(employees)
    return {
        "employees": [_wire(e) for e in rows],
        "providers": _configured_providers(),
        "all_providers": _all_providers(),
    }


def _refresh(agent_loop_factory: AgentLoopFactory | None) -> None:
    from raven.rpc.methods.subagents import _hot_apply

    _hot_apply(agent_loop_factory)


async def employees_hire(params: dict, *, agent_loop_factory: AgentLoopFactory | None = None) -> dict:
    from raven.agent.subagent.hire import Hire, HireError, employees, hire

    spec = Hire(
        name=str(params.get("name") or ""),
        role=str(params.get("role") or ""),
        system_prompt=str(params.get("system_prompt") or ""),
        owns=str(params.get("owns") or ""),
        provider=str(params.get("provider") or ""),
        model=str(params.get("model") or ""),
        api_key=str(params.get("api_key") or ""),
        api_base=str(params.get("api_base") or ""),
    )
    try:
        folder = await asyncio.to_thread(hire, spec)
    except HireError as exc:
        raise InvalidParamsError(str(exc)) from exc
    _refresh(agent_loop_factory)
    (employee,) = [e for e in employees() if e.folder == folder.name]
    return {"employee": _wire(employee)}


async def employees_prompt_get(params: dict) -> dict:
    from raven.agent.subagent.hire import HireError, system_prompt

    try:
        return {"prompt": await asyncio.to_thread(system_prompt, str(params.get("name") or ""))}
    except HireError as exc:
        raise InvalidParamsError(str(exc)) from exc


async def employees_prompt_set(params: dict) -> dict:
    from raven.agent.subagent.hire import HireError, set_system_prompt

    try:
        await asyncio.to_thread(set_system_prompt, str(params.get("name") or ""), str(params.get("prompt") or ""))
    except HireError as exc:
        raise InvalidParamsError(str(exc)) from exc
    return {"ok": True}


async def employees_retire(params: dict, *, agent_loop_factory: AgentLoopFactory | None = None) -> dict:
    from raven.agent.subagent.hire import HireError, retire

    try:
        kept = await asyncio.to_thread(retire, str(params.get("name") or ""))
    except HireError as exc:
        raise InvalidParamsError(str(exc)) from exc
    _refresh(agent_loop_factory)
    return {"kept": str(kept)}


def register_employees_methods(dispatcher: Dispatcher, *, agent_loop_factory: AgentLoopFactory | None = None) -> None:
    """Register the ``employees.*`` methods on a dispatcher instance."""

    async def _hire(params: dict) -> dict:
        return await employees_hire(params, agent_loop_factory=agent_loop_factory)

    async def _retire(params: dict) -> dict:
        return await employees_retire(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("employees.list", employees_list)
    dispatcher.register("employees.hire", _hire)
    dispatcher.register("employees.prompt_get", employees_prompt_get)
    dispatcher.register("employees.prompt_set", employees_prompt_set)
    dispatcher.register("employees.retire", _retire)


__all__ = [
    "employees_hire",
    "employees_list",
    "employees_prompt_get",
    "employees_prompt_set",
    "employees_retire",
    "register_employees_methods",
]

"""``org.get`` and ``org.set`` -- the reporting structure the agent canvas draws and edits.

The chart travels with the companies and departments its members are grouped into. A saved
chart is pushed into the running agent at once, so who may hand work to whom changes with the
canvas and not on the next start.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from raven.agent.subagent import org_store
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


async def org_get(params: dict) -> dict:
    members = await asyncio.to_thread(org_store.read_org)
    companies = await asyncio.to_thread(org_store.read_companies)
    return {"members": members, "companies": companies, "max_depth": org_store.MAX_DEPTH}


def _apply_live(agent_loop_factory: AgentLoopFactory | None, members: list, companies: list) -> None:
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    manager = getattr(loop, "subagents", None)
    apply = getattr(manager, "apply_org", None)
    if not callable(apply):
        return
    try:
        apply(members, companies)
    except Exception as exc:  # noqa: BLE001 - the file is written; the running agent catches up on its next start
        logger.warning("org.set: the running agent kept its old chart: {}", exc)


async def org_set(params: dict, *, agent_loop_factory: AgentLoopFactory | None = None) -> dict:
    rows = params.get("members")
    if not isinstance(rows, list):
        raise ConfigValidationError("members must be a list")
    companies = params.get("companies")
    if companies is not None and not isinstance(companies, list):
        raise ConfigValidationError("companies must be a list")
    try:
        members = await asyncio.to_thread(org_store.write_org, rows, companies)
    except org_store.OrgRefusedError as exc:
        raise ConfigValidationError(str(exc), data={"field": "subagents.org"}) from exc
    groups = await asyncio.to_thread(org_store.read_companies)
    _apply_live(agent_loop_factory, members, groups)
    return {"members": members, "companies": groups, "max_depth": org_store.MAX_DEPTH}


def register_org_methods(dispatcher: "Dispatcher", *, agent_loop_factory: AgentLoopFactory | None = None) -> None:
    async def _set(params: dict) -> dict:
        return await org_set(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("org.get", org_get)
    dispatcher.register("org.set", _set)


__all__ = ["org_get", "org_set", "register_org_methods"]

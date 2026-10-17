"""``org.get`` and ``org.set`` -- the reporting structure the agent canvas draws and edits."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from raven.config import update_org
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


async def org_get(params: dict) -> dict:
    members = await asyncio.to_thread(update_org.read_org)
    return {"members": members, "max_depth": update_org.MAX_DEPTH}


async def org_set(params: dict) -> dict:
    rows = params.get("members")
    if not isinstance(rows, list):
        raise ConfigValidationError("members must be a list")
    try:
        members = await asyncio.to_thread(update_org.write_org, rows)
    except update_org.OrgRefused as exc:
        raise ConfigValidationError(str(exc), data={"field": "subagents.org"}) from exc
    return {"members": members, "max_depth": update_org.MAX_DEPTH}


def register_org_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("org.get", org_get)
    dispatcher.register("org.set", org_set)


__all__ = ["org_get", "org_set", "register_org_methods"]

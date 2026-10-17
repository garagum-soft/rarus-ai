"""``rarus.account`` -- who is signed in and how much is left on the account.

The page asks this for the account screen and the first-run wizard. The server call for the
balance runs off the event loop; a failed call still answers (``error`` says why) so the page
can show the e-mail without a figure instead of an error dialog.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from raven.providers import rarus_account

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


async def rarus_account_status(params: dict) -> dict:
    status = await asyncio.to_thread(rarus_account.status)
    return status.as_dict()


def register_rarus_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("rarus.account", rarus_account_status)


__all__ = ["register_rarus_methods", "rarus_account_status"]

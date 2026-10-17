"""The models a signed-in Rarus account offers.

The server's ``GET /v1/models`` is the authority (spec 18.3); what it said last is kept in the
served-models cache, and until it has been asked at all the two models every account has are
offered, so a fresh sign-in shows something to pick instead of an empty list.
"""

from __future__ import annotations

from raven.providers import rarus_client, rarus_session
from raven.providers.served_models import recall, remember

PROVIDER = "rarus"
# What an account has before the server has been asked: the default and the router.
BUILT_IN = ("deepseek-flash", "auto")


def offered() -> tuple[str, ...]:
    """Model ids to offer: the server's last answer, else the two built-in ones."""
    served = tuple(recall(PROVIDER))
    return served or BUILT_IN


def refresh() -> tuple[str, ...]:
    """Ask the server which models this account may use and keep the answer.

    Never raises: with no sign-in, or no connection, the previous answer (or the built-in pair)
    stays.
    """
    key = rarus_session.load_key()
    if key is None:
        return offered()
    try:
        ids = rarus_client.models(key)
    except rarus_client.RarusError:
        return offered()
    if ids:
        remember(PROVIDER, {model: "chat" for model in ids})
    return offered()

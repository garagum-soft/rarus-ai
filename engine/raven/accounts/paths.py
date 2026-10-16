"""Where accounts mode keeps its state, and where each account's engine lives.

Everything the account gateway owns sits under ``<RAVEN_HOME>/accounts`` of the
server's home; an account's engine home is ``accounts/users/<id>/home``, except
the first administrator's, which is the server's own home -- the Raven that was
running on this machine before accounts mode was switched on stays theirs.
"""

from __future__ import annotations

import os
from pathlib import Path

from raven.home import raven_home


def accounts_root() -> Path:
    """The account gateway's state directory."""
    override = os.environ.get("RAVEN_ACCOUNTS_ROOT", "").strip()
    return Path(override).expanduser() if override else raven_home() / "accounts"


def users_root() -> Path:
    return accounts_root() / "users"


def user_dir(account_id: str) -> Path:
    return users_root() / account_id


def user_home(account_id: str) -> Path:
    """The ``RAVEN_HOME`` of one account's engine."""
    return user_dir(account_id) / "home"


def engine_home(account: object) -> Path:
    """Where ``account``'s engine keeps everything: the server's own home for the first administrator."""
    if getattr(account, "server_home", False):
        return raven_home()
    return user_home(str(getattr(account, "id")))


__all__ = ["accounts_root", "engine_home", "user_dir", "user_home", "users_root"]

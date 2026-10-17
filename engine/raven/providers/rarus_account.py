"""The signed-in person as the interface shows them: who, and how much is left.

Shared by the desktop shell and the page's ``rarus.account`` call, so both read one answer.
The key itself never leaves ``rarus_session``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from raven.providers import rarus_client, rarus_session


@dataclass(frozen=True)
class Status:
    """``signed_in``, the e-mail, the balance (None when unknown) and why it is missing."""

    signed_in: bool
    email: str = ""
    balance: int | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"signed_in": self.signed_in, "email": self.email, "balance": self.balance, "error": self.error}


def status(*, refresh: bool = True) -> Status:
    """Signed-in state; with ``refresh`` the balance comes from the server."""
    session = rarus_session.load_session()
    if session is None:
        return Status(False)
    if not refresh:
        return Status(True, email=session.email)
    try:
        info = rarus_client.whoami(session.key)
    except rarus_client.RarusError as exc:
        if exc.code == "unauthorized":
            rarus_session.delete_session()
            return Status(False, error="unauthorized")
        return Status(True, email=session.email, error=exc.code)
    balance = info.get("balance")
    return Status(
        True,
        email=str(info.get("email") or session.email),
        balance=int(balance) if isinstance(balance, (int, float)) else None,
    )


def sign_out() -> None:
    rarus_session.delete_session()

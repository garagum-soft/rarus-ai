"""The signed-in person as the window sees it: sign in, sign out, who and how much is left.

Everything goes through the engine's own Rarus modules (``raven.providers.rarus_*``), so the
key lives where the engine reads it and nowhere else. The window never receives the key.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from raven.providers import rarus_client, rarus_login, rarus_session

DEFAULT_MODEL = "rarus/deepseek-flash"


@dataclass(frozen=True)
class Status:
    """What the window shows: ``signed_in``, the e-mail, the balance and why it is missing."""

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


def make_default() -> None:
    """Point the engine's configuration at Rarus (what ``rarus provider login rarus`` does)."""
    from raven.config.update import ensure_language, set_default_model

    set_default_model(DEFAULT_MODEL, provider="rarus")
    ensure_language("ru")


def first_run_language() -> None:
    """A fresh install starts in Russian; a language already chosen is left alone."""
    from raven.config.update import ensure_language

    ensure_language("ru")


class SignIn:
    """A sign-in running in the background, so the window stays responsive while the browser works."""

    def __init__(self, *, site: str | None = None, open_browser: bool = True) -> None:
        self._attempt = rarus_login.BrowserLogin(site=site)
        self._open_browser = open_browser
        self._thread: threading.Thread | None = None
        self._outcome: str | None = None  # "done" or an error code
        self._lock = threading.Lock()
        self.url = ""

    def start(self) -> str:
        self.url = self._attempt.start()
        if self._open_browser:
            import webbrowser

            webbrowser.open(self.url)
        self._thread = threading.Thread(target=self._wait, name="rarus-signin", daemon=True)
        self._thread.start()
        return self.url

    def _wait(self) -> None:
        try:
            self._attempt.wait()
            make_default()
            outcome = "done"
        except rarus_login.LoginError as exc:
            outcome = exc.code
        except Exception:  # the window needs an answer even for a bug
            outcome = "claim"
        with self._lock:
            self._outcome = outcome

    def outcome(self) -> str | None:
        """None while waiting; ``"done"`` or an error code (see ``LoginError.code``) after."""
        with self._lock:
            return self._outcome

    def cancel(self) -> None:
        self._attempt.cancel()
        self._attempt.close()

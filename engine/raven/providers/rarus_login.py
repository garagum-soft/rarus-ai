"""Signing in to Rarus through the system browser (spec section 18.2).

The program opens ``<site>/app-login`` with a one-time ``state`` and a PKCE
challenge in the fragment. The page lets the person sign in as they would on the
site (password or Google), then sends the browser to
``http://127.0.0.1:<port>/alembox-callback?code=...&state=...``. This module
listens there, checks ``state``, trades the code and its own ``verifier`` for the
agent key (``POST /api/agent/app-code/claim``) and stores it.

Only the loopback address is ever bound, and only the one path is served. A
request with a wrong ``state`` is answered and ignored: it cannot end the
sign-in, so a stray page cannot break a real one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from raven.providers import rarus_client
from raven.providers.rarus_session import Session, save_session, site_root

CALLBACK_PATH = "/alembox-callback"
LOGIN_TIMEOUT_S = 300.0

_CLAIM_TIMEOUT_S = 20.0
_LABEL_LIMIT = 60


class LoginError(Exception):
    """The sign-in did not finish; ``code`` is one of the names the page shows."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def new_verifier() -> str:
    """43 to 128 characters of ``[A-Za-z0-9-._~]`` (RFC 7636); 64 here."""
    return secrets.token_urlsafe(48)


def challenge_of(verifier: str) -> str:
    """``BASE64URL(SHA256(verifier))`` without padding: always 43 characters."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def default_label() -> str:
    return f"Rarus AI {socket.gethostname()}"[:_LABEL_LIMIT]


class BrowserLogin:
    """One sign-in attempt: ``start`` gives the page to open, ``wait`` the result."""

    def __init__(self, *, site: str | None = None, label: str | None = None) -> None:
        self.site = (site or site_root()).rstrip("/")
        self.label = (label or default_label())[:_LABEL_LIMIT]
        self._verifier = new_verifier()
        self._state = secrets.token_urlsafe(24)
        self._done = threading.Event()
        self._session: Session | None = None
        self._error: LoginError | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> str:
        """Begin listening and return the sign-in page address."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches on
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query)
                code = (query.get("code") or [""])[0]
                state = (query.get("state") or [""])[0]
                outcome = outer._receive(code, state)
                self.send_response(302)
                self.send_header("Location", f"{outer.site}/app-login#{outcome}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature fixed by http.server
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="rarus-login", daemon=True)
        self._thread.start()
        self.url = f"{self.site}/app-login#port={port}&state={self._state}&challenge={challenge_of(self._verifier)}"
        return self.url

    def _receive(self, code: str, state: str) -> str:
        """Handle one callback; the returned fragment tells the page how it went."""
        if self._done.is_set():
            return "error=state"
        if not code or not hmac.compare_digest(state.encode("utf-8"), self._state.encode("utf-8")):
            return "error=state"
        try:
            session = self._claim(code)
        except LoginError as exc:
            self._finish(error=exc)
            return f"error={exc.code}"
        self._finish(session=session)
        return "done=1"

    def _claim(self, code: str) -> Session:
        try:
            response = httpx.post(
                f"{self.site}/api/agent/app-code/claim",
                json={"code": code, "verifier": self._verifier, "label": self.label},
                timeout=_CLAIM_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise LoginError("network", f"could not reach the server: {exc}") from exc
        if response.status_code == 409:
            raise LoginError("keys", "the account already has ten program keys; revoke one on the site")
        if response.status_code == 403:
            raise LoginError("blocked", "the account is blocked")
        if response.status_code != 200:
            raise LoginError("claim", f"the server did not hand out a key ({response.status_code})")
        try:
            key = str(response.json().get("key") or "")
        except ValueError:
            key = ""
        if not key.startswith("sk-ab-"):
            raise LoginError("claim", "the server answered without a key")
        email = ""
        try:
            email = str(rarus_client.whoami(key, site=self.site).get("email") or "")
        except rarus_client.RarusError:
            pass
        return save_session(key, email=email, label=self.label)

    def _finish(self, *, session: Session | None = None, error: LoginError | None = None) -> None:
        self._session = session
        self._error = error
        self._done.set()

    def wait(self, timeout_s: float = LOGIN_TIMEOUT_S) -> Session:
        """Block until the browser comes back, then return the stored session."""
        try:
            if not self._done.wait(timeout_s):
                raise LoginError("timeout", "the browser did not come back in time")
            if self._error is not None:
                raise self._error
            if self._session is None:
                raise LoginError("claim", "the sign-in ended without a session")
            return self._session
        finally:
            self.close()

    def cancel(self) -> None:
        if not self._done.is_set():
            self._finish(error=LoginError("cancelled", "the sign-in was cancelled"))

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


def login(
    *,
    site: str | None = None,
    label: str | None = None,
    open_browser: bool = True,
    on_url: Callable[[str], None] | None = None,
    timeout_s: float = LOGIN_TIMEOUT_S,
) -> Session:
    """Run the whole sign-in; raise :class:`LoginError` if it does not finish."""
    attempt = BrowserLogin(site=site, label=label)
    url = attempt.start()
    try:
        if on_url is not None:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
    except BaseException:
        attempt.close()
        raise
    return attempt.wait(timeout_s)


__all__ = ["BrowserLogin", "LoginError", "challenge_of", "default_label", "login", "new_verifier"]

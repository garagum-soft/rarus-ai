"""The account gateway: signs people in and puts each one through to their own engine.

One aiohttp server, the only thing people's browsers reach. Its own routes sit
under ``/_hub`` -- the sign-in page, the account page, the admin page and their
JSON API, Google Sign-In -- and every other request of a signed-in person is
relayed to that person's engine at the root of the same origin (the engine's
page addresses ``/rpc``, ``/file``, ``/health`` from the root), HTTP and the
``/rpc`` WebSocket alike, carrying the engine's token. A person who is not
signed in is sent to the sign-in page; nothing reaches an engine without a
session.

Defences: sessions are HttpOnly, SameSite=Strict cookies (Secure when the
request came over https); every state-changing ``/_hub/api`` call and every
WebSocket must come from this origin; the engine's own sign-in routes are never
relayed; sign-in failures are throttled; the first account -- the superadmin --
can only be made from the server itself (a request to ``localhost`` that no
proxy forwarded), or with ``raven accounts init`` in its terminal.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from loguru import logger

from raven.accounts import gate
from raven.accounts.billing import Ledger, Meter, Price, PriceTable, top_up, welcome_bonus
from raven.accounts.disk import GB, tree_size
from raven.accounts.engines import EngineSupervisor, apply_lent_providers, lendable
from raven.accounts.google import CALLBACK_PATH, GoogleSignIn, GoogleSignInError
from raven.accounts.paths import engine_home
from raven.accounts.settings import SettingsStore
from raven.accounts.store import (
    PERMISSIONS,
    Account,
    AccountError,
    AccountStore,
    AuditLog,
    SessionStore,
    Throttle,
    check_password,
    hash_password,
    verify_password,
)

COOKIE = "raven_hub"
OAUTH_COOKIE = "raven_hub_oauth"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# The engine's own sign-in doors: a relayed browser is already signed in here, and
# must never be able to mint an engine cookie or pair a device behind the gateway.
_ENGINE_ONLY = ("/auth", "/pair", "/oauth/callback")
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
_DROP_REQUEST = _HOP | {"cookie", "origin", "x-raven-token", "x-raven-device-token", "x-forwarded-for", "forwarded"}
_DROP_RESPONSE = _HOP | {"set-cookie", "content-encoding"}
WS_MAX_MSG = 200 * 1024 * 1024
_BAR = b'<script src="/_hub/static/bar.js" defer></script>'


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str))


def _error(message: str, status: int = 400) -> web.Response:
    return _json({"error": message}, status=status)


class Hub:
    def __init__(
        self,
        *,
        store: AccountStore | None = None,
        sessions: SessionStore | None = None,
        settings: SettingsStore | None = None,
        audit: AuditLog | None = None,
        prices: PriceTable | None = None,
        ledger: Ledger | None = None,
        supervisor: EngineSupervisor | None = None,
        google: Callable[[Any], GoogleSignIn] | None = None,
        meter_every_s: float = 30.0,
        disk_every_s: float = 600.0,
        autostart: bool = True,
    ) -> None:
        self.store = store or AccountStore()
        self.sessions = sessions or SessionStore()
        self.settings = settings or SettingsStore()
        self.audit = audit or AuditLog()
        self.prices = prices or PriceTable()
        self.ledger = ledger or Ledger()
        self.meter = Meter(self.store, self.prices, self.ledger)
        self.supervisor = supervisor or EngineSupervisor(self.store, port_base=self.settings.load().engine_port_base)
        self._google_factory = google or (lambda s: GoogleSignIn(s))
        self._google: GoogleSignIn | None = None
        self._google_key: tuple[str, str, str] | None = None
        self.throttle = Throttle()
        self.meter_every_s, self.disk_every_s, self.autostart = meter_every_s, disk_every_s, autostart
        self.disk: dict[str, int] = {}
        self.blocked: dict[str, str] = {}
        self._sockets: dict[str, set[web.WebSocketResponse]] = {}
        self._tasks: list[asyncio.Task] = []
        self._client: ClientSession | None = None
        self._starting: dict[str, asyncio.Task] = {}

    # -- the app ---------------------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(client_max_size=WS_MAX_MSG)
        r = app.router
        r.add_get("/_hub", self._to_login)
        r.add_get("/_hub/login", self._page("login.html"))
        r.add_get("/_hub/account", self._page("account.html"))
        r.add_get("/_hub/admin", self._page("admin.html"))
        r.add_static("/_hub/static", STATIC_DIR, show_index=False)
        r.add_get("/_hub/api/state", self.state)
        r.add_post("/_hub/api/setup", self.setup)
        r.add_post("/_hub/api/register", self.register)
        r.add_post("/_hub/api/login", self.login)
        r.add_post("/_hub/api/logout", self.logout)
        r.add_post("/_hub/api/password", self.change_password)
        r.add_get("/_hub/api/me", self.me)
        r.add_get("/_hub/google/start", self.google_start)
        r.add_get(CALLBACK_PATH, self.google_callback)
        r.add_get("/_hub/api/admin/accounts", self.admin_accounts)
        r.add_post("/_hub/api/admin/accounts", self.admin_create)
        r.add_post("/_hub/api/admin/accounts/{id}", self.admin_update)
        r.add_post("/_hub/api/admin/accounts/{id}/topup", self.admin_topup)
        r.add_post("/_hub/api/admin/accounts/{id}/engine", self.admin_engine)
        r.add_get("/_hub/api/admin/prices", self.admin_prices)
        r.add_post("/_hub/api/admin/prices", self.admin_set_price)
        r.add_get("/_hub/api/admin/settings", self.admin_settings)
        r.add_post("/_hub/api/admin/settings", self.admin_update_settings)
        r.add_get("/_hub/api/admin/audit", self.admin_audit)
        r.add_get("/_hub/api/admin/ledger", self.admin_ledger)
        r.add_route("*", "/{tail:.*}", self.relay)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    @staticmethod
    async def _to_login(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/_hub/login")

    def _page(self, name: str):
        async def serve(_request: web.Request) -> web.StreamResponse:
            return web.FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-cache"})

        return serve

    async def _on_startup(self, _app: web.Application) -> None:
        self.prices.ensure()
        self._client = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10))
        self._tasks.append(asyncio.create_task(self._meter_loop(), name="accounts-meter"))
        self._tasks.append(asyncio.create_task(self._disk_loop(), name="accounts-disk"))
        self.supervisor.start_watch()
        if self.autostart:
            self._tasks.append(asyncio.create_task(self._start_all(), name="accounts-autostart"))

    async def _on_cleanup(self, _app: web.Application) -> None:
        for task in self._tasks:
            task.cancel()
        self.supervisor.stop_watch()
        for sockets in self._sockets.values():
            for ws in list(sockets):
                await ws.close()
        if self._client is not None:
            await self._client.close()

    async def _start_all(self) -> None:
        for account in self.store.all():
            if not account.disabled:
                try:
                    await self.supervisor.ensure(account)
                except Exception:  # noqa: BLE001 - one engine that will not start does not stop the others
                    logger.exception("could not start the engine of {}", account.username)

    # -- who is asking ---------------------------------------------------------------------

    def account_of(self, request: web.Request) -> Account | None:
        account_id = self.sessions.resolve(request.cookies.get(COOKIE))
        account = self.store.get(account_id) if account_id else None
        return account if account is not None and not account.disabled else None

    @staticmethod
    def is_local(request: web.Request) -> bool:
        """A request made on the server itself: from loopback, to a loopback name, through no proxy."""
        if request.remote not in ("127.0.0.1", "::1"):
            return False
        if any(h in request.headers for h in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host")):
            return False
        host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1")

    @staticmethod
    def _secure(request: web.Request) -> bool:
        return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"

    @staticmethod
    def same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        return bool(origin) and urlparse(origin).netloc.lower() == (request.host or "").lower()

    def _signed_in(self, request: web.Request, response: web.StreamResponse, account: Account) -> None:
        token = self.sessions.issue(account.id)
        response.set_cookie(
            COOKIE,
            token,
            max_age=30 * 24 * 3600,
            httponly=True,
            samesite="Strict",
            secure=self._secure(request),
            path="/",
        )

    async def _body(self, request: web.Request) -> dict[str, Any]:
        if not self.same_origin(request):
            raise web.HTTPForbidden(text="cross-origin request refused")
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="the request body is not JSON") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="the request body is not a JSON object")
        return data

    def _admin(self, request: web.Request, *permissions: str) -> Account:
        account = self.account_of(request)
        if account is None:
            raise web.HTTPUnauthorized(text="sign in first")
        if not any(account.can(p) for p in permissions):
            raise web.HTTPForbidden(text="this needs a permission your account does not have")
        return account

    # -- sign-in ---------------------------------------------------------------------------

    async def state(self, request: web.Request) -> web.Response:
        settings = self.settings.load()
        account = self.account_of(request)
        return _json(
            {
                "setupNeeded": self.store.count() == 0,
                "local": self.is_local(request),
                "registrationOpen": settings.registration_open,
                "google": settings.google_ready,
                "me": account.public() if account else None,
            }
        )

    async def setup(self, request: web.Request) -> web.Response:
        data = await self._body(request)
        if not self.is_local(request):
            return _error("the first account can only be made on the server itself (open http://localhost)", 403)
        try:
            account = self.store.create(
                str(data.get("username") or ""),
                str(data.get("password") or ""),
                role="superadmin",
                server_home=True,
                first_only=True,
            )
        except AccountError as exc:
            return _error(str(exc))
        self.audit.record(account.username, "setup", account.username)
        response = _json({"me": account.public()})
        self._signed_in(request, response, account)
        return response

    async def register(self, request: web.Request) -> web.Response:
        data = await self._body(request)
        settings = self.settings.load()
        if self.store.count() == 0:
            return _error("this server has no administrator yet; it is set up from the server itself", 403)
        if not settings.registration_open:
            return _error("registration is closed on this server; ask an administrator for an account", 403)
        password = str(data.get("password") or "")
        try:
            check_password(password)
            account = self.store.create(
                str(data.get("username") or ""),
                password,
                email=str(data.get("email") or ""),
            )
        except AccountError as exc:
            return _error(str(exc))
        self.audit.record(account.username, "register", account.username, address=request.remote)
        welcome_bonus(self.store, self.ledger, account.id, settings)
        account = self.store.get(account.id)
        response = _json({"me": account.public()})
        self._signed_in(request, response, account)
        return response

    async def login(self, request: web.Request) -> web.Response:
        data = await self._body(request)
        username = str(data.get("username") or "").strip().lower()
        address = request.remote or ""
        if self.throttle.blocked(username, address):
            return _error("too many failed sign-ins; wait fifteen minutes and try again", 429)
        account = self.store.by_username(username)
        if (
            account is None
            or not account.password_hash
            or not verify_password(str(data.get("password") or ""), account.password_hash)
        ):
            self.throttle.failed(username, address)
            self.audit.record(username or "?", "login_failed", username, address=address)
            return _error("wrong username or password", 401)
        if account.disabled:
            return _error("this account is disabled; ask an administrator", 403)
        self.throttle.succeeded(username)
        self.audit.record(account.username, "login", account.username, address=address)
        response = _json({"me": account.public()})
        self._signed_in(request, response, account)
        return response

    async def logout(self, request: web.Request) -> web.Response:
        await self._body(request)
        self.sessions.revoke(request.cookies.get(COOKIE))
        response = _json({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def change_password(self, request: web.Request) -> web.Response:
        data = await self._body(request)
        account = self.account_of(request)
        if account is None:
            return _error("sign in first", 401)
        if account.password_hash and not verify_password(str(data.get("current") or ""), account.password_hash):
            return _error("the current password is wrong", 401)
        try:
            new_hash = hash_password(str(data.get("password") or ""))
        except AccountError as exc:
            return _error(str(exc))

        def change(row: Account) -> None:
            row.password_hash = new_hash

        self.store.update(account.id, change)
        self.audit.record(account.username, "password", account.username)
        return _json({"ok": True})

    async def me(self, request: web.Request) -> web.Response:
        account = self.account_of(request)
        if account is None:
            return _error("sign in first", 401)
        engine = self.supervisor.engine(account.id)
        settings = self.settings.load()
        return _json(
            {
                "me": account.public(),
                "engine": engine.public() if engine else {"state": "stopped"},
                "blocked": self.blocked.get(account.id),
                "diskGb": round(self.disk.get(account.id, 0) / GB, 3),
                "diskQuotaGb": None if account.is_admin else settings.disk_quota_gb,
                "ledger": self.ledger.rows(account.id, limit=50),
                "blockedAccounts": len(self.blocked) if account.is_admin else None,
            }
        )

    # -- Google ----------------------------------------------------------------------------

    def google(self) -> GoogleSignIn:
        settings = self.settings.load()
        key = (settings.google_client_id, settings.google_client_secret, settings.public_url)
        if self._google is None or self._google_key != key:
            self._google, self._google_key = self._google_factory(settings), key
        else:
            self._google.settings = settings
        return self._google

    async def google_start(self, request: web.Request) -> web.StreamResponse:
        signed_in = self.account_of(request)
        try:
            url, state = self.google().start(link_account=signed_in.id if signed_in else None)
        except GoogleSignInError as exc:
            return web.Response(text=str(exc), status=400)
        response = web.Response(status=302, headers={"Location": url})
        # Lax, not Strict: Google's redirect back is a cross-site navigation, and the
        # state cookie must ride on it.
        response.set_cookie(
            OAUTH_COOKIE,
            state,
            max_age=600,
            httponly=True,
            samesite="Lax",
            secure=self._secure(request),
            path="/_hub/google",
        )
        return response

    async def google_callback(self, request: web.Request) -> web.StreamResponse:
        state = request.query.get("state", "")
        if not state or state != request.cookies.get(OAUTH_COOKIE):
            return self._message_page(
                "Sign-in could not be verified (the browser did not return the same state). Start again.", 400
            )
        if request.query.get("error"):
            return self._message_page(f"Google did not sign you in ({request.query['error']}).", 400)
        try:
            identity = await self.google().finish(state, request.query.get("code", ""))
        except GoogleSignInError as exc:
            return self._message_page(str(exc), 400)
        account = self._account_for_google(identity, request)
        if isinstance(account, str):
            return self._message_page(account, 403)
        # A page rather than a redirect: the session cookie is SameSite=Strict, and a
        # redirect chain that began at Google would not carry it to the next request.
        response = web.Response(
            text='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
            '<title>Raven</title><p>Signed in. <a href="/">Continue</a></p>',
            content_type="text/html",
        )
        response.del_cookie(OAUTH_COOKIE, path="/_hub/google")
        self._signed_in(request, response, account)
        return response

    def _account_for_google(self, identity: Any, request: web.Request) -> Account | str:
        """The account a Google identity signs in to, made when registration is open; else why not."""
        linked = self.store.by_google(identity.sub)
        if identity.link_account:
            if linked is not None and linked.id != identity.link_account:
                return "That Google account is already linked to another account here."

            def link(row: Account) -> None:
                row.google_sub = identity.sub
                row.email = row.email or identity.email

            account = self.store.update(identity.link_account, link)
            self.audit.record(account.username, "google_link", account.username)
            return account
        if linked is not None:
            if linked.disabled:
                return "This account is disabled; ask an administrator."
            self.audit.record(linked.username, "login", linked.username, method="google", address=request.remote)
            return linked
        by_email = self.store.by_email(identity.email)
        if by_email is not None:
            return (
                "An account with this e-mail exists but is not linked to Google. Sign in with its password, "
                "then link Google from the account page."
            )
        settings = self.settings.load()
        if self.store.count() == 0:
            return "This server has no administrator yet; it is set up from the server itself."
        if not settings.registration_open:
            return "Registration is closed on this server; ask an administrator for an account."
        base = (identity.email.split("@")[0] or "user").lower()
        base = "".join(ch for ch in base if ch.isalnum() or ch in "._-")[:24] or "user"
        if not base[0].isalnum():
            base = "u" + base
        for suffix in ["", *[str(n) for n in range(2, 100)]]:
            try:
                account = self.store.create(
                    f"{base}{suffix}"[:32],
                    None,
                    email=identity.email,
                    google_sub=identity.sub,
                    display_name=identity.name,
                )
            except AccountError as exc:
                if "taken" in str(exc):
                    continue
                return str(exc)
            self.audit.record(account.username, "register", account.username, method="google", address=request.remote)
            welcome_bonus(self.store, self.ledger, account.id, settings)
            return self.store.get(account.id)
        return "Could not find a free username for this Google account; register with a password instead."

    def _message_page(self, message: str, status: int) -> web.Response:
        import html

        return web.Response(
            text=f'<!doctype html><meta charset="utf-8"><title>Raven</title><link rel="stylesheet" href="/_hub/static/hub.css">'
            f'<main class="hub-card"><p>{html.escape(message)}</p><p><a href="/_hub/login">Back to sign-in</a></p></main>',
            content_type="text/html",
            status=status,
        )

    # -- admin -----------------------------------------------------------------------------

    def _account_row(self, account: Account) -> dict[str, Any]:
        engine = self.supervisor.engine(account.id)
        return {
            **account.public(),
            "engine": engine.public() if engine else {"state": "stopped"},
            "blocked": self.blocked.get(account.id),
            "diskGb": round(self.disk.get(account.id, 0) / GB, 3),
        }

    async def admin_accounts(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "users.manage", "points.topup", "audit.read")
        return _json(
            {
                "accounts": [self._account_row(a) for a in self.store.all()],
                "permissions": PERMISSIONS,
                "lendable": lendable(self.supervisor.server_home),
                "me": actor.public(),
            }
        )

    async def admin_create(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "users.manage")
        data = await self._body(request)
        role = str(data.get("role") or "user")
        if role != "user" and actor.role != "superadmin":
            return _error("only the superadmin makes administrators", 403)
        if role == "superadmin":
            return _error("there is one superadmin", 400)
        try:
            account = self.store.create(
                str(data.get("username") or ""),
                str(data.get("password") or "") or None,
                role=role,
                email=str(data.get("email") or ""),
            )
        except AccountError as exc:
            return _error(str(exc))
        self.audit.record(actor.username, "account_create", account.username, role=role)
        welcome_bonus(self.store, self.ledger, account.id, self.settings.load())
        return _json({"account": self._account_row(self.store.get(account.id))})

    async def admin_update(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "users.manage")
        data = await self._body(request)
        target = self.store.get(request.match_info["id"])
        if target is None:
            return _error("no such account", 404)
        if target.role == "superadmin" and any(k in data for k in ("disabled", "role", "permissions")):
            # Not even by the superadmin: a server with no superadmin has nobody to make admins.
            return _error("the superadmin stays the superadmin, enabled", 403)
        if target.role == "admin" and actor.role != "superadmin":
            return _error("an administrator's account is changed by the superadmin only", 403)
        if ("role" in data or "permissions" in data) and actor.role != "superadmin":
            return _error("only the superadmin grants roles and permissions", 403)
        if "role" in data and str(data["role"]) not in ("admin", "user"):
            return _error("an account is made an admin or a user", 400)
        lent = data.get("lent")
        if lent is not None:
            available = set(lendable(self.supervisor.server_home))
            unknown = sorted(set(map(str, lent)) - available)
            if unknown:
                return _error(f"the server has nothing to lend named {', '.join(unknown)}", 400)
        password = data.get("password")
        new_hash = None
        if password:
            try:
                new_hash = hash_password(str(password))
            except AccountError as exc:
                return _error(str(exc))

        def change(row: Account) -> None:
            if "disabled" in data:
                row.disabled = bool(data["disabled"])
            if "role" in data:
                row.role = str(data["role"])
                if row.role == "user":
                    row.permissions = []
            if "permissions" in data and row.role == "admin":
                row.permissions = [str(p) for p in data["permissions"] or []]
            if lent is not None:
                row.lent = sorted({str(p) for p in lent})
            if "display_name" in data:
                row.display_name = str(data["display_name"] or row.username)[:64]
            if new_hash:
                row.password_hash = new_hash

        try:
            account = self.store.update(target.id, change)
        except AccountError as exc:
            return _error(str(exc))
        detail = {k: data[k] for k in ("disabled", "role", "permissions", "lent", "display_name") if k in data}
        if password:
            detail["password"] = "reset"  # noqa: S105 - what happened, not a password
        self.audit.record(actor.username, "account_update", account.username, **detail)
        if account.disabled:
            self.sessions.revoke_account(account.id)
            await self._close_sockets(account.id)
            await self.supervisor.stop(account.id)
        elif lent is not None:
            needs_restart = set(account.lent) ^ set(target.lent)
            apply_lent_providers(account, engine_home(account), server_home=self.supervisor.server_home)
            if needs_restart & {"claude_subscription", "openai_codex"} and self.supervisor.engine(account.id):
                # A subscription is reached through the engine's environment, read at start.
                self._tasks.append(asyncio.create_task(self.supervisor.restart(account)))
        return _json({"account": self._account_row(account)})

    async def admin_topup(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "points.topup")
        data = await self._body(request)
        target = self.store.get(request.match_info["id"])
        if target is None:
            return _error("no such account", 404)
        try:
            credits = float(data.get("credits"))
            balance = top_up(
                self.store,
                self.ledger,
                target.id,
                credits,
                actor=actor.username,
                note=str(data.get("note") or "")[:200],
            )
        except (TypeError, ValueError) as exc:
            return _error(str(exc) or "a top-up is a number of credits")
        self.audit.record(actor.username, "topup", target.username, credits=credits, balance=balance)
        await asyncio.to_thread(self._evaluate, self.store.get(target.id))
        return _json({"balance": balance, "account": self._account_row(self.store.get(target.id))})

    async def admin_engine(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "users.manage")
        data = await self._body(request)
        target = self.store.get(request.match_info["id"])
        if target is None:
            return _error("no such account", 404)
        action = str(data.get("action") or "")
        if action == "restart":
            engine = await self.supervisor.restart(target)
        elif action == "stop":
            await self.supervisor.stop(target.id)
            engine = self.supervisor.engine(target.id)
        elif action == "start":
            engine = await self.supervisor.ensure(target)
        else:
            return _error("the action is start, stop or restart")
        self.audit.record(actor.username, f"engine_{action}", target.username)
        return _json({"engine": engine.public() if engine else {"state": "stopped"}})

    async def admin_prices(self, request: web.Request) -> web.Response:
        self._admin(request, "prices.edit", "audit.read", "points.topup")
        settings = self.settings.load()
        return _json(
            {
                "prices": {k: v.to_wire() for k, v in sorted(self.prices.load().items())},
                "creditsPerUsd": settings.credits_per_usd,
                "ownShare": settings.own_access_share,
                "lentShare": settings.lent_access_share,
                "unpriced": self.ledger.unpriced_models(),
            }
        )

    async def admin_set_price(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "prices.edit")
        data = await self._body(request)
        model = str(data.get("model") or "").strip()
        if data.get("remove"):
            price = None
        else:
            price = Price.of(
                {"input": data.get("input"), "output": data.get("output"), "cacheHit": data.get("cacheHit", 0)}
            )
            if price is None:
                return _error("a price is three non-negative numbers: input, output and cache hit, per million tokens")
        try:
            table = self.prices.set(model, price)
        except ValueError as exc:
            return _error(str(exc))
        self.audit.record(actor.username, "price", model, **(price.to_wire() if price else {"removed": True}))
        return _json({"prices": {k: v.to_wire() for k, v in sorted(table.items())}})

    async def admin_settings(self, request: web.Request) -> web.Response:
        self._admin(request, "settings.edit", "prices.edit")
        return _json({"settings": self.settings.load().public()})

    async def admin_update_settings(self, request: web.Request) -> web.Response:
        actor = self._admin(request, "settings.edit", "prices.edit")
        data = await self._body(request)
        pricing_keys = {"credits_per_usd", "own_access_share", "lent_access_share"}
        if not actor.can("settings.edit") and set(data) - pricing_keys:
            return _error("this needs the settings permission", 403)
        if not actor.can("prices.edit") and set(data) & pricing_keys:
            return _error("this needs the prices permission", 403)
        if data.get("google_client_secret") == "":
            data.pop("google_client_secret")  # an empty field leaves the stored secret as it is
        try:
            settings = self.settings.update(data)
        except (ValueError, TypeError) as exc:
            return _error(str(exc))
        shown = {k: ("(set)" if k == "google_client_secret" else v) for k, v in data.items()}
        self.audit.record(actor.username, "settings", "", **shown)
        return _json({"settings": settings.public()})

    async def admin_audit(self, request: web.Request) -> web.Response:
        self._admin(request, "audit.read")
        return _json({"audit": list(reversed(self.audit.tail(int(request.query.get("limit", 300)))))})

    async def admin_ledger(self, request: web.Request) -> web.Response:
        self._admin(request, "audit.read", "points.topup")
        return _json(
            {
                "ledger": self.ledger.rows(
                    request.query.get("account") or None, limit=int(request.query.get("limit", 300))
                )
            }
        )

    # -- metering and the gate -------------------------------------------------------------

    def _evaluate(self, account: Account | None) -> None:
        """Decide whether ``account`` may start new work, and tell its engine."""
        if account is None:
            return
        settings = self.settings.load()
        reason = ""
        if account.role != "superadmin" and account.balance <= 0:
            reason = gate.NO_CREDITS
        used = self.disk.get(account.id, 0) / GB
        if not account.is_admin and used > settings.disk_quota_gb:
            reason = gate.over_disk(used, settings.disk_quota_gb)
        was = self.blocked.get(account.id)
        if reason:
            self.blocked[account.id] = reason
        else:
            self.blocked.pop(account.id, None)
        if gate.write_gate(
            engine_home(account), blocked=bool(reason), reason=reason, detail={"balance": account.balance}
        ):
            self.audit.record("hub", "blocked" if reason else "unblocked", account.username, reason=reason or None)
            if reason and not was:
                logger.warning("account {} is blocked: {}", account.username, reason)

    def meter_once(self) -> None:
        settings = self.settings.load()
        for account in self.store.all():
            try:
                self.meter.charge(account, engine_home(account), settings)
                self._evaluate(self.store.get(account.id))
            except Exception:  # noqa: BLE001 - one account's bad file does not stop the others' metering
                logger.exception("metering failed for {}", account.username)

    def disk_once(self) -> None:
        for account in self.store.all():
            if account.is_admin:
                continue
            self.disk[account.id] = tree_size(engine_home(account))

    async def _meter_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.meter_once)
            await asyncio.sleep(self.meter_every_s)

    async def _disk_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.disk_once)
            await asyncio.sleep(self.disk_every_s)

    # -- the relay -------------------------------------------------------------------------

    async def _close_sockets(self, account_id: str) -> None:
        for ws in list(self._sockets.pop(account_id, set())):
            await ws.close(message=b"signed out")

    def _starting_page(self) -> web.Response:
        return web.Response(
            text='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="2">'
            '<title>Raven</title><link rel="stylesheet" href="/_hub/static/hub.css">'
            '<main class="hub-card"><p data-i18n="starting">Starting your Raven...</p></main>'
            '<script src="/_hub/static/hub.js"></script>',
            content_type="text/html",
            status=503,
            headers={"Cache-Control": "no-store", "Retry-After": "2"},
        )

    async def relay(self, request: web.Request) -> web.StreamResponse:
        account = self.account_of(request)
        path = request.path
        if account is None:
            if request.method == "GET" and "text/html" in request.headers.get("Accept", "") or path == "/":
                raise web.HTTPFound("/_hub/login")
            return _error("sign in first", 401)
        if path.startswith(_ENGINE_ONLY) or path.startswith("/_hub"):
            return _error("not found", 404)
        engine = self.supervisor.engine(account.id)
        if engine is None or engine.state != "running":
            task = self._starting.get(account.id)
            if task is None or task.done():
                self._starting[account.id] = asyncio.create_task(self.supervisor.ensure(account))
            if engine is not None and engine.state == "failed":
                return self._message_page(f"Your Raven could not start: {engine.error}", 503)
            return self._starting_page() if path == "/" else _error("your Raven is starting", 503)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._relay_ws(request, account, engine)
        return await self._relay_http(request, engine)

    async def _relay_http(self, request: web.Request, engine: Any) -> web.StreamResponse:
        if self._client is None:
            return _error("the account gateway is not running", 503)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        headers["X-Raven-Token"] = engine.token
        headers["Accept-Encoding"] = "identity"
        url = f"{engine.base_url}{request.rel_url}"

        async def body():
            async for chunk in request.content.iter_chunked(65536):
                yield chunk

        try:
            upstream_cm = self._client.request(
                request.method,
                url,
                headers=headers,
                data=body() if request.body_exists else None,
                allow_redirects=False,
            )
            async with upstream_cm as upstream:
                out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE}
                is_page = request.path == "/" and upstream.headers.get("Content-Type", "").startswith("text/html")
                if is_page:
                    page = await upstream.read()
                    page = page.replace(b"</head>", _BAR + b"</head>", 1) if b"</head>" in page else page + _BAR
                    return web.Response(body=page, status=upstream.status, headers=out_headers)
                response = web.StreamResponse(status=upstream.status, headers=out_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("relay to {} failed: {}", url, exc)
            return _error("your Raven did not answer", 502)

    async def _relay_ws(self, request: web.Request, account: Account, engine: Any) -> web.StreamResponse:
        if self._client is None:
            return _error("the account gateway is not running", 503)
        if not self.same_origin(request):
            return _error("cross-origin socket refused", 403)
        client = web.WebSocketResponse(max_msg_size=WS_MAX_MSG, heartbeat=30.0)
        await client.prepare(request)
        sockets = self._sockets.setdefault(account.id, set())
        sockets.add(client)
        try:
            async with self._client.ws_connect(
                f"{engine.base_url}{request.rel_url}",
                headers={"X-Raven-Token": engine.token},
                max_msg_size=WS_MAX_MSG,
                heartbeat=30.0,
            ) as upstream:

                async def pump(source: Any, sink: Any) -> None:
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            await sink.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await sink.send_bytes(message.data)
                        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                            break

                tasks = [asyncio.create_task(pump(client, upstream)), asyncio.create_task(pump(upstream, client))]
                _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
        except Exception as exc:  # noqa: BLE001 - a socket that cannot reach the engine closes; the page reconnects
            logger.info("socket relay for {} ended: {}", account.username, exc)
        finally:
            sockets.discard(client)
            if not client.closed:
                await client.close()
        return client


def build_hub(**kwargs: Any) -> Hub:
    return Hub(**kwargs)


__all__ = ["COOKIE", "Hub", "build_hub"]

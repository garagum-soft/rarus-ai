"""The account gateway end to end: sign-in, the admin API, the relay to an engine, metering, Google.

The engines are a stand-in process (``tests/accounts_fake_engine.py``) that
answers like a gateway's page port; everything else is the real server.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from raven.accounts import gate
from raven.accounts.billing import Ledger, Price, PriceTable
from raven.accounts.engines import EngineSupervisor
from raven.accounts.google import GoogleSignIn
from raven.accounts.hub import COOKIE, Hub
from raven.accounts.paths import user_home
from raven.accounts.settings import SettingsStore
from raven.accounts.store import AccountStore, AuditLog

FAKE_ENGINE = str(Path(__file__).with_name("accounts_fake_engine.py"))


def _free_port_base() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.delenv("RAVEN_ACCOUNTS_ROOT", raising=False)
    monkeypatch.delenv(gate.ENV_ACCOUNT, raising=False)
    store = AccountStore()
    supervisor = EngineSupervisor(
        store, port_base=_free_port_base(), command=[sys.executable, FAKE_ENGINE, "{page}"], start_timeout=60
    )
    google_box: dict = {}

    def google_factory(settings):
        google = GoogleSignIn(settings, exchange=google_box.get("exchange"), key_for=google_box.get("key_for"))
        google_box["instance"] = google
        return google

    hub = Hub(
        store=store,
        supervisor=supervisor,
        autostart=False,
        meter_every_s=3600,
        disk_every_s=3600,
        google=google_factory,
    )
    client = TestClient(TestServer(hub.app()))
    await client.start_server()
    origin = f"http://{client.host}:{client.port}"

    async def post(path: str, body: dict | None = None, *, headers: dict | None = None, same_origin: bool = True):
        sent = {"Origin": origin} if same_origin else {}
        sent.update(headers or {})
        return await client.post(path, json=body or {}, headers=sent)

    yield SimpleNamespace(
        hub=hub, client=client, store=store, supervisor=supervisor, post=post, origin=origin, google=google_box
    )
    await supervisor.stop_all()
    await client.close()


async def _setup_root(env) -> None:
    response = await env.post("/_hub/api/setup", {"username": "root", "password": "rootpass1"})
    assert response.status == 200, await response.text()


async def _sign_in_as(env, username: str, password: str):
    env.client.session.cookie_jar.clear()
    response = await env.post("/_hub/api/login", {"username": username, "password": password})
    assert response.status == 200, await response.text()
    return (await response.json())["me"]


# -- the first account -----------------------------------------------------------------------


async def test_the_superadmin_is_made_only_from_the_server_itself(env) -> None:
    state = await (await env.client.get("/_hub/api/state")).json()
    assert state["setupNeeded"] and state["local"] and state["me"] is None

    proxied = await env.post(
        "/_hub/api/setup", {"username": "x", "password": "password1"}, headers={"X-Forwarded-For": "8.8.8.8"}
    )
    assert proxied.status == 403, "a request a proxy forwarded is not the server itself"
    elsewhere = await env.post(
        "/_hub/api/setup", {"username": "x", "password": "password1"}, headers={"Host": "raven.example.com"}
    )
    assert elsewhere.status in (403, 404)

    await _setup_root(env)
    me = await (await env.client.get("/_hub/api/me")).json()
    assert me["me"]["role"] == "superadmin" and me["me"]["server_home"] is True
    again = await env.post("/_hub/api/setup", {"username": "y", "password": "password1"})
    assert again.status == 400 and "already" in (await again.json())["error"]


async def test_a_post_from_another_origin_is_refused(env) -> None:
    await _setup_root(env)

    foreign = await env.post(
        "/_hub/api/login", {"username": "root", "password": "rootpass1"}, headers={"Origin": "http://evil.example"}
    )
    bare = await env.post("/_hub/api/login", {"username": "root", "password": "rootpass1"}, same_origin=False)

    assert foreign.status == 403 and bare.status == 403


# -- registration and sign-in ----------------------------------------------------------------


async def test_registration_sign_in_and_out(env) -> None:
    early = await env.post("/_hub/api/register", {"username": "bob", "password": "bobpass12"})
    assert early.status == 403, "no registering on a server with no administrator"
    await _setup_root(env)
    SettingsStore().update({"new_account_credits": 25})
    env.client.session.cookie_jar.clear()

    registered = await env.post(
        "/_hub/api/register", {"username": "Bob", "password": "bobpass12", "email": "bob@x.org"}
    )
    me = (await registered.json())["me"]
    assert registered.status == 200 and me["username"] == "bob" and me["role"] == "user" and me["balance"] == 25
    assert any(c.key == COOKIE for c in env.client.session.cookie_jar)
    assert (await env.client.get("/_hub/api/me")).status == 200

    await env.post("/_hub/api/logout")
    assert (await env.client.get("/_hub/api/me")).status == 401
    weak = await env.post("/_hub/api/register", {"username": "eve", "password": "short"})
    assert weak.status == 400
    SettingsStore().update({"registration_open": False})
    closed = await env.post("/_hub/api/register", {"username": "eve", "password": "evepass12"})
    assert closed.status == 403


async def test_every_new_account_gets_the_welcome_bonus_on_the_ledger(env) -> None:
    await _setup_root(env)
    made = await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})
    bob = (await made.json())["account"]
    env.client.session.cookie_jar.clear()
    registered = await env.post("/_hub/api/register", {"username": "carol", "password": "carolpass1"})
    carol = (await registered.json())["me"]

    assert bob["balance"] == 10000 and carol["balance"] == 10000
    for account in (bob, carol):
        (row,) = Ledger().rows(account["id"])
        assert row["kind"] == "topup" and row["credits"] == 10000 and row["actor"] == "welcome bonus"
    SettingsStore().update({"new_account_credits": 0})
    none = await env.post("/_hub/api/register", {"username": "dave", "password": "davepass12"})
    assert (await none.json())["me"]["balance"] == 0 and Ledger().rows((await none.json())["me"]["id"]) == []


async def test_wrong_passwords_are_throttled_and_audited(env) -> None:
    await _setup_root(env)
    env.client.session.cookie_jar.clear()
    for _ in range(5):
        wrong = await env.post("/_hub/api/login", {"username": "root", "password": "nope-nope"})
        assert wrong.status == 401

    blocked = await env.post("/_hub/api/login", {"username": "root", "password": "rootpass1"})

    assert blocked.status == 429
    actions = [r["action"] for r in AuditLog().tail()]
    assert actions.count("login_failed") == 5
    assert "nope-nope" not in AuditLog().path.read_text(encoding="utf-8"), "no password in the audit log"


async def test_a_password_is_changed_with_the_current_one(env) -> None:
    await _setup_root(env)

    wrong = await env.post("/_hub/api/password", {"current": "bad", "password": "newpass123"})
    right = await env.post("/_hub/api/password", {"current": "rootpass1", "password": "newpass123"})

    assert wrong.status == 401 and right.status == 200
    await _sign_in_as(env, "root", "newpass123")


# -- the admin API ---------------------------------------------------------------------------


async def test_roles_and_permissions_decide_what_the_admin_api_allows(env) -> None:
    await _setup_root(env)
    created = await env.post("/_hub/api/admin/accounts", {"username": "ops", "password": "opspass12", "role": "admin"})
    assert created.status == 200
    ops_id = (await created.json())["account"]["id"]
    bob = await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})
    bob_id = (await bob.json())["account"]["id"]

    await _sign_in_as(env, "bob", "bobpass12")
    assert (await env.client.get("/_hub/api/admin/accounts")).status == 403

    await _sign_in_as(env, "ops", "opspass12")
    assert (await env.client.get("/_hub/api/admin/accounts")).status == 403, "an admin with no permission"

    await _sign_in_as(env, "root", "rootpass1")
    granted = await env.post(f"/_hub/api/admin/accounts/{ops_id}", {"permissions": ["points.topup", "users.manage"]})
    assert granted.status == 200 and (await granted.json())["account"]["permissions"] == [
        "points.topup",
        "users.manage",
    ]

    await _sign_in_as(env, "ops", "opspass12")
    topped = await env.post(f"/_hub/api/admin/accounts/{bob_id}/topup", {"credits": 40, "note": "welcome"})
    assert topped.status == 200 and (await topped.json())["balance"] == 10000 + 40
    assert (await env.post(f"/_hub/api/admin/accounts/{bob_id}", {"role": "admin"})).status == 403
    assert (
        await env.post("/_hub/api/admin/accounts", {"username": "boss", "password": "bosspass1", "role": "admin"})
    ).status == 403
    assert (await env.post("/_hub/api/admin/settings", {"registration_open": False})).status == 403
    root_id = env.store.by_username("root").id
    assert (await env.post(f"/_hub/api/admin/accounts/{root_id}", {"disabled": True})).status == 403
    (row,) = [r for r in Ledger().rows(bob_id) if r["kind"] == "topup" and r["actor"] != "welcome bonus"]
    assert row["actor"] == "ops" and row["note"] == "welcome"


async def test_the_superadmin_cannot_be_unmade_and_admins_answer_to_the_superadmin_only(env) -> None:
    await _setup_root(env)
    root_id = env.store.by_username("root").id
    ops = await env.post("/_hub/api/admin/accounts", {"username": "ops", "password": "opspass12", "role": "admin"})
    ops_id = (await ops.json())["account"]["id"]
    ops2 = await env.post("/_hub/api/admin/accounts", {"username": "ops2", "password": "opspass12", "role": "admin"})
    ops2_id = (await ops2.json())["account"]["id"]
    await env.post(f"/_hub/api/admin/accounts/{ops_id}", {"permissions": ["users.manage"]})

    demote_self = await env.post(f"/_hub/api/admin/accounts/{root_id}", {"role": "user"})
    disable_self = await env.post(f"/_hub/api/admin/accounts/{root_id}", {"disabled": True})
    await _sign_in_as(env, "ops", "opspass12")
    disable_peer = await env.post(f"/_hub/api/admin/accounts/{ops2_id}", {"disabled": True})
    reset_peer = await env.post(f"/_hub/api/admin/accounts/{ops2_id}", {"password": "takeover123"})

    assert demote_self.status == 403 and disable_self.status == 403
    assert disable_peer.status == 403 and reset_peer.status == 403
    assert env.store.get(root_id).role == "superadmin" and not env.store.get(ops2_id).disabled


async def test_settings_never_send_the_google_secret_back(env) -> None:
    await _setup_root(env)

    saved = await env.post(
        "/_hub/api/admin/settings",
        {"google_client_id": "cid", "google_client_secret": "s3cret-value", "public_url": "https://raven.example.com"},
    )
    kept = await env.post("/_hub/api/admin/settings", {"google_client_secret": "", "registration_open": False})
    shown = await (await env.client.get("/_hub/api/admin/settings")).json()

    assert saved.status == 200 and kept.status == 200
    assert "s3cret-value" not in json.dumps(shown) and shown["settings"]["google_client_secret_set"] is True
    assert SettingsStore().load().google_client_secret == "s3cret-value", "an empty field keeps the stored secret"
    assert "s3cret-value" not in AuditLog().path.read_text(encoding="utf-8")


async def test_prices_are_set_and_removed(env) -> None:
    await _setup_root(env)

    bad = await env.post("/_hub/api/admin/prices", {"model": "gpt-4o", "input": -1, "output": 1})
    good = await env.post("/_hub/api/admin/prices", {"model": "gpt-4o", "input": 2.5, "output": 10, "cacheHit": 1.25})
    table = await (await env.client.get("/_hub/api/admin/prices")).json()
    removed = await env.post("/_hub/api/admin/prices", {"model": "gpt-4o", "remove": True})

    assert bad.status == 400 and good.status == 200
    assert table["prices"]["gpt-4o"] == {"input": 2.5, "output": 10.0, "cacheHit": 1.25}
    assert "gpt-4o" not in (await removed.json())["prices"]


async def test_a_disabled_account_is_signed_out_everywhere(env) -> None:
    await _setup_root(env)
    bob_id = (await (await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})).json())[
        "account"
    ]["id"]
    await _sign_in_as(env, "bob", "bobpass12")
    bob_cookie = next(c for c in env.client.session.cookie_jar if c.key == COOKIE).value

    await _sign_in_as(env, "root", "rootpass1")
    assert (await env.post(f"/_hub/api/admin/accounts/{bob_id}", {"disabled": True})).status == 200

    env.client.session.cookie_jar.clear()
    env.client.session.cookie_jar.update_cookies({COOKIE: bob_cookie})
    assert (await env.client.get("/_hub/api/me")).status == 401
    assert (await env.post("/_hub/api/login", {"username": "bob", "password": "bobpass12"})).status == 403


# -- the relay to an engine ------------------------------------------------------------------


async def _bob_with_engine(env):
    await _setup_root(env)
    await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})
    me = await _sign_in_as(env, "bob", "bobpass12")
    first = await env.client.get("/", allow_redirects=False)
    assert first.status == 503 and "Starting" in await first.text()
    for _ in range(240):
        engine = env.supervisor.engine(me["id"])
        if engine is not None and engine.state in ("running", "failed"):
            break
        await asyncio.sleep(0.25)
    assert engine.state == "running", engine.error
    return me, engine


async def test_nobody_reaches_an_engine_without_signing_in(env) -> None:
    page = await env.client.get("/", allow_redirects=False)
    api = await env.client.get("/echo")

    assert page.status == 302 and page.headers["Location"] == "/_hub/login"
    assert api.status == 401


async def test_a_signed_in_person_reaches_their_own_engine_with_its_token(env) -> None:
    me, engine = await _bob_with_engine(env)

    page = await env.client.get("/")
    echoed = await (
        await env.client.post("/echo?x=1", data=b"payload", headers={"Origin": env.origin, "X-Raven-Token": "forged"})
    ).json()
    engine_sign_in = await env.client.get("/auth")

    body = await page.text()
    assert "engine page" in body and '<script src="/_hub/static/bar.js" defer></script></head>' in body
    headers = echoed["headers"]
    assert headers["x-raven-token"] == engine.token, "the engine's own token, not what the browser sent"
    assert "cookie" not in headers and "origin" not in headers
    assert echoed["query"] == {"x": "1"} and echoed["body"] == "payload" and echoed["method"] == "POST"
    assert Path(echoed["home"]) == user_home(me["id"])
    assert Path(echoed["claude"]) == user_home(me["id"]) / "cli" / "claude"
    assert engine_sign_in.status == 404, "the engine's own sign-in is never relayed"


async def test_the_socket_is_relayed_from_this_origin_only(env) -> None:
    await _bob_with_engine(env)

    async with env.client.ws_connect("/rpc", headers={"Origin": env.origin}) as ws:
        await ws.send_str("hello")
        assert (await ws.receive(timeout=10)).data == "echo:hello"
        await ws.send_bytes(b"\x01\x02")
        assert (await ws.receive(timeout=10)).data == b"echo:\x01\x02"
    with pytest.raises(WSServerHandshakeError):
        async with env.client.ws_connect("/rpc", headers={"Origin": "http://evil.example"}):
            pass


async def test_an_engine_already_running_on_its_home_is_adopted_not_started_twice(env) -> None:
    import os
    import subprocess

    await _setup_root(env)
    bob_id = (await (await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})).json())[
        "account"
    ]["id"]
    home = user_home(bob_id)
    port = _free_port_base()
    running = subprocess.Popen(
        [sys.executable, FAKE_ENGINE, str(port)],
        env=dict(os.environ, RAVEN_HOME=str(home), RAVEN_SERVE_TOKEN="already-running"),
    )
    try:
        for _ in range(120):
            if (home / "serve.json").exists():
                break
            await asyncio.sleep(0.25)
        engine = await env.supervisor.ensure(env.store.get(bob_id))

        assert engine.adopted and engine.page_port == port and engine.token == "already-running"
        assert engine.pid, "the pid serve.json names (a venv launcher on Windows runs the real one as its child)"
        await env.supervisor.stop_all()
        assert running.poll() is None, "an adopted engine was running before the gateway and is left running"
    finally:
        running.kill()
        running.wait()


async def test_each_account_has_its_own_engine(env) -> None:
    bob, bob_engine = await _bob_with_engine(env)
    await _sign_in_as(env, "root", "rootpass1")
    await env.post("/_hub/api/admin/accounts", {"username": "carol", "password": "carolpass1"})
    carol = await _sign_in_as(env, "carol", "carolpass1")
    await env.client.get("/")
    for _ in range(240):
        engine = env.supervisor.engine(carol["id"])
        if engine is not None and engine.state == "running":
            break
        await asyncio.sleep(0.25)

    echoed = await (await env.client.get("/echo")).json()

    assert engine.page_port != bob_engine.page_port and engine.token != bob_engine.token
    assert Path(echoed["home"]) == user_home(carol["id"]) != user_home(bob["id"])


# -- metering and the gate -------------------------------------------------------------------


async def test_an_account_at_zero_is_blocked_and_a_top_up_lifts_it(env) -> None:
    await _setup_root(env)
    SettingsStore().update({"new_account_credits": 0})  # starts at zero, as the case needs
    bob_id = (await (await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})).json())[
        "account"
    ]["id"]
    home = user_home(bob_id)
    PriceTable().save({"m": Price(1000, 0, 0)})

    env.hub.meter_once()
    gate_file = json.loads((home / gate.GATE_FILE).read_text(encoding="utf-8"))
    assert gate_file["blocked"] and gate_file["reason"] == gate.NO_CREDITS

    await env.post(f"/_hub/api/admin/accounts/{bob_id}/topup", {"credits": 100})
    assert json.loads((home / gate.GATE_FILE).read_text(encoding="utf-8"))["blocked"] is False

    env.hub.meter_once()  # the cursor starts at the end of what is there
    usage = home / "telemetry" / f"usage-{time.strftime('%Y-%m-%d')}.jsonl"
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text(json.dumps({"model": "m", "input_tokens": 2000}) + "\n", encoding="utf-8")
    env.hub.meter_once()

    assert env.store.get(bob_id).balance == pytest.approx(100 - 2000 * 1000 / 1e6 * 100 * 0.1)
    usage.open("a", encoding="utf-8").write(json.dumps({"model": "m", "input_tokens": 100000}) + "\n")
    env.hub.meter_once()
    assert env.store.get(bob_id).balance < 0
    assert json.loads((home / gate.GATE_FILE).read_text(encoding="utf-8"))["blocked"] is True
    assert {"blocked", "unblocked"} <= {r["action"] for r in AuditLog().tail()}
    root = env.store.by_username("root")
    assert root.id not in env.hub.blocked, "the superadmin is never blocked"


async def test_over_the_disk_allowance_is_blocked_but_not_an_administrator(env) -> None:
    await _setup_root(env)
    SettingsStore().update({"new_account_credits": 0})  # starts at zero, as the case needs
    bob_id = (await (await env.post("/_hub/api/admin/accounts", {"username": "bob", "password": "bobpass12"})).json())[
        "account"
    ]["id"]
    ops = (
        await (
            await env.post("/_hub/api/admin/accounts", {"username": "ops", "password": "opspass12", "role": "admin"})
        ).json()
    )["account"]
    for account_id in (bob_id, ops["id"]):
        env.store.adjust_balance(account_id, 50)
        home = user_home(account_id)
        home.mkdir(parents=True, exist_ok=True)
        (home / "big.bin").write_bytes(b"x" * 4096)
    SettingsStore().update({"disk_quota_gb": 0.000001})

    env.hub.disk_once()
    env.hub.meter_once()

    assert "disk allowance" in env.hub.blocked[bob_id]
    assert ops["id"] not in env.hub.blocked


# -- Google Sign-In --------------------------------------------------------------------------


def _google_keys():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def _google_round(env, key, *, sub="g-1", email="bob@gmail.com", verified=True, nonce_override=None):
    import jwt

    start = await env.client.get("/_hub/google/start", allow_redirects=False)
    assert start.status == 302, await start.text()
    location = urlparse(start.headers["Location"])
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert location.netloc == "accounts.google.com" and query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == f"{env.origin}/_hub/google/callback"

    async def exchange(form):
        assert form["code_verifier"] and form["client_secret"] == "gsecret"
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "gid",
            "sub": sub,
            "email": email,
            "email_verified": verified,
            "name": "Bob G",
            "nonce": nonce_override or query["nonce"],
            "iat": now,
            "exp": now + 600,
        }
        return {"id_token": jwt.encode(claims, key, algorithm="RS256")}

    env.google["instance"]._exchange = exchange
    return await env.client.get(f"/_hub/google/callback?state={query['state']}&code=abc", allow_redirects=False)


async def test_google_sign_in_makes_an_account_and_signs_in_to_it_again(env) -> None:
    await _setup_root(env)
    key = _google_keys()
    env.google["key_for"] = lambda _token: key.public_key()
    SettingsStore().update({"google_client_id": "gid", "google_client_secret": "gsecret", "public_url": env.origin})
    env.client.session.cookie_jar.clear()

    first = await _google_round(env, key)
    me = await (await env.client.get("/_hub/api/me")).json()
    env.client.session.cookie_jar.clear()
    second = await _google_round(env, key)
    again = await (await env.client.get("/_hub/api/me")).json()

    assert first.status == 200 and "Signed in" in await first.text()
    assert me["me"]["username"] == "bob" and me["me"]["google_linked"] and me["me"]["email"] == "bob@gmail.com"
    assert me["me"]["balance"] == 10000, "a Google sign-up gets the welcome bonus too"
    assert second.status == 200 and again["me"]["id"] == me["me"]["id"]


async def test_google_tokens_that_do_not_verify_sign_nobody_in(env) -> None:
    await _setup_root(env)
    key, other = _google_keys(), _google_keys()
    env.google["key_for"] = lambda _token: other.public_key()
    SettingsStore().update({"google_client_id": "gid", "google_client_secret": "gsecret", "public_url": env.origin})
    env.client.session.cookie_jar.clear()

    forged = await _google_round(env, key)
    env.google["key_for"] = lambda _token: key.public_key()
    env.hub._google = None
    replayed = await _google_round(env, key, nonce_override="someone-elses")
    env.hub._google = None
    unverified = await _google_round(env, key, verified=False)
    stray = await env.client.get("/_hub/google/callback?state=made-up&code=abc", allow_redirects=False)

    assert forged.status == 400 and "did not verify" in await forged.text()
    assert replayed.status == 400 and unverified.status == 400 and stray.status == 400
    assert (await env.client.get("/_hub/api/me")).status == 401
    assert env.store.count() == 1


async def test_without_keys_google_sign_in_is_not_offered(env) -> None:
    await _setup_root(env)

    state = await (await env.client.get("/_hub/api/state")).json()
    start = await env.client.get("/_hub/google/start", allow_redirects=False)

    assert state["google"] is False and start.status == 400

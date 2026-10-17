"""``rarus.account``: the page's view of the signed-in account."""

from __future__ import annotations

import pytest

from raven.providers import rarus_client, rarus_session
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.rarus import register_rarus_methods, rarus_account_status

KEY = "sk-ab-" + "0123456789abcdef" * 3


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "rarus-home"))
    monkeypatch.setenv(rarus_session.STORE_ENV, "plain")


async def test_nobody_signed_in():
    assert await rarus_account_status({}) == {"signed_in": False, "email": "", "balance": None, "error": ""}


async def test_the_balance_comes_from_the_server(monkeypatch):
    rarus_session.save_session(KEY, email="old@example.com")
    monkeypatch.setattr(rarus_client, "whoami", lambda key, **_: {"email": "me@example.com", "balance": 1200})

    assert await rarus_account_status({}) == {
        "signed_in": True,
        "email": "me@example.com",
        "balance": 1200,
        "error": "",
    }


async def test_a_server_that_is_down_still_names_the_person(monkeypatch):
    rarus_session.save_session(KEY, email="me@example.com")

    def down(key, **_):
        raise rarus_client.RarusError("network", "down")

    monkeypatch.setattr(rarus_client, "whoami", down)

    out = await rarus_account_status({})

    assert out["signed_in"] is True
    assert out["email"] == "me@example.com"
    assert out["balance"] is None
    assert out["error"] == "network"


async def test_a_refused_key_signs_the_person_out(monkeypatch):
    rarus_session.save_session(KEY)

    def refuse(key, **_):
        raise rarus_client.RarusError("unauthorized", "no", status=401)

    monkeypatch.setattr(rarus_client, "whoami", refuse)

    out = await rarus_account_status({})

    assert out["signed_in"] is False
    assert out["error"] == "unauthorized"
    assert rarus_session.load_key() is None


async def test_the_method_is_registered():
    dispatcher = Dispatcher()
    register_rarus_methods(dispatcher)
    reply = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "rarus.account", "params": {}})
    assert reply["result"]["signed_in"] is False

"""Remote pairing: a valid ticket pairs a device, a wrong one is refused, and the token opens the gateway."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from typer.testing import CliRunner

from raven.auth.pairing import PairingError, PairingStore


def test_a_valid_ticket_pairs_a_device_once(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json")
    ticket, ttl = store.issue_ticket()

    token = store.redeem(ticket, "phone")

    assert ttl == 300 and token
    assert store.device_for(token) == "phone"
    with pytest.raises(PairingError, match="wrong or was already used"):
        store.redeem(ticket, "tablet")


def test_a_wrong_or_expired_ticket_pairs_nothing(tmp_path: Path) -> None:
    now = [1000.0]
    store = PairingStore(tmp_path / "pairing.json", clock=lambda: now[0])
    ticket, _ = store.issue_ticket(ttl_s=60)

    with pytest.raises(PairingError, match="wrong"):
        store.redeem("not-the-ticket", "phone")
    now[0] += 61
    with pytest.raises(PairingError, match="expired"):
        store.redeem(ticket, "phone")
    assert store.devices() == []


def test_only_hashes_are_kept_and_a_revoked_device_is_shut_out(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json")
    ticket, _ = store.issue_ticket()
    token = store.redeem(ticket, "phone")

    stored = (tmp_path / "pairing.json").read_text(encoding="utf-8")
    assert ticket not in stored and token not in stored
    assert store.revoke("phone") and store.device_for(token) is None
    assert not store.revoke("phone")


def test_a_device_name_is_paired_once(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json")
    store.redeem(store.issue_ticket()[0], "phone")
    with pytest.raises(PairingError, match="already paired"):
        store.redeem(store.issue_ticket()[0], "phone")


async def test_the_gateway_pairs_over_http_and_then_accepts_the_device_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    from raven.auth.pairing import default_store
    from raven.rpc.transports.ws import WsGateway

    gateway = WsGateway()

    async def protected(request: web.Request) -> web.Response:
        return web.json_response({"authorized": gateway._authorized(request)})

    app = web.Application()
    app.router.add_post("/pair", gateway.handle_pair)
    app.router.add_get("/protected", protected)
    ticket, _ = default_store().issue_ticket()

    async with TestClient(TestServer(app)) as client:
        wrong = await client.post("/pair", json={"ticket": "guess", "device": "phone"})
        right = await client.post("/pair", json={"ticket": ticket, "device": "phone"})
        again = await client.post("/pair", json={"ticket": ticket, "device": "phone"})
        body = await right.json()
        with_token = await client.get("/protected", headers={"X-Raven-Device-Token": body["token"]})
        without = await client.get("/protected")
        forged = await client.get("/protected", headers={"X-Raven-Device-Token": "forged"})

        assert wrong.status == 403 and (await wrong.json())["ok"] is False
        assert right.status == 200 and body["device"] == "phone"
        assert again.status == 403
        assert (await with_token.json())["authorized"] is True
        assert (await without.json())["authorized"] is False
        assert (await forged.json())["authorized"] is False


def test_the_command_line_pairs_lists_and_revokes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    from raven.auth.pairing import default_store
    from raven.cli.commands import app

    runner = CliRunner()
    issued = runner.invoke(app, ["remote", "pair"])
    assert issued.exit_code == 0, issued.output
    ticket = issued.output.split("ticket: ", 1)[1].split()[0]
    default_store().redeem(ticket, "laptop")

    listed = runner.invoke(app, ["remote", "devices"])
    revoked = runner.invoke(app, ["remote", "revoke", "laptop"])
    missing = runner.invoke(app, ["remote", "revoke", "laptop"])

    assert "laptop" in listed.output
    assert revoked.exit_code == 0 and missing.exit_code == 1
    assert json.loads((tmp_path / "remote" / "pairing.json").read_text(encoding="utf-8"))["devices"] == []

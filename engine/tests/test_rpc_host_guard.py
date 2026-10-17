"""The Host-header guard: a name that is not loopback reaches nothing (DNS rebinding)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.rpc.transports.ws import WsGateway, _host_name, build_app


@pytest.fixture
async def client():
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None))
    test_client = TestClient(server)
    await test_client.start_server()
    gateway.port = server.port
    try:
        yield test_client
    finally:
        await test_client.close()


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("127.0.0.1:18792", "127.0.0.1"),
        ("localhost", "localhost"),
        ("LOCALHOST:80", "localhost"),
        ("[::1]:18792", "[::1]"),
        ("evil.example:18792", "evil.example"),
        ("", ""),
    ],
)
def test_the_host_part_is_taken_without_its_port(header, name):
    assert _host_name(header) == name


async def test_the_loopback_name_the_page_uses_is_served(client):
    reply = await client.get("/health")
    assert reply.status == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:18792", "127.0.0.1.evil.example", "10.0.0.5:18792"])
async def test_a_foreign_host_is_refused_on_every_route(client, host):
    for path in ("/health", "/auth", "/rpc", "/file"):
        reply = await client.get(path, headers={"Host": host})
        assert reply.status == 403, (path, host)


async def test_localhost_by_name_is_accepted(client):
    reply = await client.get("/health", headers={"Host": "localhost:1"})
    assert reply.status == 200


async def test_an_operator_can_add_a_name(client, monkeypatch):
    monkeypatch.setenv("RARUS_ALLOWED_HOSTS", "gateway.internal, other.internal")
    reply = await client.get("/health", headers={"Host": "gateway.internal:18792"})
    assert reply.status == 200
    reply = await client.get("/health", headers={"Host": "evil.example"})
    assert reply.status == 403

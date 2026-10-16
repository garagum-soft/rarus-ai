"""Live preview: the gateway streams a file's rewrites to an open preview over server-sent events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.rpc.file_watch import file_changes


async def test_a_rewrite_is_reported_with_its_new_size(tmp_path: Path) -> None:
    doc = tmp_path / "deck.pptx"
    doc.write_bytes(b"v1")
    stream = file_changes(doc, poll_s=0.05, heartbeat_s=10)

    async def rewrite() -> None:
        await asyncio.sleep(0.15)
        doc.write_bytes(b"version two")

    writer = asyncio.create_task(rewrite())
    change = await asyncio.wait_for(anext(stream), 3)
    await writer

    assert change == {"mtime": doc.stat().st_mtime, "size": 11}


async def test_a_quiet_file_sends_heartbeats_only(tmp_path: Path) -> None:
    doc = tmp_path / "quiet.docx"
    doc.write_bytes(b"x")
    stream = file_changes(doc, poll_s=0.02, heartbeat_s=0.06)
    assert await asyncio.wait_for(anext(stream), 2) is None


async def _client(gateway) -> TestClient:
    app = web.Application()
    app.router.add_get("/file/watch", gateway.handle_file_watch)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_an_open_preview_receives_the_change_without_reloading(tmp_path: Path, monkeypatch) -> None:
    from raven.rpc import file_watch
    from raven.rpc.transports.ws import WsGateway

    monkeypatch.setattr(file_watch, "POLL_S", 0.05)
    monkeypatch.setattr(file_watch.file_changes, "__defaults__", (0.05, 10.0))
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"draft")
    gateway = WsGateway()
    client = await _client(gateway)
    try:
        response = await client.get(
            "/file/watch", params={"path": str(doc)}, headers={"X-Raven-Token": gateway.session_token}
        )
        assert response.status == 200 and response.headers["Content-Type"].startswith("text/event-stream")
        assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 3) == b"event: ready\ndata: {}\n\n"

        doc.write_bytes(b"final text the agent saved")
        event = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)

        head, data = event.decode().strip().split("\n")
        assert head == "event: change" and json.loads(data.removeprefix("data: "))["size"] == 26
        assert gateway._file_watches == 1
        response.close()
    finally:
        await client.close()


async def test_a_preview_without_a_session_is_refused(tmp_path: Path) -> None:
    from raven.rpc.transports.ws import WsGateway

    doc = tmp_path / "secret.xlsx"
    doc.write_bytes(b"x")
    client = await _client(WsGateway())
    try:
        refused = await client.get("/file/watch", params={"path": str(doc)})
        assert refused.status == 401
    finally:
        await client.close()


async def test_the_number_of_open_previews_is_capped(tmp_path: Path) -> None:
    from raven.rpc.file_watch import MAX_WATCHES
    from raven.rpc.transports.ws import WsGateway

    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"x")
    gateway = WsGateway()
    gateway._file_watches = MAX_WATCHES
    client = await _client(gateway)
    try:
        busy = await client.get(
            "/file/watch", params={"path": str(doc)}, headers={"X-Raven-Token": gateway.session_token}
        )
        assert busy.status == 503
    finally:
        await client.close()

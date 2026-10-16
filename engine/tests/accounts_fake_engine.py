"""A stand-in for an account's engine in the accounts-mode tests: ``python accounts_fake_engine.py <page port>``.

Answers ``/health`` as ``raven-serve`` does, refuses everything else without the
token the supervisor handed it (``RAVEN_SERVE_TOKEN``), serves a page with a
``</head>``, echoes the headers it was sent, echoes ``/rpc`` WebSocket frames, and
leaves a ``serve.json`` in its ``RAVEN_HOME`` the way the real gateway does.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from aiohttp import WSMsgType, web

TOKEN = os.environ.get("RAVEN_SERVE_TOKEN", "")


def _authorized(request: web.Request) -> bool:
    return request.headers.get("X-Raven-Token") == TOKEN


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "raven-serve", "account": os.environ.get("RAVEN_ACCOUNT")})


async def page(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=401, text="no token")
    return web.Response(text="<!doctype html><html><head><title>engine</title></head><body>engine page</body></html>", content_type="text/html")


async def echo(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=401, text="no token")
    body = await request.read()
    return web.json_response(
        {
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "method": request.method,
            "query": dict(request.query),
            "body": body.decode("utf-8", "replace"),
            "home": os.environ.get("RAVEN_HOME"),
            "claude": os.environ.get("CLAUDE_CONFIG_DIR"),
            "codex": os.environ.get("CODEX_HOME"),
            "chatgpt": os.environ.get("CHATGPT_TOKEN_DIR"),
        }
    )


async def auth(_request: web.Request) -> web.Response:
    return web.Response(text="engine sign-in")


async def rpc(request: web.Request) -> web.StreamResponse:
    if not _authorized(request):
        return web.Response(status=401, text="no token")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for message in ws:
        if message.type == WSMsgType.TEXT:
            await ws.send_str("echo:" + message.data)
        elif message.type == WSMsgType.BINARY:
            await ws.send_bytes(b"echo:" + message.data)
    return ws


def main() -> None:
    port = int(sys.argv[1])
    home = Path(os.environ["RAVEN_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "serve.json").write_text(json.dumps({"port": port, "token": TOKEN, "pid": os.getpid()}), encoding="utf-8")
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", page)
    app.router.add_route("*", "/echo", echo)
    app.router.add_get("/auth", auth)
    app.router.add_get("/rpc", rpc)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()

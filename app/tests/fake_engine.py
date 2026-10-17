"""A stand-in for ``rarus serve``: the same files and endpoints the shell relies on, nothing else.

usage: fake_engine.py <port> [--die]
Writes ``$RARUS_HOME/serve.json`` once it listens, answers /health and POST /auth/nonce.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "test-token-" + "x" * 20


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "raven-serve"})
        else:
            self._json(404, {})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/auth/nonce" and self.headers.get("X-Raven-Token") == TOKEN:
            self._json(200, {"nonce": "nonce-123"})
        else:
            self._json(403, {})

    def log_message(self, *args: object) -> None:
        return


def main() -> None:
    port = int(sys.argv[1])
    if "--die" in sys.argv:
        sys.exit(3)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    home = Path(os.environ["RARUS_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "serve.json").write_text(json.dumps({"port": port, "token": TOKEN, "pid": os.getpid()}))
    server.serve_forever()


if __name__ == "__main__":
    main()

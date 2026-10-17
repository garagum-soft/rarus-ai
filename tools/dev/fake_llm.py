"""A stand-in for the alembox agent API with a scripted model, for checking chats without a real one.

    python fake_llm.py <port> [<log-file>]

It answers ``/whoami`` and ``/models`` like ``fake_site.py`` and, on ``/chat/completions``, streams a
canned reply word by word (or returns it whole when the request does not ask for a stream). What it
says depends on the last user message:

* the request that names a new chat (its only tool is ``emit_session_title``) gets the title ``Тестовый чат``;
* ``SLOW`` in the message makes the reply long (about 40 s), so a stop button can be pressed;
* ``TOOL write <name>`` makes it call ``write_file`` once with a small file, then, after the tool's
  result comes back, say that the file is written;
* anything else gets a short fixed reply that echoes the message.

Every request body is appended to the log file (one JSON line each), so a run can be inspected.
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2] if len(sys.argv) > 2 else None


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _plan(body: dict) -> dict:
    """What to say: {'text': str, 'delay': float} or {'tool': {...}}."""
    messages = body.get("messages") or []
    last_user = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    last = messages[-1] if messages else {}
    names = [(t.get("function") or {}).get("name") for t in body.get("tools") or []]
    if names == ["emit_session_title"]:
        return {"tool": {"name": "emit_session_title", "arguments": json.dumps({"title": "Тестовый чат"}, ensure_ascii=False)}}
    if last.get("role") == "tool":
        return {"text": "Файл записан.", "delay": 0.05}
    found = re.search(r"TOOL write (\S+)", last_user)
    if found and body.get("tools"):
        args = {"path": found.group(1), "content": "создано тестовой моделью\n"}
        return {"tool": {"name": "write_file", "arguments": json.dumps(args)}}
    if "SLOW" in last_user:
        return {"text": " ".join(f"слово{i}" for i in range(200)), "delay": 0.2}
    return {"text": f"Это ответ тестовой модели на сообщение: {last_user[:60]}", "delay": 0.12}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.endswith("/whoami"):
            self._json(200, {"email": "demo@example.com", "balance": 12400, "key_prefix": "sk-ab-0123"})
        elif self.path.endswith("/models"):
            self._json(200, {"data": [{"id": "deepseek-flash"}, {"id": "auto"}, {"id": "qwen-max"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if LOG:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": self.path, "body": body}, ensure_ascii=False) + "\n")
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        plan = _plan(body)
        model = body.get("model", "deepseek-flash")
        usage = {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}
        if not body.get("stream"):
            message = {"role": "assistant", "content": plan.get("text")}
            finish = "stop"
            if "tool" in plan:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": plan["tool"]}],
                }
                finish = "tool_calls"
            self._json(
                200,
                {
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(delta: dict, finish: str | None = None, with_usage: bool = False) -> None:
            chunk = {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if with_usage:
                chunk["usage"] = usage
            self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.flush()

        try:
            send({"role": "assistant", "content": ""})
            if "tool" in plan:
                call = {"index": 0, "id": "call_1", "type": "function", "function": plan["tool"]}
                send({"tool_calls": [call]})
                send({}, "tool_calls", True)
            else:
                for word in plan["text"].split(" "):
                    send({"content": word + " "})
                    time.sleep(plan["delay"])
                send({}, "stop", True)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()

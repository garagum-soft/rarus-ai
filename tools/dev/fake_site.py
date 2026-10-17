"""A stand-in for the alembox agent API (whoami, models) so the demo engine can show a balance."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/whoami"):
            body = {"email": "demo@example.com", "balance": 12400, "key_prefix": "sk-ab-0123"}
        elif self.path.endswith("/models"):
            body = {"data": [{"id": "deepseek-flash"}, {"id": "auto"}, {"id": "qwen-max"}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()

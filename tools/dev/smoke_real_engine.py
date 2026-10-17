"""Start the REAL engine through the shell's Engine class and check page, health, nonce exchange."""
import os
import sys
import tempfile

home = tempfile.mkdtemp(prefix="rarus-smoke-")
os.environ["RARUS_HOME"] = home
os.environ["RAVEN_HOME"] = home
os.environ["RARUS_SECRET_STORE"] = "plain"

import httpx

from rarus_app.controller import Controller

ctl = Controller(open_browser=False)
ctl.start_engine(wait=True)
print("boot:", ctl.boot())
eng = ctl._engine
if eng is None:
    print("ENGINE FAILED", ctl._engine_error)
    sys.exit(1)
try:
    base = eng.state.base_url
    print("health:", httpx.get(f"{base}/health").json())
    page = httpx.get(f"{base}/")
    print("page:", page.status_code, len(page.text), "<title>" in page.text)
    url = eng.auth_url()
    print("auth url:", url.split("#")[0])
    # unauthenticated rpc must be refused
    r = httpx.get(f"{base}/file", params={"path": "C:/Windows/win.ini"})
    print("file without session:", r.status_code)
    r = httpx.get(f"{base}/health", headers={"Host": "evil.example"})
    print("health with foreign Host:", r.status_code)
finally:
    ctl.stop_engine()
print("alive after stop:", ctl.engine_alive())

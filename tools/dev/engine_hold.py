"""Start the real engine in a temp home, print a one-time URL, keep it up for N seconds."""
import os
import sys
import tempfile
import time

home = os.environ.get("HOLD_HOME") or tempfile.mkdtemp(prefix="rarus-hold-")
os.environ["RARUS_HOME"] = home
os.environ["RAVEN_HOME"] = home
os.environ["RARUS_SECRET_STORE"] = "plain"

from rarus_app import account
from rarus_app.controller import Controller

account.first_run_language()
ctl = Controller(open_browser=False)
ctl.start_engine(wait=True)
eng = ctl._engine
if eng is None:
    print("ENGINE FAILED", ctl._engine_error, flush=True)
    sys.exit(1)
print("URL", eng.auth_url(), flush=True)
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 600)
ctl.stop_engine()

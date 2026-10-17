import asyncio
import json
import os

sp = os.path.dirname(os.path.abspath(__file__))
os.environ["RARUS_HOME"] = os.path.join(sp, "hold_home")
os.environ["RAVEN_HOME"] = os.environ["RARUS_HOME"]
os.environ["RARUS_SECRET_STORE"] = "plain"

from raven.config.paths import get_config_path
from raven.rpc.methods.setup import _detect_provider_configured, setup_status

print(asyncio.run(setup_status({})))
payload = json.loads(get_config_path().read_text(encoding="utf-8"))
print(json.dumps(payload.get("agents"), indent=1)[:400])
print("providers keys:", list((payload.get("providers") or {}).keys()))

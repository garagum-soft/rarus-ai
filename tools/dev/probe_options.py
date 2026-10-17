import asyncio
import os
import time

sp = os.path.dirname(os.path.abspath(__file__))
os.environ["RARUS_HOME"] = os.path.join(sp, "hold_home")
os.environ["RAVEN_HOME"] = os.environ["RARUS_HOME"]
os.environ["RARUS_SECRET_STORE"] = "plain"

from raven.rpc.methods.model import model_options


async def main():
    t = time.time()
    out = await model_options({})
    print("seconds", round(time.time() - t, 1))
    print("model", out["model"], "provider", out["provider"], "n", len(out["providers"]))
    for p in out["providers"]:
        if p["slug"] == "rarus":
            print({k: p[k] for k in ("slug", "authenticated", "is_current", "models", "warning")})
    print("authenticated:", [p["slug"] for p in out["providers"] if p["authenticated"]])


asyncio.run(main())

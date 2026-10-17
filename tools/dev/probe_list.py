import os
sp = os.path.dirname(os.path.abspath(__file__))
os.environ["RARUS_HOME"] = os.path.join(sp, "hold_home")
os.environ["RAVEN_HOME"] = os.environ["RARUS_HOME"]
os.environ["RARUS_SECRET_STORE"] = "plain"
from raven.config.update_providers import list_providers
from raven.rpc.methods.model import _build_provider_entry
rows = {p["name"]: p for p in list_providers()}
print("rarus row:", {k: rows["rarus"][k] for k in ("name", "configured", "is_oauth", "api_key_redacted")})
e = _build_provider_entry("rarus", current_provider="rarus")
print({k: e[k] for k in e if k in ("slug", "authenticated", "configured", "models", "warning", "kind")})

"""Add (or replace) catalogue keys: python add_keys.py keys.json  -- {key: {en, zh, ru}}."""
import json
import sys

CATALOG = r"C:\235235\rarus-ai\engine\i18n\messages.json"
new = json.load(open(sys.argv[1], encoding="utf-8"))
raw = open(CATALOG, encoding="utf-8", newline="").read()
data = json.loads(raw)
for key, cols in new.items():
    assert set(cols) == {"en", "zh", "ru"}, key
    data["ui"][key] = cols
open(CATALOG, "w", encoding="utf-8", newline="").write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print("keys:", len(new), "total ui:", len(data["ui"]))

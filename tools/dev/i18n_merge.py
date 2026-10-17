"""Merge the Russian chunks into i18n/messages.json (ui section), with checks."""
import json
import os
import re
import sys

SP = os.path.dirname(os.path.abspath(__file__))
PATH = r"C:\235235\rarus-ai\engine\i18n\messages.json"
raw = open(PATH, encoding="utf-8").read()
data = json.loads(raw)
ui = data["ui"]
keys = json.load(open(os.path.join(SP, "ru", "keys.json"), encoding="utf-8"))

# round-trip check: the dump must reproduce the file byte for byte, or the diff would be noise
dumped = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
if dumped != raw:
    print("ROUND TRIP DIFFERS", len(dumped), len(raw))
    for i, (a, b) in enumerate(zip(dumped, raw)):
        if a != b:
            print(repr(dumped[i - 60 : i + 60]))
            print(repr(raw[i - 60 : i + 60]))
            break
    pass

ru = {}
for name in sorted(os.listdir(os.path.join(SP, "ru"))):
    if not (name.startswith("ru_") and name.endswith(".txt")):
        continue
    for line in open(os.path.join(SP, "ru", name), encoding="utf-8").read().splitlines():
        if not line.strip():
            continue
        head, _, text = line.partition("|")
        n = int(head.lstrip("@"))
        if n in ru:
            print("DUPLICATE", n)
            sys.exit(1)
        ru[n] = text

missing = [n for n in range(len(keys)) if n not in ru]
print("translated", len(ru), "of", len(keys), "missing", len(missing), missing[:10])

PH = re.compile(r"\{\{[^}]*\}\}|\{\w+\}")
TAG = re.compile(r"</?\w+>")
bad = 0
for n, text in sorted(ru.items()):
    key = keys[n]
    en = ui[key].get("en", "")
    if sorted(PH.findall(en)) != sorted(PH.findall(text)):
        print("PLACEHOLDERS", n, key, "|", en, "|", text)
        bad += 1
    if sorted(TAG.findall(en)) != sorted(TAG.findall(text)):
        print("TAGS", n, key, "|", en, "|", text)
        bad += 1
    if not text.strip() and en.strip():
        print("EMPTY", n, key)
        bad += 1
print("problems", bad)
if bad or missing:
    if "--force" not in sys.argv:
        sys.exit(1)

for n, text in ru.items():
    key = keys[n]
    en = ui[key].get("en", "")
    value = text.replace("\\n", "\n") if "\n" in en else text
    ui[key]["ru"] = value

extra = {
    "gui.onb.lang_ru": {"en": "Русский", "zh": "俄语", "ru": "Русский"},
    "gui.settings.general.lang_ru": {"en": "Русский", "zh": "俄语", "ru": "Русский"},
}
for k, v in extra.items():
    ui.setdefault(k, v)
    ui[k].setdefault("ru", v["ru"])

out = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
if "--write" in sys.argv:
    open(PATH, "w", encoding="utf-8", newline="").write(out)
    print("written")

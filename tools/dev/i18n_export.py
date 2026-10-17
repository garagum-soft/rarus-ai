"""Export the English UI strings as numbered lines, in chunks, for translation."""
import json
import os

SP = os.path.dirname(os.path.abspath(__file__))
d = json.load(open(r"C:\235235\rarus-ai\engine\i18n\messages.json", encoding="utf-8"))
keys = [k for k, v in d["ui"].items() if "ru" not in v]
os.makedirs(os.path.join(SP, "ru"), exist_ok=True)
json.dump(keys, open(os.path.join(SP, "ru", "keys.json"), "w", encoding="utf-8"), ensure_ascii=False)
CH = 300
for i in range(0, len(keys), CH):
    lines = []
    for n in range(i, min(i + CH, len(keys))):
        en = d["ui"][keys[n]].get("en")
        if en is None:
            en = "<<NO EN; zh: " + d["ui"][keys[n]].get("zh", "") + ">>"
        lines.append(f"@{n:04d}|{en.replace(chr(10), chr(92) + chr(110))}")
    open(os.path.join(SP, "ru", f"en_{i // CH:02d}.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(len(keys), "keys,", (len(keys) + CH - 1) // CH, "chunks")

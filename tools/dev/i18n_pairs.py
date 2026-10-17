import json, os, sys
SP = os.path.dirname(os.path.abspath(__file__))
keys = json.load(open(os.path.join(SP, "ru", "keys.json"), encoding="utf-8"))
d = json.load(open(r"C:\235235\rarus-ai\engine\i18n\messages.json", encoding="utf-8"))["ui"]
ru = {}
for name in sorted(os.listdir(os.path.join(SP, "ru"))):
    if name.startswith("ru_"):
        for line in open(os.path.join(SP, "ru", name), encoding="utf-8").read().splitlines():
            h, _, t = line.partition("|")
            ru[int(h.lstrip("@"))] = t
a, b = int(sys.argv[1]), int(sys.argv[2])
for n in range(a, b):
    print(n, "|", d[keys[n]]["en"][:60].replace("\n", " "), "=>", ru[n][:60])

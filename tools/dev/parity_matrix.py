"""One-off: turn the tables of the spec's appendix A into docs/compat/parity-matrix.csv (spec 11.3).

    python parity_matrix.py <TECH_SPEC.md> <out.csv>

The status column follows the appendix's own "Rarus v1" decision; the evidence column names only
what exists (the contract comparison and the engine test run), and every deleted, replaced or
postponed row is left for the owner to confirm.
"""

import csv
import re
import sys

EVIDENCE = (
    "РўРµСЃС‚С‹ Raven РІС‹РїРѕР»РЅСЏР»РёСЃСЊ РІ Raven Рё РІ Rarus, РїР°РґРµРЅРёСЏ СЃРІРµСЂРµРЅС‹ СЃ Р±Р°Р·РѕР№ (docs/reports/milestone-M7.md); "
    "RPC Р±РµР· РѕС‚Р»РёС‡РёР№ (docs/compat/2026-10-17-raven-parity.md); СЃС†РµРЅР°СЂРёР№ РёР· 12.6 РЅРµ РІС‹РїРѕР»РЅСЏР»СЃСЏ"
)


def status(decision: str, note: str) -> tuple[str, str]:
    low = decision.lower().replace("*", "").strip()
    if low.startswith("СѓРґР°Р»"):
        return (
            "РЈРґР°Р»РµРЅРѕ",
            "СЂРµС€РµРЅРёРµ D-17 Рё РўР— 11.2, РµСЃР»Рё РЅРµ СѓРєР°Р·Р°РЅРѕ РёРЅРѕРµ; РїРѕРґС‚РІРµСЂРґРёС‚СЊ",
        )
    if low.startswith("Р·Р°РјРµРЅ"):
        return "Р—Р°РјРµРЅРµРЅРѕ", "СЃРј. РїСЂРёРјРµС‡Р°РЅРёРµ"
    if low.startswith("РѕС‚Р»РѕР¶"):
        return (
            "РћС‚Р»РѕР¶РµРЅРѕ",
            "СЃРј. РїСЂРёРјРµС‡Р°РЅРёРµ; СЃСЂРѕРє РЅРµ РЅР°Р·РЅР°С‡РµРЅ",
        )
    if low.startswith("РѕРїС†Рё"):
        return (
            "РЎРѕС…СЂР°РЅРµРЅРѕ",
            "РѕРїС†РёСЏ: РІРєР»СЋС‡Р°РµС‚СЃСЏ РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј",
        )
    if low.startswith("РґР°"):
        return "РЎРѕС…СЂР°РЅРµРЅРѕ", ""
    return "РўСЂРµР±СѓРµС‚ СЂРµС€РµРЅРёСЏ", decision.strip()


def main(spec: str, out: str) -> None:
    with open(spec, encoding="utf-8") as fh:
        text = fh.read()
    appendix = text[
        text.index("## РџСЂРёР»РѕР¶РµРЅРёРµ A.") : text.index(
            "## РџСЂРёР»РѕР¶РµРЅРёРµ B."
        )
    ]
    rows = []
    for table in re.split(r"\n(?=### A\.)", appendix):
        lines = [ln for ln in table.splitlines() if ln.startswith("|")]
        if len(lines) < 3:
            continue
        header = [c.strip() for c in lines[0].strip("|").split("|")]
        if "Rarus v1" not in header:
            continue
        i_dec = header.index("Rarus v1")
        i_note = (
            header.index("РџСЂРёРјРµС‡Р°РЅРёРµ")
            if "РџСЂРёРјРµС‡Р°РЅРёРµ" in header
            else None
        )
        for line in lines[2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != len(header):
                continue
            decision = cells[i_dec]
            note = cells[i_note] if i_note is not None else ""
            st, extra = status(decision, note)
            where = "; ".join(
                c
                for h, c in zip(header, cells)
                if h
                in (
                    "РљРѕРґ РІ Raven",
                    "RPC / CLI",
                    "РРЅСЃС‚СЂСѓРјРµРЅС‚С‹ (РёРјРµРЅР°)",
                )
                and c
            )
            rows.append(
                [
                    cells[0],
                    cells[1],
                    where or "вЂ”",
                    st
                    + (
                        f" ({extra})"
                        if extra and st != "РЎРѕС…СЂР°РЅРµРЅРѕ"
                        else (f" ({extra})" if extra else "")
                    ),
                    note or "вЂ”",
                    EVIDENCE if st == "РЎРѕС…СЂР°РЅРµРЅРѕ" else "вЂ”",
                    "2026-10-17, Claude",
                    "РЅРµС‚ (РѕР±СЏР·Р°С‚РµР»СЊРЅРѕ)"
                    if st != "РЎРѕС…СЂР°РЅРµРЅРѕ"
                    else "РЅРµС‚",
                ]
            )
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "F-ID",
                "Р¤СѓРЅРєС†РёСЏ",
                "Р’ Raven",
                "РЎС‚Р°С‚СѓСЃ РІ Rarus",
                "РљСЂРёС‚РµСЂРёР№ РїСЂРёС‘РјРєРё",
                "Р”РѕРєР°Р·Р°С‚РµР»СЊСЃС‚РІРѕ",
                "Р”Р°С‚Р° Рё РёСЃРїРѕР»РЅРёС‚РµР»СЊ",
                "РџСЂРѕРІРµСЂРёР» Р·Р°РєР°Р·С‡РёРє",
            ]
        )
        w.writerows(rows)
    print(len(rows), "rows")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

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
    "Тесты Raven выполнялись в Raven и в Rarus, падения сверены с базой (docs/reports/milestone-M7.md); "
    "RPC без отличий (docs/compat/2026-10-18-raven-parity.md); сценарий из 12.6 не выполнялся"
)


def status(decision: str, note: str) -> tuple[str, str]:
    low = decision.lower().replace("*", "").strip()
    if low.startswith("удал"):
        return (
            "Удалено",
            "решение D-17 и ТЗ 11.2, если не указано иное; подтвердить",
        )
    if low.startswith("замен"):
        return "Заменено", "см. примечание"
    if low.startswith("отлож"):
        return (
            "Отложено",
            "см. примечание; срок не назначен",
        )
    if low.startswith("опци"):
        return (
            "Сохранено",
            "опция: включается пользователем",
        )
    if low.startswith("да"):
        return "Сохранено", ""
    return "Требует решения", decision.strip()


def main(spec: str, out: str) -> None:
    with open(spec, encoding="utf-8") as fh:
        text = fh.read()
    appendix = text[text.index("## Приложение A.") : text.index("## Приложение B.")]
    rows = []
    for table in re.split(r"\n(?=### A\.)", appendix):
        lines = [ln for ln in table.splitlines() if ln.startswith("|")]
        if len(lines) < 3:
            continue
        header = [c.strip() for c in lines[0].strip("|").split("|")]
        if "Rarus v1" not in header:
            continue
        i_dec = header.index("Rarus v1")
        i_note = header.index("Примечание") if "Примечание" in header else None
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
                    "Код в Raven",
                    "RPC / CLI",
                    "Инструменты (имена)",
                )
                and c
            )
            rows.append(
                [
                    cells[0],
                    cells[1],
                    where or "—",
                    st
                    + (
                        f" ({extra})"
                        if extra and st != "Сохранено"
                        else (f" ({extra})" if extra else "")
                    ),
                    note or "—",
                    EVIDENCE if st == "Сохранено" else "—",
                    "2026-10-18, Claude",
                    "нет (обязательно)" if st != "Сохранено" else "нет",
                ]
            )
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "F-ID",
                "Функция",
                "В Raven",
                "Статус в Rarus",
                "Критерий приёмки",
                "Доказательство",
                "Дата и исполнитель",
                "Проверил заказчик",
            ]
        )
        w.writerows(rows)
    print(len(rows), "rows")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

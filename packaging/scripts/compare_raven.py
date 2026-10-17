"""Compare Rarus with the Raven it was taken from (spec 11.4): write the parity report.

    python packaging/scripts/compare_raven.py \
        --raven <an unmodified copy of Raven> --raven-python <its python> \
        --rarus <this repository> --rarus-python <python that has the engine installed> \
        --out docs/compat/2026-10-18-raven-parity.md

Five of the nine steps of 11.4 are mechanical and are done here: the RPC contract, the page's
feature folders, the command line, the configuration keys and the message catalogue. The test run,
the 30 scenarios and the import are not (they take a person or an hour) and the report says so.

The Raven tree is only ever read. Command line and configuration are asked of the two engines
themselves, each by its own python, run from its own folder -- two trees under one interpreter would
compare the same package with itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

#: Printed by each engine's python: every command path with its option names.
CLI_PROBE = """
import json, typer
from raven.cli.commands import app
def walk(c, path):
    out = {" ".join(path): sorted(p.name for p in c.params)}
    for name, sub in getattr(c, "commands", {}).items():
        out.update(walk(sub, path + [name]))
    return out
print(json.dumps(walk(typer.main.get_command(app), ["cli"])))
"""

#: Printed by each engine's python: every configuration key, dotted, from the settings model.
CONFIG_PROBE = """
import json
from raven.config.schema import Config
def walk(schema, defs, prefix, seen):
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return []
        schema, seen = defs[name], seen | {name}
    for key in ("anyOf", "allOf", "oneOf"):
        for part in schema.get(key, []):
            yield from walk(part, defs, prefix, seen)
    for key, sub in schema.get("properties", {}).items():
        yield prefix + key
        yield from walk(sub, defs, prefix + key + ".", seen)
    if isinstance(schema.get("items"), dict):
        yield from walk(schema["items"], defs, prefix + "[].", seen)
js = Config.model_json_schema(by_alias=True)
print(json.dumps(sorted(set(walk(js, js.get("$defs", {}), "", frozenset())))))
"""


def rpc_methods(path: Path) -> dict[str, dict]:
    """Method name -> {params, result} as the contract states them."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {m["name"]: {"params": m.get("params", []), "result": m.get("result")} for m in doc["methods"]}


def compare_rpc(raven: dict[str, dict], rarus: dict[str, dict]) -> dict[str, list[str]]:
    """What Rarus lacks, what it adds, and which shared methods say something different."""
    changed = sorted(n for n in raven.keys() & rarus.keys() if raven[n] != rarus[n])
    return {
        "missing": sorted(raven.keys() - rarus.keys()),
        "added": sorted(rarus.keys() - raven.keys()),
        "changed": changed,
    }


def feature_dirs(root: Path) -> list[str]:
    base = root / "ui-web" / "src" / "features"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def compare_sets(raven: list[str], rarus: list[str]) -> dict[str, list[str]]:
    return {
        "missing": sorted(set(raven) - set(rarus)),
        "added": sorted(set(rarus) - set(raven)),
        "common": sorted(set(raven) & set(rarus)),
    }


def compare_cli(raven: dict[str, list[str]], rarus: dict[str, list[str]]) -> dict[str, list[str]]:
    """Commands and options, with the program's own name stripped from the front of each path."""

    def strip(table: dict[str, list[str]]) -> dict[str, list[str]]:
        return {path.split(" ", 1)[1] if " " in path else "": opts for path, opts in table.items()}

    a, b = strip(raven), strip(rarus)
    options = sorted(
        f"{cmd}: {sorted(set(a[cmd]) ^ set(b[cmd]))}" for cmd in a.keys() & b.keys() if set(a[cmd]) != set(b[cmd])
    )
    return {"missing": sorted(a.keys() - b.keys()), "added": sorted(b.keys() - a.keys()), "options": options}


def catalogue(root: Path) -> dict[str, dict[str, str]]:
    path = root / "i18n" / "messages.json"
    return json.loads(path.read_text(encoding="utf-8"))["ui"]


def _brand(text: str | None) -> str | None:
    """Raven's wording with the product name it carries replaced by ours (the one allowed difference)."""
    return text.replace("Raven", "Rarus") if text else text


def compare_catalogues(raven: dict[str, dict[str, str]], rarus: dict[str, dict[str, str]]) -> dict[str, list[str]]:
    """Every Raven key in Rarus for en and zh with the same words but the product name."""
    missing = sorted(k for k in raven if k not in rarus)
    both = [k for k in raven if k in rarus]
    reworded = sorted(k for k in both if any(rarus[k].get(c) != _brand(raven[k].get(c)) for c in ("en", "zh")))
    renamed = sorted(k for k in both if any(rarus[k].get(c) != raven[k].get(c) for c in ("en", "zh")))
    no_russian = sorted(k for k, cols in rarus.items() if not (cols.get("ru") or "").strip())
    return {
        "missing": missing,
        "reworded": reworded,
        "renamed": sorted(set(renamed) - set(reworded)),
        "no_russian": no_russian,
        "added": sorted(k for k in rarus if k not in raven),
    }


def ask_engine(python: str, cwd: Path, code: str) -> object:
    """Run a probe in an engine's own interpreter and read the JSON it prints."""
    done = subprocess.run(
        [python, "-c", code],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if done.returncode != 0:
        raise SystemExit(f"probe failed in {cwd}:\n{done.stderr[-1500:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def listing(title: str, items: list[str], limit: int = 60) -> list[str]:
    if not items:
        return [f"{title}: нет."]
    shown = items[:limit]
    more = f" … и ещё {len(items) - limit}" if len(items) > limit else ""
    return [f"{title} ({len(items)}):", *[f"- `{i}`" for i in shown], *([more] if more else [])]


def render(date: str, sections: dict[str, dict]) -> str:
    rpc, feats, cli, cfg, i18n = (sections[k] for k in ("rpc", "features", "cli", "config", "i18n"))
    out = [
        f"# Сверка с Raven — {date}",
        "",
        "Raven: коммит `5a675b61` (неизменённая копия). Отчёт строится скриптом `packaging/scripts/compare_raven.py`; "
        "ТЗ 11.4, шаги 1–5. Шаги 6–8 (тесты, 30 сценариев, импорт данных) — вручную, см. в конце.",
        "",
        "## 1. RPC",
        "",
        *listing("В Raven есть, в Rarus нет", rpc["missing"]),
        "",
        *listing("Добавлено в Rarus", rpc["added"]),
        "",
        *listing("Общие методы с другим описанием", rpc["changed"]),
        "",
        "## 2. Интерфейс (`ui-web/src/features/`)",
        "",
        *listing("Папки Raven без соответствия в Rarus", feats["missing"]),
        "",
        *listing("Новые папки Rarus", feats["added"]),
        "",
        "## 3. Командная строка",
        "",
        *listing("Команды Raven, которых нет в Rarus", cli["missing"]),
        "",
        *listing("Команды, добавленные в Rarus", cli["added"]),
        "",
        *listing("Команды с другим набором параметров", cli["options"]),
        "",
        "## 4. Конфигурация",
        "",
        *listing("Ключи Raven, которых нет в Rarus", cfg["missing"]),
        "",
        *listing("Ключи, добавленные в Rarus", cfg["added"]),
        "",
        "## 5. Каталог сообщений",
        "",
        *listing("Ключи Raven, которых нет в Rarus", i18n["missing"]),
        "",
        f"Ключей, где в en/zh только «Raven» заменено на «Rarus»: {len(i18n['renamed'])}.",
        "",
        *listing("Ключи с другим текстом en/zh (кроме названия)", i18n["reworded"]),
        "",
        *listing("Ключи Rarus без русского текста", i18n["no_russian"]),
        "",
        f"Добавлено ключей в Rarus: {len(i18n['added'])}.",
        "",
        "## 6–8. Не выполнялось этим скриптом",
        "",
        "- Тесты: полный прогон Rarus сравнивается с базой падений Raven на Windows (`tools/dev/baselines`); итог — в `docs/reports/milestone-M7.md`.",
        "- 30 сценариев (ТЗ 12.6): требуют живой модели и человека; результаты — в `docs/compat/scenarios.md`.",
        "- Импорт данных: `rarus migrate-raven` проверен тестами на тестовом `RAVEN_HOME`; на чужих данных не запускался.",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raven", type=Path, required=True)
    ap.add_argument("--raven-python", required=True)
    ap.add_argument("--rarus", type=Path, required=True)
    ap.add_argument("--rarus-python", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--date", default="")
    args = ap.parse_args(argv)

    raven_engine, rarus_engine = args.raven, args.rarus / "engine"
    sections = {
        "rpc": compare_rpc(
            rpc_methods(raven_engine / "rpc-schema" / "openrpc.json"),
            rpc_methods(rarus_engine / "rpc-schema" / "openrpc.json"),
        ),
        "features": compare_sets(feature_dirs(raven_engine), feature_dirs(rarus_engine)),
        "cli": compare_cli(
            ask_engine(args.raven_python, raven_engine, CLI_PROBE),  # type: ignore[arg-type]
            ask_engine(args.rarus_python, rarus_engine, CLI_PROBE),  # type: ignore[arg-type]
        ),
        "config": compare_sets(
            ask_engine(args.raven_python, raven_engine, CONFIG_PROBE),  # type: ignore[arg-type]
            ask_engine(args.rarus_python, rarus_engine, CONFIG_PROBE),  # type: ignore[arg-type]
        ),
        "i18n": compare_catalogues(catalogue(raven_engine), catalogue(rarus_engine)),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(args.date or "сегодня", sections), encoding="utf-8")
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

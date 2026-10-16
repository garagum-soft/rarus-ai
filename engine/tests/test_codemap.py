"""The code map: an incremental index of what a project defines and imports, ranked for a question."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.codemap.index import CodeIndex, list_files
from raven.codemap.languages import extract
from raven.codemap.query import rank, render_find, render_map


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args], capture_output=True, check=True
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


SHOP = {
    ".gitignore": "generated/\n",
    "shop/__init__.py": "",
    "shop/billing.py": (
        "from shop.money import Money\n\n"
        "TAX_RATE = 0.2\n\n"
        "class Invoice:\n"
        "    def total(self, lines: list[Money]) -> Money:\n"
        "        return sum(lines)\n\n"
        "    class Line:\n"
        "        def amount(self): ...\n\n"
        "async def send_invoice(invoice: Invoice, to: str) -> None: ...\n"
    ),
    "shop/money.py": "class Money(int):\n    def cents(self) -> int:\n        return self * 100\n",
    "shop/cart.py": "from . import money\nfrom .billing import Invoice\n\ndef checkout(cart): return Invoice()\n",
    "tests/test_billing.py": "from shop.billing import Invoice\n\ndef test_invoice_total(): ...\ndef test_invoice_line(): ...\n",
    "web/api.ts": (
        "import { formatMoney } from './format'\n"
        "import React from 'react'\n\n"
        "export interface InvoiceView { id: string }\n"
        "export class InvoiceClient {\n"
        "  constructor(private base: string) {\n"
        "    if (base) {\n"
        "      doSomething(base)\n"
        "    }\n"
        "  }\n"
        "  async fetchInvoice(id: string): Promise<InvoiceView> {\n"
        "    return fetch(id)\n"
        "  }\n"
        "}\n"
        "export const toLabel = (v: InvoiceView): string => formatMoney(v.id)\n"
    ),
    "web/format.ts": "export function formatMoney(x: string): string { return x }\n",
    "docs/guide.md": "# Billing guide\n\n```\n# not a heading\n```\n\n## Sending invoices\n",
    "generated/huge.py": "def ignored(): ...\n",
}


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, SHOP)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


# -- reading one file ------------------------------------------------------------------------


def test_python_is_read_exactly_with_members_constants_and_imports() -> None:
    symbols, imports = extract(SHOP["shop/billing.py"], "python")

    got = [(s.kind, s.name, s.parent, s.line) for s in symbols]
    assert got == [
        ("constant", "TAX_RATE", "", 3),
        ("class", "Invoice", "", 5),
        ("method", "total", "Invoice", 6),
        ("class", "Line", "Invoice", 9),
        ("method", "amount", "Invoice.Line", 10),
        ("function", "send_invoice", "", 12),
    ]
    assert symbols[2].signature == "def total(self, lines: list[Money]) -> Money"
    assert imports == ["shop.money|Money"]


def test_python_that_does_not_parse_is_still_outlined() -> None:
    symbols, imports = extract("import os\nclass A:\n    def b(self):\n        if (\n\ndef c(): pass\n", "python")

    assert [(s.kind, s.name, s.parent) for s in symbols] == [
        ("class", "A", ""),
        ("method", "b", "A"),
        ("function", "c", ""),
    ]
    assert imports == ["os"]


def test_typescript_reads_declarations_not_the_calls_inside_bodies() -> None:
    symbols, imports = extract(SHOP["web/api.ts"], "typescript")

    got = [(s.kind, s.name, s.parent) for s in symbols]
    assert got == [
        ("type", "InvoiceView", ""),
        ("class", "InvoiceClient", ""),
        ("method", "constructor", "InvoiceClient"),
        ("method", "fetchInvoice", "InvoiceClient"),
        ("function", "toLabel", ""),
    ]
    assert imports == ["./format", "react"]


@pytest.mark.parametrize(
    ("lang", "text", "expected", "imports"),
    [
        (
            "go",
            'package x\n\nimport (\n\t"fmt"\n\tm "example.com/shop/money"\n)\n\ntype Cart struct{}\n\n'
            "func (c *Cart) Total() int { return 0 }\nfunc New() *Cart { return nil }\n",
            [("type", "Cart", ""), ("method", "Total", "Cart"), ("function", "New", "")],
            ["fmt", "example.com/shop/money"],
        ),
        (
            "rust",
            "use crate::money::Money;\nmod tax;\n\npub struct Cart;\n\nimpl Cart {\n    pub fn total(&self) -> u32 {\n"
            "        helper();\n        0\n    }\n}\n\nfn helper() {}\n",
            [("type", "Cart", ""), ("type", "Cart", ""), ("method", "total", "Cart"), ("function", "helper", "")],
            ["crate::money::Money", "tax"],
        ),
        (
            "java",
            "import shop.money.Money;\n\npublic class Cart {\n    @Override\n    public int total(int x) {\n"
            "        compute(x,\n            1);\n        return 0;\n    }\n    private static class Line {}\n}\n",
            [("class", "Cart", ""), ("method", "total", "Cart"), ("class", "Line", "Cart")],
            ["shop.money.Money"],
        ),
        (
            "c",
            '#include "money.h"\n#include <stdio.h>\n#define MAX_ITEMS 10\n\nstruct cart {\n  int n;\n};\n\n'
            "static int total(struct cart *c)\n{\n  return helper(c->n);\n}\n",
            [("constant", "MAX_ITEMS", ""), ("type", "cart", ""), ("function", "total", "")],
            ["money.h"],
        ),
        (
            "markdown",
            SHOP["docs/guide.md"],
            [("section", "Billing guide", ""), ("section", "Sending invoices", "")],
            [],
        ),
        (
            "sql",
            "-- schema\nCREATE TABLE IF NOT EXISTS invoices (id int);\ncreate or replace view open_invoices as select 1;\n",
            [("type", "invoices", ""), ("type", "open_invoices", "")],
            [],
        ),
    ],
)
def test_other_languages_are_outlined_line_by_line(lang, text, expected, imports) -> None:
    symbols, found = extract(text, lang)

    assert [(s.kind, s.name, s.parent) for s in symbols] == expected
    assert found == imports


# -- the index -------------------------------------------------------------------------------


def test_the_index_follows_git_and_lives_outside_the_project(shop: Path) -> None:
    (shop / "shop" / "draft.py").write_text("def unfinished(): ...\n", encoding="utf-8")  # untracked, not ignored
    index = CodeIndex(shop)

    stats = index.refresh()

    paths = {row.path for row in index.files()}
    assert "shop/draft.py" in paths, "untracked files git does not ignore are part of the project"
    assert "generated/huge.py" not in paths, ".gitignore decides what is noise"
    assert stats.source == "git" and stats.parsed == len(paths) and not stats.partial
    assert index.db.parent.name == "codemap" and shop not in index.db.parents
    status = subprocess.run(["git", "-C", str(shop), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert status.strip() == "?? shop/draft.py", "nothing is written into the project"


def test_a_refresh_reads_only_what_changed(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()

    again = index.refresh()
    (shop / "shop" / "money.py").write_text("class Money(int):\n    def euros(self): ...\n", encoding="utf-8")
    (shop / "web" / "format.ts").unlink()
    changed = index.refresh()

    assert again.parsed == 0 and again.unchanged == again.listed
    assert changed.parsed == 1 and changed.removed == 1
    assert [f.symbol.name for f in index.find("euros")[0]] == ["euros"]
    assert index.find("formatMoney")[0] == []


def test_large_binary_and_minified_files_are_listed_but_not_read(shop: Path, monkeypatch) -> None:
    import raven.codemap.index as idx

    monkeypatch.setattr(idx, "MAX_FILE_BYTES", 60_000)
    _write(shop, {"big.py": "x = 1\n" * 12_000, "bundle.js": "var a=1;" * 7_000})
    (shop / "blob.c").write_bytes(b"int main(){}\0\0\0")
    index = CodeIndex(shop)

    stats = index.refresh()

    skipped = {row.path: row.skipped for row in index.files() if row.skipped}
    assert skipped == {"big.py": "too large", "bundle.js": "minified", "blob.c": "binary"}
    assert stats.skipped == 3


def test_a_deadline_leaves_a_partial_index_the_next_refresh_completes(shop: Path) -> None:
    index = CodeIndex(shop)

    first = index.refresh(deadline_s=1e-9)
    rest = index.refresh(deadline_s=None)

    assert first.partial and first.parsed < first.listed
    assert not rest.partial and first.parsed + rest.parsed == rest.listed


def test_a_file_git_lists_but_the_checkout_lacks_is_left_out(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()
    # What a sparse checkout looks like to the index: tracked, not on disk.
    _git(shop, "update-index", "--skip-worktree", "shop/cart.py")
    (shop / "shop" / "cart.py").unlink()

    stats = index.refresh()

    assert stats.removed == 1 and "shop/cart.py" not in {row.path for row in index.files()}


def test_a_workspace_of_several_repositories_lists_each_by_its_own_git(tmp_path: Path) -> None:
    for name in ("api", "web"):
        repo = tmp_path / "ws" / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        _write(repo, {".gitignore": "out/\n", "src/main.py": "def main(): ...\n", "out/gen.py": "def gen(): ...\n"})
    _write(tmp_path / "ws", {"notes.md": "# Notes\n", "node_modules/x/index.js": "function x() {}\n"})

    files, source, cut = list_files(tmp_path / "ws")

    assert sorted(files) == ["api/src/main.py", "notes.md", "web/src/main.py"]
    assert source == "git+walk" and not cut


def test_imports_become_a_graph_of_who_depends_on_whom(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()

    rows = {row.path: row for row in index.files()}

    assert rows["shop/money.py"].indeg == 2  # billing (absolute) and cart (relative)
    assert rows["shop/billing.py"].indeg == 2  # cart and the test
    assert rows["web/format.ts"].indeg == 1
    assert rows["shop/cart.py"].deps == ["shop/billing.py", "shop/money.py"]


# -- reading the index -----------------------------------------------------------------------


def test_the_map_ranks_the_implementation_above_its_tests_and_fits_the_budget(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()

    ranked = [row.path for _, row in rank(index.files(), query="invoice total")]
    text = render_map(index, query="invoice total", budget=600)

    assert ranked[0] == "shop/billing.py" and ranked.index("tests/test_billing.py") > ranked.index("shop/billing.py")
    assert "shop/billing.py  [python, 12 lines, used by 2]" in text
    assert "    def total(self, lines: list[Money]) -> Money  :6" in text
    assert len(text) <= 600 + 200, "the budget holds, give or take the header"
    assert "more file(s) ranked lower" in text


def test_with_no_query_the_most_depended_on_files_come_first(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()

    ranked = [row.path for _, row in rank(index.files())][:2]

    assert sorted(ranked) == ["shop/billing.py", "shop/money.py"]


def test_focus_brings_a_files_neighbours_forward(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()

    ranked = [row.path for _, row in rank(index.files(), focus=["shop/cart.py"])]

    assert set(ranked) == {"shop/billing.py", "shop/money.py"}


def test_a_name_is_found_exactly_then_loosely_and_by_owner(shop: Path) -> None:
    index = CodeIndex(shop)
    index.refresh()

    exact, how = index.find("Invoice")
    loose, how2 = index.find("invoiceclient")
    partial, how3 = index.find("Invoic")
    member, _ = index.find("Invoice.Line.amount")
    under, _ = index.find("main", under="web")

    assert how == "exact" and [(f.path, f.symbol.line) for f in exact] == [("shop/billing.py", 5)]
    assert how2 == "case-insensitive" and loose[0].symbol.name == "InvoiceClient"
    assert how3 == "partial" and {f.symbol.name for f in partial} >= {
        "Invoice",
        "InvoiceClient",
        "InvoiceView",
        "send_invoice",
    }
    assert [(f.symbol.name, f.symbol.parent) for f in member] == [("amount", "Invoice.Line")]
    assert under == []
    assert "shop/billing.py:12  function send_invoice" in render_find(index, "send_invoice")


# -- the agent tool --------------------------------------------------------------------------


async def test_the_tool_maps_and_finds_inside_the_fence(shop: Path, tmp_path: Path) -> None:
    from raven.agent.tools.code_map import CodeMapTool

    tool = CodeMapTool(workspace=shop, allowed_dirs=(shop,))

    mapped = await tool.execute(query="money cents", path="shop")
    found = await tool.execute(symbol="Invoice.total")
    focused = await tool.execute(path="shop/cart.py")
    outside = await tool.execute(path=str(tmp_path))

    assert "Code map of shop/ in" in mapped and "shop/money.py" in mapped and "web/api.ts" not in mapped
    assert "shop/billing.py:6  method Invoice.total" in found
    assert "near shop/cart.py" in focused
    assert outside.startswith("Error")


async def test_the_tool_refuses_the_home_directory_and_says_when_the_index_is_partial(shop: Path, monkeypatch) -> None:
    import raven.codemap.index as idx
    from raven.agent.tools.code_map import CodeMapTool

    monkeypatch.setattr(idx, "REFRESH_DEADLINE_S", 1e-9)
    tool = CodeMapTool(workspace=shop)

    home = await tool.execute(path=str(Path.home()))
    partial = await tool.execute(query="invoice")

    assert home.startswith("Error: refusing to map")
    assert partial.startswith("(index partial:")


def test_the_tool_is_served_only_on_the_flag_and_reads_without_asking(tmp_path: Path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
    from raven.config.schema import Config, ToolsConfig
    from raven.permissions.rules import DEFAULT_ALLOW_TOOLS
    from tests.test_settings_tool_groups import _StubProvider

    def loop(**wiring) -> AgentLoop:
        return AgentLoop(
            provider=_StubProvider(),
            workspace=tmp_path,
            model="stub",
            policy=TurnPolicy(max_iterations=2),
            tools=ToolWiring(**wiring),
            engine=EngineWiring(),
        )

    assert "code_map" not in loop().tools.names()
    assert "code_map" in loop(code_map=True).tools.names()
    assert "code_map" in DEFAULT_ALLOW_TOOLS
    assert ToolsConfig().code_map is False
    assert Config.model_validate({"tools": {"codeMap": True}}).tools.code_map is True


# -- the command line ------------------------------------------------------------------------


def test_the_command_line_indexes_shows_finds_and_drops(shop: Path) -> None:
    from raven.cli.commands import app

    runner = CliRunner()
    cwd = os.getcwd()
    os.chdir(shop / "web")
    try:
        indexed = runner.invoke(app, ["codemap", "index"])
        shown = runner.invoke(app, ["codemap", "show", str(shop), "-q", "invoice client", "--budget", "800"])
        found = runner.invoke(app, ["codemap", "find", "formatMoney", str(shop)])
        sub = runner.invoke(app, ["codemap", "show"])
        dropped = runner.invoke(app, ["codemap", "drop", str(shop)])
    finally:
        os.chdir(cwd)

    assert indexed.exit_code == 0 and f"{shop}: " in indexed.output and "(git)" in indexed.output
    assert shown.exit_code == 0 and shown.output.index("web/api.ts") < shown.output.index("shop/billing.py")
    assert "web/format.ts:1  function formatMoney" in found.output
    assert "Code map of web/ in" in sub.output and "shop/billing.py" not in sub.output
    assert dropped.exit_code == 0 and "dropped" in dropped.output
    assert not CodeIndex(shop).exists

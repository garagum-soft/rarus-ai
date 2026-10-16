"""Reading a code index: a ranked map of the files that matter, and where a name is defined.

The map answers "where should I look" in a tree too large to read: files are
ranked by how well their path and the names they define match the words asked
about, then by how many other files import them -- the files everything else
depends on are the ones worth knowing first. Each file is shown with the
outline of what it defines (classes, functions, members, with signatures and
line numbers), cut to a character budget so the answer fits a model's turn.
With no words to rank by, the map is the tree's most depended-on files.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from raven.codemap.index import CodeIndex, FileRow, _words, stem
from raven.codemap.languages import Symbol

DEFAULT_BUDGET = 6000
MAX_BUDGET = 40_000
SYMBOLS_PER_FILE = 25


_STOPWORDS = frozenset(
    "a an the of for to in on at by with from and or not is are be it its this that these those how what where which "
    "who why when do does did all any some into about over under via per as than then there here my our your".split()
)
_TEST_WORDS = frozenset({"test", "tests", "spec", "specs", "fixture", "fixtures", "mock", "mocks"})


def terms_of(query: str) -> list[str]:
    seen: dict[str, None] = {}
    for word in _words(query):
        if word not in _STOPWORDS:
            seen.setdefault(stem(word), None)
    return list(seen)


class _Scorer:
    """BM25-shaped: a rare word counts for more than a common one, and a file does not win on size.

    A word in the file's path is worth more than the same word among its
    definitions; a file matching more of the words beats one matching one word
    many times. Test files rank lower unless the words ask for tests -- the
    question is usually where something is done, and a test names it too.
    """

    _K1 = 1.2
    _B = 0.75

    def __init__(self, files: list[FileRow], terms: list[str]) -> None:
        self.terms = terms
        live = [f for f in files if not f.skipped]
        self.n = max(len(live), 1)
        self.avg = max(sum(sum(f.stokens.values()) for f in live) / self.n, 1.0)
        df: dict[str, int] = defaultdict(int)
        for row in live:
            for term in terms:
                if term in row.ptokens or term in row.stokens:
                    df[term] += 1
        self.idf = {t: math.log(1 + (self.n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms}
        self.wants_tests = bool(_TEST_WORDS & set(terms))

    def __call__(self, row: FileRow) -> float:
        size = sum(row.stokens.values())
        norm = self._K1 * (1 - self._B + self._B * size / self.avg)
        total = 0.0
        matched = 0
        for term in self.terms:
            count = row.stokens.get(term, 0)
            in_path = term in row.ptokens
            if not (count or in_path):
                continue
            matched += 1
            part = count * (self._K1 + 1) / (count + norm) if count else 0.0
            total += self.idf[term] * (part + (1.5 if in_path else 0.0))
        if not matched:
            return 0.0
        total *= 0.5 + 0.5 * matched / len(self.terms)
        if not self.wants_tests and _TEST_WORDS & row.ptokens:
            total *= 0.5
        return total


def rank(files: list[FileRow], *, query: str = "", focus: Iterable[str] = ()) -> list[tuple[float, FileRow]]:
    """Files worth showing, best first; with no query and no focus, every file by how depended-on it is."""
    terms = terms_of(query)
    score = _Scorer(files, terms) if terms else None
    focus = [f for f in focus if f]
    near: set[str] = set()
    if focus:
        wanted = set(focus)
        for row in files:
            if row.path in wanted:
                near.update(row.deps)
            elif wanted & set(row.deps):
                near.add(row.path)
        near -= wanted
    ranked: list[tuple[float, FileRow]] = []
    for row in files:
        if row.skipped:
            continue
        base = score(row) if score is not None else 0.0
        if row.path in near:
            base += 3.0
        if (terms or focus) and base <= 0:
            continue
        ranked.append((base + 0.3 * math.log1p(row.indeg), row))
    ranked.sort(key=lambda item: (-item[0], -item[1].indeg, item[1].path))
    return ranked


def _order_symbols(symbols: list[Symbol], terms: list[str]) -> list[Symbol]:
    """The symbols to show for one file: those matching the query first, then the outline in file order."""
    if not terms:
        return symbols
    wanted = set(terms)

    def hit(s: Symbol) -> bool:
        return bool(wanted & {stem(w) for w in _words(s.name)})

    hits = {id(s) for s in symbols if hit(s)}
    keep = [s for s in symbols if id(s) in hits or s.kind in ("class", "type")]
    rest = [s for s in symbols if id(s) not in hits and s.kind not in ("class", "type")]
    return keep + rest


def _outline(symbols: list[Symbol], terms: list[str], limit: int) -> list[str]:
    chosen = _order_symbols(symbols, terms)[:limit]
    chosen.sort(key=lambda s: s.line)
    shown_parents = {s.name for s in chosen if s.kind in ("class", "type")}
    lines: list[str] = []
    for s in chosen:
        text = s.signature or f"{s.kind} {s.name}"
        if s.kind == "section":
            text = f"# {s.name}"
        if s.parent and s.parent.split(".")[-1] in shown_parents:
            lines.append(f"    {text}  :{s.line}")
        elif s.parent:
            lines.append(f"  {s.parent}.{s.name}: {text}  :{s.line}")
        else:
            lines.append(f"  {text}  :{s.line}")
    if len(symbols) > limit:
        lines.append(f"  … {len(symbols) - limit} more definitions")
    return lines


def _header(row: FileRow) -> str:
    used = f", used by {row.indeg}" if row.indeg else ""
    return f"{row.path}  [{row.lang}, {row.lines} lines{used}]"


def summary_line(index: CodeIndex, files: list[FileRow]) -> str:
    langs: dict[str, int] = defaultdict(int)
    for row in files:
        langs[row.lang] += 1
    top = ", ".join(f"{lang} {n}" for lang, n in sorted(langs.items(), key=lambda kv: -kv[1])[:6])
    return f"{len(files)} source files ({top})" if files else "no source files"


def render_map(
    index: CodeIndex,
    *,
    query: str = "",
    focus: Iterable[str] = (),
    under: str = "",
    budget: int = DEFAULT_BUDGET,
) -> str:
    """The map as text, at most ``budget`` characters (one file is always shown, cut to fit)."""
    budget = max(500, min(budget, MAX_BUDGET))
    files = index.files(under)
    ranked = rank(files, query=query, focus=focus)
    terms = terms_of(query)
    scope = f"{under}/ in " if under else ""
    head = [f"Code map of {scope}{index.root} -- {summary_line(index, files)}"]
    if terms or focus:
        head.append(f"ranked by: {' '.join(terms) or '-'}" + (f"; near {', '.join(focus)}" if focus else ""))
    else:
        head.append("ranked by: how many files import each one (no query given)")
    if not ranked:
        head.append("nothing matches; try other words, a wider path, or grep for the text itself")
        return "\n".join(head)
    out = head[:]
    used = sum(len(line) + 1 for line in out)
    shown = 0
    top = [row.path for _, row in ranked[:200]]
    outlines = index.symbols_of(top)
    for _, row in ranked[:200]:
        block = ["", _header(row), *_outline(outlines.get(row.path, []), terms, SYMBOLS_PER_FILE)]
        size = sum(len(line) + 1 for line in block)
        if used + size > budget:
            if shown:
                break
            # The first file is always shown, its outline cut to what fits.
            while len(block) > 2 and used + sum(len(line) + 1 for line in block) > budget:
                block.pop()
        out.extend(block)
        used += sum(len(line) + 1 for line in block)
        shown += 1
    left = len(ranked) - shown
    if left > 0:
        out.append("")
        out.append(f"… {left} more file(s) ranked lower; narrow with path= or more specific words")
    return "\n".join(out)


def render_find(index: CodeIndex, name: str, *, under: str = "", limit: int = 20) -> str:
    found, how = index.find(name, under=under, limit=limit)
    if not found:
        return f"no definition of {name!r} in the index of {index.root}; grep for it if it is defined dynamically"
    lines = [f"{len(found)} definition(s) of {name!r} ({how} match):"]
    for item in found:
        s = item.symbol
        owner = f"{s.parent}." if s.parent else ""
        used = f"  [file used by {item.indeg}]" if item.indeg else ""
        lines.append(f"{item.path}:{s.line}  {s.kind} {owner}{s.name}{used}")
        if s.signature:
            lines.append(f"    {s.signature}")
    if len(found) >= limit:
        lines.append(f"(first {limit}; narrow with path= or Parent.name)")
    return "\n".join(lines)


__all__ = ["DEFAULT_BUDGET", "MAX_BUDGET", "rank", "render_find", "render_map", "summary_line", "terms_of"]

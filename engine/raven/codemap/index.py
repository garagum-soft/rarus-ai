"""The code index of one project: its source files, what each defines and imports.

Kept in SQLite under ``<RARUS_HOME>/codemap/``, never inside the project, and
brought up to date incrementally: a file whose size and modification time are
unchanged is not read again, so after the first pass a refresh of a large tree
costs one ``stat`` per file. The file list is git's where there is a repository
(tracked plus untracked-but-not-ignored, so ``.gitignore`` decides what is
noise), and a pruned walk elsewhere; a directory inside the walk that holds its
own repository is listed by its own git, which is what a multi-repository
workspace looks like.

A refresh runs under a deadline. What it parsed before the deadline is kept, it
says it is partial, and the next refresh carries on from there -- the first
look at a very large tree is spread over a few calls instead of blocking one.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sqlite3
import stat
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from raven.codemap.languages import EXTRACTOR_VERSION, Symbol, extract, language_of

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 200_000
REFRESH_DEADLINE_S = 20.0
_BATCH = 400
_GIT_TIMEOUT_S = 60
_NOISE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".gradle",
        ".idea",
        ".vs",
        "htmlcov",
        "vendor",
        "bower_components",
    }
)


def default_dir() -> Path:
    from raven.home import raven_home

    return raven_home() / "codemap"


def index_path(root: Path) -> Path:
    """Where ``root``'s index lives: a readable slug plus a hash, since slugs of two paths can collide."""
    from raven.utils.paths import project_slug

    key = os.path.normcase(str(root))
    return (
        default_dir() / f"{project_slug(root)[-80:].lstrip('-')}-{hashlib.sha1(key.encode()).hexdigest()[:10]}.sqlite"
    )


@dataclass
class RefreshStats:
    listed: int = 0
    parsed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    """Files listed but not read: too large, binary or minified."""
    partial: bool = False
    source: str = ""
    """``git``, ``walk`` or ``git+walk`` -- where the file list came from."""
    seconds: float = 0.0

    def line(self) -> str:
        done = (
            f"{self.listed} source files ({self.source}): {self.parsed} read, {self.unchanged} unchanged, "
            f"{self.removed} gone, {self.skipped} skipped, {self.seconds:.1f}s"
        )
        return done + ("; partial -- the next refresh continues" if self.partial else "")


@dataclass
class FileRow:
    path: str
    lang: str
    lines: int
    indeg: int
    """How many indexed files import this one."""
    deps: list[str]
    ptokens: frozenset[str]
    stokens: dict[str, int]
    skipped: str


@dataclass(frozen=True)
class Found:
    path: str
    symbol: Symbol
    indeg: int


# -- listing ---------------------------------------------------------------------------------


def _git_files(directory: Path) -> list[str] | None:
    """``directory``'s files as git sees them, relative to it; ``None`` where git has no repository."""
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607 -- git location varies; PATH lookup intended
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [p for p in done.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _has_repo(directory: Path) -> bool:
    return (directory / ".git").exists()


def project_root(path: Path) -> Path:
    """The repository ``path`` sits in, or ``path`` itself (its directory, for a file) outside one."""
    base = path if path.is_dir() else path.parent
    try:
        done = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],  # noqa: S607 -- PATH lookup intended
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return base.resolve()
    top = done.stdout.strip()
    return Path(top).resolve() if done.returncode == 0 and top else base.resolve()


def list_files(root: Path, *, deadline: float | None = None) -> tuple[list[str], str, bool]:
    """Source files under ``root`` (relative, ``/``-separated), where the list came from, and whether it is cut short."""
    out: list[str] = []
    sources: set[str] = set()
    cut = False

    def add_git(directory: Path, prefix: str, listed: list[str], depth: int) -> None:
        nonlocal cut
        sources.add("git")
        for rel in listed:
            full = directory / rel
            if language_of(rel):
                out.append(prefix + rel)
            elif (
                depth < 3
                and not rel.endswith("/")
                and "." not in posixpath.basename(rel)
                and full.is_dir()
                and _has_repo(full)
            ):
                # A submodule is one gitlink entry; its files are its own repository's.
                inner = _git_files(full)
                if inner is not None:
                    add_git(full, f"{prefix}{rel}/", inner, depth + 1)
            if len(out) >= MAX_FILES:
                cut = True
                return

    listed = _git_files(root)
    if listed is not None:
        add_git(root, "", listed, 0)
        return out, "+".join(sorted(sources)), cut

    sources.add("walk")
    pending = [root]
    while pending and not cut:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if deadline is not None and time.monotonic() > deadline:
                cut = True
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            rel = Path(entry.path).relative_to(root).as_posix()
            if is_dir:
                if entry.name in _NOISE_DIRS or entry.name.startswith("."):
                    continue
                if _has_repo(Path(entry.path)) and (inner := _git_files(Path(entry.path))) is not None:
                    add_git(Path(entry.path), f"{rel}/", inner, 1)
                    continue
                pending.append(Path(entry.path))
            elif language_of(entry.name):
                out.append(rel)
            if len(out) >= MAX_FILES:
                cut = True
                break
    return out, "+".join(sorted(sources, key=lambda s: s != "git")), cut


# -- resolving imports to files --------------------------------------------------------------

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")


class _Resolver:
    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = set(paths)
        self.by_suffix: dict[str, list[str]] = defaultdict(list)
        go_dirs: dict[str, list[str]] = defaultdict(list)
        for path in self.paths:
            parts = path.split("/")
            for k in range(1, min(len(parts), 8) + 1):
                self.by_suffix["/".join(parts[-k:])].append(path)
            if path.endswith(".go") and not path.endswith("_test.go"):
                go_dirs[posixpath.dirname(path)].append(path)
        # A Go import path ends with the package directory; keyed by every tail of it.
        self.go_by_suffix: dict[str, list[str]] = defaultdict(list)
        for directory, files in go_dirs.items():
            parts = directory.split("/") if directory else []
            for k in range(1, len(parts) + 1):
                self.go_by_suffix["/".join(parts[-k:])].extend(files)

    def _suffix(self, cand: str, *, max_hits: int = 3) -> list[str]:
        hits = self.by_suffix.get(cand, [])
        return hits if len(hits) <= max_hits else []

    def _exact(self, cand: str) -> list[str]:
        cand = posixpath.normpath(cand)
        return [cand] if cand in self.paths else []

    def resolve(self, importer: str, lang: str, raw: str) -> list[str]:
        here = posixpath.dirname(importer)
        if lang == "python":
            return self._python(here, raw)
        if lang in ("javascript", "typescript"):
            if raw.startswith("."):
                return self._js(posixpath.join(here, raw))
            if raw.startswith(("@/", "~/")):
                return self._js(f"src/{raw[2:]}") or self._js(raw[2:])
            return []
        if lang == "go":
            parts = raw.split("/")
            # Two components at least: a one-word path is the standard library's.
            for k in range(len(parts), 1, -1):
                if files := self.go_by_suffix.get("/".join(parts[-k:])):
                    return files
            return []
        if lang == "rust":
            return self._rust(importer, raw)
        if lang in ("java", "kotlin", "scala"):
            stem = raw.rstrip(".").replace(".", "/")
            for ext in (".java", ".kt", ".scala"):
                if hits := self._suffix(stem + ext):
                    return hits
            return []
        if lang in ("c", "cpp", "shell"):
            return self._exact(posixpath.join(here, raw)) or self._suffix(raw)
        if lang == "ruby":
            name = raw if raw.endswith(".rb") else raw + ".rb"
            return self._exact(posixpath.join(here, name)) or self._suffix(name)
        if lang == "php":
            if raw.endswith(".php"):
                return self._exact(posixpath.join(here, raw)) or self._suffix(raw)
            return self._suffix(raw.replace("\\", "/") + ".php")
        if lang == "lua":
            return self._suffix(raw.replace(".", "/") + ".lua")
        if lang == "gdscript":
            return self._suffix(raw)
        return []

    def _python(self, here: str, raw: str) -> list[str]:
        if "|" in raw:
            base, _, names = raw.partition("|")
            joiner = "" if base.endswith(".") or not base else "."
            hits = [hit for name in names.split(",") if name for hit in self._python(here, f"{base}{joiner}{name}")]
            return hits or self._python(here, base)
        level = len(raw) - len(raw.lstrip("."))
        name = raw[level:]
        parts = [p for p in name.split(".") if p]
        if level:
            base = here.split("/") if here else []
            if level > 1:
                base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
            stem = "/".join(base + parts)
            if not parts:
                return self._exact(f"{stem}/__init__.py") if stem else self._exact("__init__.py")
            return self._exact(stem + ".py") or self._exact(stem + "/__init__.py")
        if not parts:
            return []
        stem = "/".join(parts)
        if len(parts) == 1:
            # A bare name is a project module only at the root or one source directory down
            # (``src/``); deeper, ``import os`` would land on some package's own ``os.py``.
            near = [p for p in self.by_suffix.get(stem + ".py", []) if p.count("/") <= 1]
            near += [p for p in self.by_suffix.get(stem + "/__init__.py", []) if p.count("/") <= 2]
            return near[:1]
        return self._suffix(stem + ".py") or self._suffix(stem + "/__init__.py")

    def _js(self, base: str) -> list[str]:
        base = posixpath.normpath(base)
        if base in self.paths:
            return [base]
        stem = (
            base[: -len(posixpath.splitext(base)[1])]
            if posixpath.splitext(base)[1] in (".js", ".jsx", ".mjs")
            else base
        )
        for ext in _JS_EXTS:
            if stem + ext in self.paths:
                return [stem + ext]
        for ext in _JS_EXTS:
            if f"{base}/index{ext}" in self.paths:
                return [f"{base}/index{ext}"]
        return []

    def _rust(self, importer: str, raw: str) -> list[str]:
        here = posixpath.dirname(importer)
        if "::" not in raw:  # ``mod name;``
            stem = posixpath.splitext(posixpath.basename(importer))[0]
            base = here if stem in ("mod", "lib", "main") else posixpath.join(here, stem)
            return self._exact(f"{base}/{raw}.rs") or self._exact(f"{base}/{raw}/mod.rs")
        head, *rest = raw.split("::")
        for cut in range(len(rest), 0, -1):
            tail = "/".join(rest[:cut])
            if head == "crate":
                hits = self._suffix(f"src/{tail}.rs") or self._suffix(f"src/{tail}/mod.rs")
            else:
                base = here if head == "self" else posixpath.dirname(here)
                hits = self._exact(f"{base}/{tail}.rs") or self._exact(f"{base}/{tail}/mod.rs")
            if hits:
                return hits
        return []


# -- the index -------------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    """``AgentLoop``, ``agent_loop`` and ``agent-loop.ts`` all give ``agent``, ``loop`` (and ``ts``)."""
    out: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9]+", text):
        for word in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", chunk):
            if len(word) >= 2:
                out.append(word.lower())
    return out


def stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _minified(text: str) -> bool:
    return len(text) > 50_000 and text.count("\n") < len(text) / 1000


class CodeIndex:
    """One project's index: refreshed incrementally, read by :mod:`raven.codemap.query`."""

    def __init__(self, root: Path, db: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.db = db or index_path(self.root)

    def _connect(self) -> sqlite3.Connection:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db, timeout=30)
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files(
                path TEXT PRIMARY KEY, lang TEXT, size INTEGER, mtime_ns INTEGER, lines INTEGER,
                imports TEXT, deps TEXT DEFAULT '[]', indeg INTEGER DEFAULT 0,
                ptokens TEXT, stokens TEXT, skipped TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS symbols(
                path TEXT, kind TEXT, name TEXT, line INTEGER, parent TEXT, signature TEXT);
            CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path);
            CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS symbols_name_nocase ON symbols(name COLLATE NOCASE);
            """
        )
        version = f"{SCHEMA_VERSION}:{EXTRACTOR_VERSION}"
        row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        stored_root = con.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if row is None or row[0] != version or stored_root is None or stored_root[0] != str(self.root):
            con.execute("DELETE FROM files")
            con.execute("DELETE FROM symbols")
            con.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (version,))
            con.execute("INSERT OR REPLACE INTO meta VALUES('root', ?)", (str(self.root),))
            con.commit()
        return con

    @property
    def exists(self) -> bool:
        return self.db.is_file()

    def refresh(self, *, deadline_s: float | None = REFRESH_DEADLINE_S) -> RefreshStats:
        """Bring the index up to date with the tree, within ``deadline_s`` (``None``: no limit)."""
        from raven.utils.portable_lock import file_lock

        began = time.monotonic()
        deadline = began + deadline_s if deadline_s else None
        with file_lock(self.db.with_suffix(".lock")):
            stats = self._refresh(deadline)
        stats.seconds = time.monotonic() - began
        return stats

    def _refresh(self, deadline: float | None) -> RefreshStats:
        listed, source, list_cut = list_files(self.root, deadline=deadline)
        stats = RefreshStats(listed=len(listed), source=source, partial=list_cut)
        con = self._connect()
        try:
            known = {p: (s, m) for p, s, m in con.execute("SELECT path, size, mtime_ns FROM files")}
            changed = False
            if not list_cut:
                gone = set(known) - set(listed)
                for path in gone:
                    con.execute("DELETE FROM files WHERE path=?", (path,))
                    con.execute("DELETE FROM symbols WHERE path=?", (path,))
                stats.removed = len(gone)
                changed = bool(gone)
            pending = 0
            for rel in listed:
                if deadline is not None and time.monotonic() > deadline:
                    stats.partial = True
                    break
                try:
                    st = (self.root / rel).stat()
                except OSError:
                    # Listed but not on disk: a sparse checkout, or deleted and not yet staged.
                    if rel in known:
                        con.execute("DELETE FROM files WHERE path=?", (rel,))
                        con.execute("DELETE FROM symbols WHERE path=?", (rel,))
                        stats.removed += 1
                        changed = True
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if known.get(rel) == (st.st_size, st.st_mtime_ns):
                    stats.unchanged += 1
                    continue
                self._read_one(con, rel, st, stats)
                changed = True
                pending += 1
                if pending >= _BATCH:
                    con.commit()
                    pending = 0
            if changed:
                self._rebuild_graph(con)
            con.execute("INSERT OR REPLACE INTO meta VALUES('refreshed_at', ?)", (str(time.time()),))
            con.commit()
        finally:
            con.close()
        return stats

    def _read_one(self, con: sqlite3.Connection, rel: str, st: os.stat_result, stats: RefreshStats) -> None:
        lang = language_of(rel)
        symbols: list[Symbol] = []
        imports: list[str] = []
        skipped = ""
        lines = 0
        if st.st_size > MAX_FILE_BYTES:
            skipped = "too large"
        else:
            try:
                data = (self.root / rel).read_bytes()
            except OSError:
                data = b""
                skipped = "unreadable"
            if not skipped and b"\0" in data[:8192]:
                skipped = "binary"
            if not skipped:
                text = data.decode("utf-8", errors="replace")
                if _minified(text):
                    skipped = "minified"
                else:
                    lines = text.count("\n") + (0 if not text or text.endswith("\n") else 1)
                    symbols, imports = extract(text, lang)
        if skipped:
            stats.skipped += 1
        else:
            stats.parsed += 1
        stokens = Counter(stem(w) for s in symbols for w in _words(s.name))
        con.execute(
            "INSERT OR REPLACE INTO files(path, lang, size, mtime_ns, lines, imports, deps, indeg, ptokens, stokens, skipped)"
            " VALUES(?, ?, ?, ?, ?, ?, '[]', 0, ?, ?, ?)",
            (
                rel,
                lang,
                st.st_size,
                st.st_mtime_ns,
                lines,
                json.dumps(sorted(set(imports))),
                " ".join(sorted({stem(w) for w in _words(rel)})),
                " ".join(f"{w}:{n}" for w, n in sorted(stokens.items())),
                skipped,
            ),
        )
        con.execute("DELETE FROM symbols WHERE path=?", (rel,))
        con.executemany(
            "INSERT INTO symbols(path, kind, name, line, parent, signature) VALUES(?, ?, ?, ?, ?, ?)",
            [(rel, s.kind, s.name, s.line, s.parent, s.signature) for s in symbols],
        )

    def _rebuild_graph(self, con: sqlite3.Connection) -> None:
        rows = con.execute("SELECT path, lang, imports FROM files").fetchall()
        resolver = _Resolver(path for path, _, _ in rows)
        indeg: Counter[str] = Counter()
        deps: dict[str, list[str]] = {}
        for path, lang, raw in rows:
            targets: set[str] = set()
            for target in json.loads(raw or "[]"):
                targets.update(resolver.resolve(path, lang, target))
            targets.discard(path)
            deps[path] = sorted(targets)
            indeg.update(targets)
        con.executemany(
            "UPDATE files SET deps=?, indeg=? WHERE path=?",
            [(json.dumps(deps[path]), indeg.get(path, 0), path) for path, _, _ in rows],
        )

    # -- reading ---------------------------------------------------------------------------

    def _read(self) -> sqlite3.Connection:
        if not self.exists:
            raise FileNotFoundError(f"no code index for {self.root}; run a refresh first")
        return self._connect()

    def files(self, under: str = "") -> list[FileRow]:
        con = self._read()
        try:
            rows = con.execute(
                "SELECT path, lang, lines, indeg, deps, ptokens, stokens, skipped FROM files"
                + (" WHERE path = ? OR path LIKE ? ESCAPE '\\'" if under else ""),
                (under, _like_prefix(under)) if under else (),
            ).fetchall()
        finally:
            con.close()
        out = []
        for path, lang, lines, indeg, deps, ptokens, stokens, skipped in rows:
            counts = {}
            for item in (stokens or "").split():
                word, _, n = item.rpartition(":")
                counts[word] = int(n or 0)
            out.append(
                FileRow(
                    path,
                    lang,
                    lines or 0,
                    indeg or 0,
                    json.loads(deps or "[]"),
                    frozenset((ptokens or "").split()),
                    counts,
                    skipped or "",
                )
            )
        return out

    def symbols_of(self, paths: Iterable[str]) -> dict[str, list[Symbol]]:
        wanted = list(paths)
        out: dict[str, list[Symbol]] = {p: [] for p in wanted}
        if not wanted:
            return out
        con = self._read()
        try:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for path, kind, name, line, parent, signature in con.execute(
                    f"SELECT path, kind, name, line, parent, signature FROM symbols WHERE path IN ({marks}) ORDER BY path, line",  # noqa: S608 -- placeholders only
                    chunk,
                ):
                    out[path].append(Symbol(kind, name, line, parent or "", signature or ""))
        finally:
            con.close()
        return out

    def find(self, name: str, *, under: str = "", limit: int = 20) -> tuple[list[Found], str]:
        """Definitions of ``name`` (``Parent.name`` narrows by owner), and how they matched."""
        owner = ""
        for sep in ("::", "."):
            if sep in name.strip(sep):
                owner, name = name.rsplit(sep, 1)
                break
        con = self._read()
        try:
            for how, clause, value in (
                ("exact", "s.name = ?", name),
                ("case-insensitive", "s.name = ? COLLATE NOCASE", name),
                ("partial", "s.name LIKE ? ESCAPE '\\'", f"%{_escape_like(name)}%"),
            ):
                sql = (
                    "SELECT s.path, s.kind, s.name, s.line, s.parent, s.signature, f.indeg FROM symbols s"
                    f" JOIN files f ON f.path = s.path WHERE {clause}"  # noqa: S608 -- fixed clauses, values bound
                )
                args: list[object] = [value]
                if owner:
                    sql += " AND (s.parent = ? COLLATE NOCASE OR s.parent LIKE ? ESCAPE '\\')"
                    args += [owner, f"%.{_escape_like(owner)}"]
                if under:
                    sql += " AND (s.path = ? OR s.path LIKE ? ESCAPE '\\')"
                    args += [under, _like_prefix(under)]
                sql += " ORDER BY f.indeg DESC, s.path, s.line LIMIT ?"
                args.append(limit)
                rows = con.execute(sql, args).fetchall()
                if rows:
                    found = [
                        Found(p, Symbol(k, n, ln, par or "", sig or ""), deg or 0)
                        for p, k, n, ln, par, sig, deg in rows
                    ]
                    return found, how
        finally:
            con.close()
        return [], ""

    def counts(self) -> tuple[int, Counter[str]]:
        con = self._read()
        try:
            langs = Counter({lang: n for lang, n in con.execute("SELECT lang, COUNT(*) FROM files GROUP BY lang")})
            symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        finally:
            con.close()
        return symbols, langs

    def refreshed_at(self) -> float:
        if not self.exists:
            return 0.0
        con = self._connect()
        try:
            row = con.execute("SELECT value FROM meta WHERE key='refreshed_at'").fetchone()
        finally:
            con.close()
        return float(row[0]) if row else 0.0

    def drop(self) -> bool:
        removed = False
        for path in (self.db, self.db.with_suffix(".lock")):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_prefix(under: str) -> str:
    return _escape_like(under.rstrip("/")) + "/%"


__all__ = [
    "MAX_FILE_BYTES",
    "REFRESH_DEADLINE_S",
    "CodeIndex",
    "FileRow",
    "Found",
    "RefreshStats",
    "default_dir",
    "index_path",
    "list_files",
    "project_root",
    "stem",
]

"""What a source file defines and what it imports, read from its text alone.

Python is parsed with :mod:`ast`, which is exact. Every other language is read
line by line with a few patterns per language: definitions that start a line
(a class, a function, a type) and the import forms that name another file.
Nothing is compiled, run or resolved against a build, so a legacy tree with no
working toolchain maps the same as a healthy one -- at the price that a
definition written in an unusual shape is missed rather than guessed.

A member (a method) is read only one indentation step inside its class: deeper
lines are bodies, where a call looks like a declaration often enough that
reading them would fill the map with calls.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

#: Bumped whenever extraction changes what it reads, so a stored index is rebuilt.
EXTRACTOR_VERSION = 2

LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".swift": "swift",
    ".lua": "lua",
    ".sh": "shell",
    ".bash": "shell",
    ".gd": "gdscript",
    ".sql": "sql",
    ".md": "markdown",
}


@dataclass(frozen=True)
class Symbol:
    kind: str
    """``class``, ``function``, ``method``, ``type``, ``constant`` or ``section`` (a document heading)."""
    name: str
    line: int
    parent: str = ""
    """The enclosing class or type, for a member."""
    signature: str = ""


def language_of(path: str) -> str:
    dot = path.rfind(".")
    return LANG_BY_EXT.get(path[dot:].lower(), "") if dot >= 0 else ""


def extract(text: str, lang: str) -> tuple[list[Symbol], list[str]]:
    """The definitions and the raw import targets of one file's text."""
    if lang == "python":
        try:
            return _python(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass  # a file mid-edit or for another Python: the line patterns still read it
    spec = _SPECS.get(lang)
    if spec is None:
        return [], []
    return _by_lines(text, lang, spec)


# -- Python ----------------------------------------------------------------------------------

_SIG_MAX = 120


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _SIG_MAX else text[: _SIG_MAX - 1] + "…"


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return _clip(f"{prefix} {node.name}({ast.unparse(node.args)}){returns}")


def _class_sig(node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    return _clip(f"class {node.name}({bases})" if bases else f"class {node.name}")


def _python(text: str) -> tuple[list[Symbol], list[str]]:
    tree = ast.parse(text)
    symbols: list[Symbol] = []
    imports: list[str] = []

    def members(body: list[ast.stmt], parent: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(Symbol("method", node.name, node.lineno, parent, _py_signature(node)))
            elif isinstance(node, ast.ClassDef):
                symbols.append(Symbol("class", node.name, node.lineno, parent, _class_sig(node)))
                members(node.body, f"{parent}.{node.name}")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(Symbol("function", node.name, node.lineno, "", _py_signature(node)))
        elif isinstance(node, ast.ClassDef):
            symbols.append(Symbol("class", node.name, node.lineno, "", _class_sig(node)))
            members(node.body, node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) > 1:
                    symbols.append(Symbol("constant", target.id, node.lineno))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # ``from pkg import mod`` names a module as often as a symbol: written
            # ``base|name,...`` so the resolver can prefer the modules and fall back
            # to the package only when none of the names is a file.
            base = "." * node.level + (node.module or "")
            imports.append(f"{base}|{','.join(a.name for a in node.names if a.name != '*')}")
    return symbols, imports


# -- every other language, line by line ------------------------------------------------------


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


@dataclass(frozen=True)
class _Spec:
    defs: tuple[tuple[str, re.Pattern[str]], ...]
    """``(kind, pattern)``, first match wins; ``name`` is the symbol, an optional ``owner`` its parent."""
    imports: tuple[re.Pattern[str], ...] = ()
    """Patterns whose ``target`` group names another file or module."""
    member: re.Pattern[str] | None = None
    """A member definition, read one step inside an open container."""
    containers: tuple[str, ...] = ("class", "type")
    """Kinds whose indented lines below hold their members."""
    comments: tuple[str, ...] = ("//", "/*", "*")
    words: frozenset[str] = field(default_factory=frozenset)


_NOT_A_NAME = frozenset(
    {
        "if",
        "for",
        "foreach",
        "while",
        "switch",
        "catch",
        "return",
        "function",
        "new",
        "else",
        "do",
        "try",
        "with",
        "elif",
        "using",
        "lock",
        "fixed",
        "sizeof",
        "typeof",
        "when",
        "match",
        "throw",
        "super",
        "this",
    }
)

_JS_IMPORTS = (
    _p(r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"](?P<target>[^'"]+)['"]"""),
    _p(r"""^\s*export\s+[^'"]*?\s+from\s+['"](?P<target>[^'"]+)['"]"""),
    _p(r"""\brequire\(\s*['"](?P<target>[^'"]+)['"]\s*\)"""),
)
_JS_DEFS = (
    ("class", _p(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)")),
    ("function", _p(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)")),
    (
        "function",
        _p(
            r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?="
            r"\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"
        ),
    ),
    ("type", _p(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)")),
    ("type", _p(r"^\s*(?:export\s+)?(?:declare\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*=")),
    ("type", _p(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+(?P<name>[A-Za-z_$][\w$]*)")),
    ("constant", _p(r"^(?:export\s+)?const\s+(?P<name>[A-Z][A-Z0-9_]+)\s*(?::[^=]+)?=")),
)
_JS_MEMBER = _p(
    r"^\s+(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$#][\w$]*)\s*(?:<[^>]*>)?\([^;]*\)\s*(?::\s*[^{;]+)?\{\s*$"
)
_JAVA_MEMBER = _p(
    r"^\s+(?:@\w+\s+)*(?:(?:public|private|protected|internal|static|final|abstract|synchronized|override|"
    r"virtual|sealed|extern|unsafe|native|default|async|new)\s+)*(?:<[^>]+>\s*)?[\w<>\[\],.?]+\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\([^;]*$"
)
_C_FUNCTION = (
    r"^(?!(?:return|else|if|while|for|switch|typedef|namespace|using|case|do)\b)[A-Za-z_][\w\s\*&:<>,]*?"
    r"[\s\*&]\**(?P<name>~?[A-Za-z_]\w*)\s*\([^;]*$"
)

_SPECS: dict[str, _Spec] = {
    "python": _Spec(
        (
            ("class", _p(r"^\s*class\s+(?P<name>\w+)")),
            ("function", _p(r"^(?:async\s+)?def\s+(?P<name>\w+)")),
        ),
        (_p(r"^\s*from\s+(?P<target>\.*[\w.]*)\s+import\b"), _p(r"^\s*import\s+(?P<target>[\w.]+)")),
        member=_p(r"^\s+(?:async\s+)?def\s+(?P<name>\w+)"),
        containers=("class",),
        comments=("#",),
    ),
    "javascript": _Spec(_JS_DEFS, _JS_IMPORTS, _JS_MEMBER),
    "typescript": _Spec(_JS_DEFS, _JS_IMPORTS, _JS_MEMBER),
    "go": _Spec(
        (
            ("method", _p(r"^func\s+\(\s*\w*\s*\*?(?P<owner>\w+)(?:\[[^\]]*\])?\s*\)\s*(?P<name>\w+)")),
            ("function", _p(r"^func\s+(?P<name>\w+)")),
            ("type", _p(r"^type\s+(?P<name>\w+)")),
            ("constant", _p(r"^const\s+(?P<name>[A-Z]\w*)")),
        ),
        containers=(),
    ),
    "rust": _Spec(
        (
            ("function", _p(r"^(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)")),
            ("type", _p(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(?P<name>\w+)")),
            ("type", _p(r"^\s*(?:pub(?:\([^)]*\))?\s+)?type\s+(?P<name>\w+)")),
            ("type", _p(r"^impl(?:<[^>]*>)?\s+(?:[\w:]+(?:<[^>]*>)?\s+for\s+)?(?P<name>\w+)")),
            ("constant", _p(r"^(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+(?P<name>[A-Z][A-Z0-9_]*)")),
        ),
        (
            _p(r"^\s*(?:pub\s+)?use\s+(?P<target>(?:crate|self|super)::[\w:]+)"),
            _p(r"^\s*(?:pub\s+)?mod\s+(?P<target>\w+)\s*;"),
        ),
        _p(r"^\s+(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)"),
    ),
    "java": _Spec(
        (
            (
                "class",
                _p(
                    r"^\s*(?:@\w+\s+)*(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed)\s+)*"
                    r"(?:class|interface|enum|record|@interface)\s+(?P<name>\w+)"
                ),
            ),
        ),
        (_p(r"^\s*import\s+(?:static\s+)?(?P<target>[\w.]+)\s*;"),),
        _JAVA_MEMBER,
        containers=("class",),
    ),
    "kotlin": _Spec(
        (
            (
                "class",
                _p(
                    r"^\s*(?:(?:public|private|internal|open|abstract|sealed|data|enum|annotation|inner|value)\s+)*"
                    r"(?:class|interface|object)\s+(?P<name>\w+)"
                ),
            ),
            (
                "function",
                _p(
                    r"^(?:(?:public|private|internal|suspend|inline)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?(?P<name>\w+)"
                ),
            ),
        ),
        (_p(r"^\s*import\s+(?P<target>[\w.]+)"),),
        _p(
            r"^\s+(?:(?:public|private|protected|internal|override|open|suspend|inline|abstract)\s+)*fun\s+(?:<[^>]+>\s*)?(?P<name>\w+)"
        ),
        containers=("class",),
    ),
    "scala": _Spec(
        (
            ("class", _p(r"^\s*(?:(?:case|abstract|sealed|final|implicit)\s+)*(?:class|trait|object)\s+(?P<name>\w+)")),
            ("function", _p(r"^def\s+(?P<name>\w+)")),
        ),
        (_p(r"^\s*import\s+(?P<target>[\w.]+)"),),
        _p(r"^\s+(?:(?:override|private|protected|final|implicit)\s+)*def\s+(?P<name>\w+)"),
        containers=("class",),
    ),
    "csharp": _Spec(
        (
            (
                "class",
                _p(
                    r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|sealed|abstract|partial|"
                    r"readonly|ref|file)\s+)*(?:class|interface|struct|enum|record)\s+(?P<name>\w+)"
                ),
            ),
        ),
        (),
        _JAVA_MEMBER,
        containers=("class",),
    ),
    "ruby": _Spec(
        (
            ("class", _p(r"^\s*(?:class|module)\s+(?P<name>[\w:]+)")),
            ("function", _p(r"^def\s+(?:self\.)?(?P<name>[\w?!=]+)")),
        ),
        (_p(r"""^\s*require(?:_relative)?\s*\(?\s*['"](?P<target>[^'"]+)['"]"""),),
        _p(r"^\s+def\s+(?:self\.)?(?P<name>[\w?!=]+)"),
        containers=("class",),
        comments=("#",),
    ),
    "php": _Spec(
        (
            ("class", _p(r"^\s*(?:(?:abstract|final|readonly)\s+)*(?:class|interface|trait|enum)\s+(?P<name>\w+)")),
            ("function", _p(r"^function\s+&?\s*(?P<name>\w+)")),
        ),
        (
            _p(r"^\s*use\s+(?P<target>[\w\\]+)"),
            _p(r"""^\s*(?:require|include)(?:_once)?\s*\(?\s*['"](?P<target>[^'"]+)['"]"""),
        ),
        _p(r"^\s+(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?\s*(?P<name>\w+)"),
        containers=("class",),
        comments=("//", "/*", "*", "#"),
    ),
    "c": _Spec(
        (
            ("type", _p(r"^(?:typedef\s+)?(?:struct|union|enum)\s+(?P<name>\w+)\s*\{?\s*$")),
            ("constant", _p(r"^#\s*define\s+(?P<name>[A-Z][A-Z0-9_]+)")),
            ("function", _p(_C_FUNCTION)),
        ),
        (_p(r"""^\s*#\s*include\s+"(?P<target>[^"]+)\""""),),
        containers=(),
    ),
    "cpp": _Spec(
        (
            (
                "class",
                _p(
                    r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(?:\w+\s+)?(?P<name>\w+)\s*(?:final\s*)?(?::[^;{]*)?\{?\s*$"
                ),
            ),
            ("type", _p(r"^\s*(?:enum(?:\s+class)?|union)\s+(?P<name>\w+)")),
            ("constant", _p(r"^#\s*define\s+(?P<name>[A-Z][A-Z0-9_]+)")),
            ("method", _p(r"^(?!\s)[\w\s\*&:<>,]*?\b(?P<owner>\w+)::(?P<name>~?\w+)\s*\([^;]*$")),
            ("function", _p(_C_FUNCTION)),
        ),
        (_p(r"""^\s*#\s*include\s+"(?P<target>[^"]+)\""""),),
        _p(
            r"^\s+(?:(?:virtual|static|inline|explicit|constexpr)\s+)*[\w:<>,\*&\s]+?[\s\*&](?P<name>~?\w+)\s*\([^;]*\)\s*(?:const\s*)?(?:override\s*)?\{?\s*$"
        ),
        containers=("class",),
    ),
    "swift": _Spec(
        (
            (
                "class",
                _p(
                    r"^\s*(?:(?:public|private|internal|open|final|fileprivate)\s+)*(?:class|struct|enum|protocol|extension|actor)\s+(?P<name>\w+)"
                ),
            ),
            ("function", _p(r"^(?:(?:public|private|internal)\s+)*func\s+(?P<name>\w+)")),
        ),
        (),
        _p(
            r"^\s+(?:(?:public|private|internal|open|override|static|class|final|mutating|fileprivate)\s+)*func\s+(?P<name>\w+)"
        ),
        containers=("class",),
    ),
    "lua": _Spec(
        (("function", _p(r"^(?:local\s+)?function\s+(?:(?P<owner>[\w.]+)[.:])?(?P<name>\w+)")),),
        (_p(r"""\brequire\s*\(?\s*['"](?P<target>[^'"]+)['"]"""),),
        containers=(),
        comments=("--",),
    ),
    "shell": _Spec(
        (("function", _p(r"^\s*(?:function\s+)?(?P<name>[\w-]+)\s*\(\s*\)\s*\{?")),),
        (_p(r"""^\s*(?:source|\.)\s+['"]?(?P<target>[^\s'"$]+)"""),),
        containers=(),
        comments=("#",),
    ),
    "gdscript": _Spec(
        (
            ("class", _p(r"^class_name\s+(?P<name>\w+)")),
            ("class", _p(r"^class\s+(?P<name>\w+)")),
            ("function", _p(r"^(?:static\s+)?func\s+(?P<name>\w+)")),
            ("constant", _p(r"^const\s+(?P<name>[A-Z][A-Z0-9_]*)")),
        ),
        (_p(r"""\b(?:pre)?load\(\s*"res://(?P<target>[^"]+)"\s*\)"""),),
        _p(r"^\s+(?:static\s+)?func\s+(?P<name>\w+)"),
        containers=("class",),
        comments=("#",),
    ),
    "sql": _Spec(
        (
            (
                "type",
                _p(
                    r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:temporary\s+|temp\s+)?(?:table|view|materialized\s+view)"
                    r"\s+(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"`\[\]]+)"
                ),
            ),
            (
                "function",
                _p(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:function|procedure|trigger)\s+(?P<name>[\w.\"`\[\]]+)"),
            ),
        ),
        containers=(),
        comments=("--",),
    ),
    "markdown": _Spec((("section", _p(r"^#{1,3}\s+(?P<name>\S.*?)\s*#*\s*$")),), containers=(), comments=()),
}

_GO_IMPORT = _p(r"""^(?:import\s+)?(?:[\w.]+\s+)?"(?P<target>[^"]+)"$""")


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


@dataclass
class _Open:
    indent: int
    name: str
    member_indent: int | None = None


def _by_lines(text: str, lang: str, spec: _Spec) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    imports: list[str] = []
    stack: list[_Open] = []
    fenced = False
    go_block = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if lang == "markdown":
            if stripped.startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
        elif spec.comments and stripped.startswith(spec.comments):
            continue

        if lang == "go":
            if stripped.startswith("import ("):
                go_block = True
                continue
            if go_block:
                if stripped.startswith(")"):
                    go_block = False
                elif match := _GO_IMPORT.match(stripped):
                    imports.append(match.group("target"))
                continue
            if stripped.startswith("import ") and (match := _GO_IMPORT.match(stripped)):
                imports.append(match.group("target"))
                continue
        for pattern in spec.imports:
            if match := pattern.search(line):
                imports.append(match.group("target"))
                break

        depth = _indent(line)
        if stripped not in ("{", "{{"):
            while stack and depth <= stack[-1].indent:
                stack.pop()
        if stack and stack[-1].member_indent is None and depth > stack[-1].indent:
            stack[-1].member_indent = depth
        inside = stack[-1] if stack else None
        at_member_level = inside is not None and depth == inside.member_indent

        matched = False
        for kind, pattern in spec.defs:
            if (match := pattern.match(line)) is None:
                continue
            name = match.group("name").strip('"`[]')
            if name in _NOT_A_NAME:
                continue
            if inside is not None and not at_member_level:
                matched = True  # a definition inside a body: not part of the outline
                break
            owner = match.groupdict().get("owner") or (inside.name if inside else "")
            if kind == "function" and owner:
                kind = "method"
            symbols.append(Symbol(kind, name, number, owner, _clip(stripped.rstrip("{").rstrip())))
            if kind in spec.containers:
                stack.append(_Open(depth, f"{inside.name}.{name}" if inside else name))
            matched = True
            break
        if matched or spec.member is None or not at_member_level or inside is None:
            continue
        if (match := spec.member.match(line)) is None:
            continue
        name = match.group("name")
        if name in _NOT_A_NAME or stripped.startswith(("return ", "new ", "throw ", "await ", "yield ")):
            continue
        symbols.append(Symbol("method", name, number, inside.name, _clip(stripped.rstrip("{").rstrip())))
    return symbols, imports


__all__ = ["EXTRACTOR_VERSION", "LANG_BY_EXT", "Symbol", "extract", "language_of"]

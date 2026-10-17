"""Gate over a finished build: no key-shaped strings and no internal addresses (spec 12.4).

    python packaging/scripts/check_build.py <dir-or-file> [<dir-or-file> ...]

Reads every text file under the paths (and the printable runs of every binary one, which is what an
installer and a frozen program are) and fails when it finds:

* a provider key shape -- ``sk-`` followed by 20 or more key characters, ``gsk_``, ``AIza``;
* the literal ``dev-secret``;
* an address of the build host's own network -- ``192.168.``, ``10.x.x.x`` -- or the server's
  address and its SSH port (``95.85.125.50``);
* ``127.0.0.1:8115``, the internal port of the server side.

``sk-ab-`` followed by the shape of a *placeholder* (zeros, ``x`` runs, ``...``) is allowed: the
docs and the tests name the agent key's prefix. A line a reviewer has judged a false positive is
listed in ``ALLOW`` below with the reason, never skipped by a flag.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

#: A key-shaped string: the prefix a provider uses and then the characters its keys are made of.
KEY_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("provider key (sk-...)", re.compile(rb"\bsk-(?!ab-)[A-Za-z0-9_\-]{20,}")),
    ("agent key (sk-ab-...)", re.compile(rb"\bsk-ab-[A-Za-z0-9]{24,}")),
    ("provider key (gsk_...)", re.compile(rb"\bgsk_[A-Za-z0-9]{20,}")),
    ("Google key (AIza...)", re.compile(rb"\bAIza[0-9A-Za-z_\-]{30,}")),
)

ADDRESS_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("dev secret", re.compile(rb"dev-secret")),
    ("private network address (192.168.)", re.compile(rb"\b192\.168\.\d{1,3}\.\d{1,3}")),
    ("private network address (10.)", re.compile(rb"(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("the server's address", re.compile(rb"95\.85\.125\.50")),
    ("an internal server port", re.compile(rb"127\.0\.0\.1:8115\b")),
)

#: (path suffix, pattern name, reason) triples a reviewer judged to be a false positive. They are
#: reviewed findings of the first full build (2026-10-17): every one is example data inside a
#: third-party package, none is ours. A new finding in a new file is a failure until it is reviewed.
_NET10 = "private network address (10.)"
_NET192 = "private network address (192.168.)"
_SK = "provider key (sk-...)"
ALLOW: tuple[tuple[str, str, str], ...] = (
    ("botocore/data/ec2/2016-04-01/examples-1.json", _NET10, "botocore API example (10.0.0.82)"),
    ("botocore/data/ec2/2016-09-15/examples-1.json", _NET10, "botocore API example (10.0.0.82)"),
    ("botocore/data/ec2/2016-11-15/examples-1.json", _NET10, "botocore API example (10.0.0.82)"),
    ("botocore/data/storagegateway/2013-06-30/examples-1.json", _NET10, "botocore API example (10.1.1.1)"),
    (
        "litellm/llms/huggingface/huggingface_llms_metadata/hf_text_generation_models.txt",
        _SK,
        "a model name on Hugging Face (sk-kogptv2-...), not a key",
    ),
    ("litellm/llms/ollama/completion/transformation.py", _NET10, "an example address in a litellm docstring"),
    ("litellm/llms/vertex_ai/common_utils.py", _NET10, "an example address in a litellm docstring"),
    (
        "litellm/proxy/_experimental/out/_next/static/chunks/1etef47tue5fx.js",
        _NET10,
        "litellm admin page: private ranges list",
    ),
    (
        "litellm/proxy/_experimental/out/_next/static/chunks/1etef47tue5fx.js",
        _NET192,
        "litellm admin page: private ranges list",
    ),
    ("litellm/proxy/_types.py", _NET10, "litellm's own list of private ranges"),
    ("litellm/proxy/_types.py", _NET192, "litellm's own list of private ranges"),
    ("litellm/proxy/auth/ip_address_utils.py", _NET10, "litellm's own list of private ranges"),
    ("litellm/proxy/auth/ip_address_utils.py", _NET192, "litellm's own list of private ranges"),
    ("litellm/proxy/custom_auth_auto.py", _SK, "a placeholder key in litellm's proxy example"),
    (
        "litellm/proxy/management_endpoints/key_management_endpoints.py",
        _SK,
        "an example key in litellm's API docstring",
    ),
    ("pythonnet/runtime/Python.Runtime.deps.json", _NET10, "a dependency version number (10.0.0.12)"),
)

#: Where the scan does not go: a sibling's metadata cannot hold ours, and the cache directories are
#: not shipped.
SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules"})

#: Python and its libraries print version triples as 10.x.y.z-looking runs in compiled tables; a
#: binary file is read for key shapes only, which are anchored on a prefix, never for the numeric
#: addresses -- the one pattern family that is noise in machine code.
_BINARY_SUFFIXES = frozenset(
    {".exe", ".dll", ".pyd", ".so", ".bin", ".onnx", ".pyc", ".lance", ".ico", ".png", ".jpg", ".woff2"}
)


@dataclass(frozen=True)
class Finding:
    path: Path
    what: str
    sample: str

    def __str__(self) -> str:
        return f"{self.path}: {self.what}: {self.sample}"


#: What the tail of a placeholder key looks like: one repeated character, or an obvious filler.
_PLACEHOLDER = re.compile(rb"^(.)\1+$|xxxx|0000|1234|abcd|\.\.\.", re.IGNORECASE)


def _is_placeholder(token: bytes) -> bool:
    """Whether the part of a key-shaped string after its prefix is filler rather than a key."""
    tail = re.split(rb"[-_]", token, maxsplit=2)[-1]
    return bool(_PLACEHOLDER.search(tail))


def _allowed(path: Path, what: str) -> bool:
    return any(str(path).replace("\\", "/").endswith(suffix) and what == name for suffix, name, _ in ALLOW)


def _walk(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not (SKIP_DIRS & set(path.parts)):
            yield path


def scan_bytes(path: Path, data: bytes) -> list[Finding]:
    """The findings in one file's bytes."""
    patterns = list(KEY_PATTERNS)
    if path.suffix.lower() not in _BINARY_SUFFIXES:
        patterns += list(ADDRESS_PATTERNS)
    else:
        patterns += [
            p for p in ADDRESS_PATTERNS if p[0] in {"dev secret", "the server's address", "an internal server port"}
        ]
    found: list[Finding] = []
    for what, pattern in patterns:
        if _allowed(path, what):
            continue
        for match in pattern.finditer(data):
            is_key = what.startswith(("provider", "agent", "Google"))
            if is_key and _is_placeholder(match.group(0)):
                continue
            text = match.group(0).decode("ascii", "replace")
            # Show enough to find it, never the whole of what looks like a key.
            shown = text[:8] + "..." if is_key else text
            found.append(Finding(path, what, shown))
            break
    return found


def scan(paths: Iterable[Path]) -> list[Finding]:
    """Every finding under the paths."""
    found: list[Finding] = []
    for root in paths:
        for path in _walk(root):
            try:
                data = path.read_bytes()
            except OSError as exc:
                found.append(Finding(path, "unreadable", str(exc)))
                continue
            found.extend(scan_bytes(path, data))
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: check_build.py <dir-or-file> [<dir-or-file> ...]", file=sys.stderr)
        return 2
    roots = [Path(a) for a in args]
    missing = [r for r in roots if not r.exists()]
    if missing:
        print("check_build: no such path: " + ", ".join(str(m) for m in missing), file=sys.stderr)
        return 2
    findings = scan(roots)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"check_build: {len(findings)} finding(s); the build is not released", file=sys.stderr)
        return 1
    print(f"check_build: OK ({len(roots)} path(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

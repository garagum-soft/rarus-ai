"""How demanding a request is, read from its text: a score, a tier and the reasons for both.

Deterministic and free -- no model call, no embedding -- so it can run before
every turn. The signals are the ones that separate "thanks, translate this" from
"refactor these five modules": how long the request is, whether it carries code
or a stack trace, how many files it names, whether it lists several steps,
whether it asks to design, refactor, migrate or debug, and whether it asks
outright for careful thought. Keywords are matched in English, Russian and
Chinese. The score starts at the middle and each signal moves it; the tier is
where it lands against two thresholds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from raven.i18n import zh_lexicon

TIERS = ("simple", "standard", "complex")


def _any(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(w) for w in words)


# The CJK Unified Ideographs block, spelled by code point so this module stays
# free of CJK text (tests/test_i18n_boundary.py).
_CJK = re.compile(f"[{chr(0x4E00)}-{chr(0x9FFF)}]")


_BASE = 0.4

_CODE = re.compile(
    r"```|Traceback \(most recent call last\)|^\s+at [\w.$<>]+\(|^\s*File \".+\", line \d+|\bException\b|\berror:", re.M
)
_PATH = re.compile(
    r"(?<![\w/])(?:[\w.-]+/)*[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|kt|cs|rb|php|c|h|cc|cpp|hpp|swift|sql|md|json|ya?ml|toml|ini|sh|css|html|vue)\b"
)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+\S", re.M)
_STEP_WORDS = re.compile(
    r"\b(?:then|after that|afterwards|and also|finally|step \d)\b|потом|затем|после этого|а также|в конце|"
    + _any(zh_lexicon.COMPLEXITY_STEP_WORDS),
    re.I,
)
_HEAVY = re.compile(
    r"\b(?:design|architect\w*|refactor\w*|migrat\w*|optimi[sz]\w*|prove|analy[sz]\w*|investigat\w*|debug\w*|root cause"
    r"|trade-?offs?|implement\w*|from scratch|security audit|benchmark\w*|scal\w+ out|concurren\w+)\b"
    r"|спроектир\w*|архитектур\w*|рефактор\w*|миграц\w*|мигрир\w*|оптимиз\w*|докаж\w*|проанализир\w*|анализ\w*|исследу\w*"
    r"|отлад\w*|найди причину|реализу\w*|реализац\w*|с нуля|компромисс\w*|" + _any(zh_lexicon.COMPLEXITY_HEAVY_WORDS),
    re.I,
)
_LIGHT = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|ok|okay)\b|\b(?:translate|rephrase|spell|define|what time|what's the date|convert)\b"
    r"|^\s*(?:привет|здравствуй\w*|спасибо|ок|хорошо)\b|переведи|перефразируй|что такое|который час|сколько будет"
    rf"|^\s*(?:{_any(zh_lexicon.COMPLEXITY_LIGHT_OPENERS)})|" + _any(zh_lexicon.COMPLEXITY_LIGHT_WORDS),
    re.I,
)
_CAREFUL = re.compile(
    r"\b(?:think (?:hard|carefully|deeply)|step by step|be thorough|carefully|in depth)\b"
    r"|подумай (?:хорошо|внимательно|как следует)|тщательно|глубоко|пошагово|"
    + _any(zh_lexicon.COMPLEXITY_CAREFUL_WORDS),
    re.I,
)


@dataclass(frozen=True)
class Assessment:
    score: float
    tier: str
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        why = ", ".join(self.reasons) if self.reasons else "nothing stands out"
        return f"{self.tier} ({self.score:.2f}: {why})"


def _words(text: str) -> int:
    # CJK text has no spaces: count its characters in pairs, roughly a word each.
    cjk = len(_CJK.findall(text))
    return len(re.findall(r"[^\W\d_]+|\d+", _CJK.sub(" ", text))) + cjk // 2


def assess(prompt: str, *, simple_below: float = 0.3, complex_from: float = 0.65) -> Assessment:
    """Score ``prompt`` in [0, 1] and place it in a tier."""
    text = prompt or ""
    score = _BASE
    reasons: list[str] = []

    words = _words(text)
    if words < 6:
        score -= 0.2
        reasons.append("short")
    elif words > 250:
        score += 0.3
        reasons.append(f"very long ({words} words)")
    elif words > 60:
        score += 0.15
        reasons.append(f"long ({words} words)")

    if _CODE.search(text):
        score += 0.2
        reasons.append("code or a stack trace")

    files = {m.group(0) for m in _PATH.finditer(text)}
    if len(files) >= 3:
        score += 0.15
        reasons.append(f"names {len(files)} files")

    items = len(_LIST_ITEM.findall(text))
    steps = len(_STEP_WORDS.findall(text))
    if items >= 3 or steps >= 2:
        score += 0.15
        reasons.append("several steps")

    heavy = {m.group(0).lower() for m in _HEAVY.finditer(text)}
    if heavy:
        score += min(0.12 * len(heavy), 0.3)
        reasons.append("asks to " + "/".join(sorted(heavy)[:3]))

    if _LIGHT.search(text) and not heavy:
        score -= 0.15
        reasons.append("a quick ask")

    if _CAREFUL.search(text):
        score += 0.3
        reasons.append("asks for careful thought")

    score = max(0.0, min(1.0, round(score, 3)))
    tier = "simple" if score < simple_below else "complex" if score >= complex_from else "standard"
    return Assessment(score, tier, reasons)


__all__ = ["TIERS", "Assessment", "assess"]

"""Credits: the administrator's price table, read against each engine's usage files.

The administrator sets, for each model, three prices in US dollars per million
tokens -- input, output, and a cache hit (an input token read from the
provider's prompt cache) -- plus how many credits a dollar is
(``credits_per_usd``). A call's price is its tokens at those prices; cache
writes are priced as input. What the account is charged is that price, in
credits, times its share:

- the full price (``lent_access_share``, 1.0) for a call made on access the
  server lends it -- the administrator's keys or subscriptions;
- a tenth (``own_access_share``, 0.1) for a call made on the account's own key
  or subscription, where the server only orchestrates.

Which access a call used is read off the account's own engine config
(``Config.get_provider_name``): a provider the account was lent is the
server's, any other is its own. An external CLI agent reports no usage, so it
is charged on the estimate its engine writes (:mod:`raven.accounts.estimate`),
priced as ``cli:<agent>`` (or ``cli:*``), and marked estimated in the ledger.

A model the table does not name is priced by its ``*`` entry; with no ``*``
either the call is recorded as unpriced and costs nothing, and the admin page
lists the unpriced models so the gap is visible.

Charged after the fact, from the usage files the engine writes as it goes, so
a balance can go below zero by what was running when it reached zero.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.accounts.paths import accounts_root
from raven.accounts.settings import HubSettings
from raven.accounts.store import Account, AccountStore
from raven.utils.atomic_io import atomic_replace, locked_append, write_transaction

PER_TOKENS = 1_000_000

DEFAULT_PRICES: dict[str, dict[str, float]] = {
    # Placeholders so nothing is free by accident on a fresh server; the
    # administrator replaces them in the admin page.
    "*": {"input": 3.0, "output": 15.0, "cacheHit": 0.3},
    "cli:*": {"input": 3.0, "output": 15.0, "cacheHit": 0.0},
}


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_hit: float

    @classmethod
    def of(cls, raw: Any) -> Price | None:
        if not isinstance(raw, dict):
            return None
        try:
            values = (float(raw.get("input", 0)), float(raw.get("output", 0)), float(raw.get("cacheHit", 0)))
        except (TypeError, ValueError):
            return None
        if any(v < 0 for v in values):
            return None
        return cls(*values)

    def to_wire(self) -> dict[str, float]:
        return {"input": self.input, "output": self.output, "cacheHit": self.cache_hit}


class PriceTable:
    """``accounts/prices.json``: ``{model: {input, output, cacheHit}}`` in US dollars per million tokens."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_root() / "prices.json"

    def load(self) -> dict[str, Price]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            raw = DEFAULT_PRICES
        table = {str(k): p for k, v in raw.items() if (p := Price.of(v)) is not None}
        return table

    def save(self, table: dict[str, Price]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_wire() for k, v in sorted(table.items())}
        atomic_replace(self.path, json.dumps(data, indent=2))

    def ensure(self) -> None:
        """Write the placeholder table on a fresh server, so the admin page has something to edit."""
        if not self.path.exists():
            self.save({k: p for k, v in DEFAULT_PRICES.items() if (p := Price.of(v)) is not None})

    def set(self, model: str, price: Price | None) -> dict[str, Price]:
        """Set (or, with None, remove) one model's price."""
        model = (model or "").strip()
        if not model:
            raise ValueError("name the model the price is for")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            table = self.load()
            if price is None:
                table.pop(model, None)
            else:
                table[model] = price
            self.save(table)
        return table


def lookup(table: dict[str, Price], model: str) -> tuple[Price | None, str | None]:
    """The price for ``model`` and the table entry that gave it; ``(None, None)`` when nothing does."""
    if model in table:
        return table[model], model
    folded = {k.lower(): k for k in table}
    candidates = [model.lower()]
    if "/" in model and not model.startswith("cli:"):
        candidates.append(model.split("/", 1)[1].lower())
    for candidate in candidates:
        if candidate in folded:
            return table[folded[candidate]], folded[candidate]
    default = "cli:*" if model.startswith("cli:") and "cli:*" in table else "*"
    if default in table:
        return table[default], default
    return None, None


def cli_provider(agent: str) -> str:
    """Whose subscription an external CLI agent most likely runs on, for the lent/own split."""
    name = (agent or "").lower()
    if "claude" in name:
        return "claude_subscription"
    if "codex" in name:
        return "openai_codex"
    return "cli"


def price_usd(row: dict[str, Any], price: Price) -> float:
    fresh = int(row.get("input_tokens") or 0) + int(row.get("cache_write_tokens") or 0)
    return (
        fresh * price.input
        + int(row.get("cache_read_tokens") or 0) * price.cache_hit
        + int(row.get("output_tokens") or 0) * price.output
    ) / PER_TOKENS


# -- reading an engine's usage files ---------------------------------------------------------


@dataclass
class Charge:
    """What one model cost one account over a scan."""

    model: str
    provider: str
    share: float
    estimated: bool
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    credits: float = 0.0
    priced_as: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "share": self.share,
            "estimated": self.estimated,
            "calls": self.calls,
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "cacheRead": self.cache_read_tokens,
                "cacheWrite": self.cache_write_tokens,
            },
            "usd": round(self.usd, 6),
            "credits": round(self.credits, 6),
            "pricedAs": self.priced_as,
        }


@dataclass
class _Cursor:
    offsets: dict[str, int] = field(default_factory=dict)


def _engine_config(home: Path) -> Any:
    from raven.config.schema import Config

    try:
        raw = json.loads((home / "config.json").read_text(encoding="utf-8"))
        return Config.model_validate(raw)
    except Exception:  # noqa: BLE001 - an unreadable config resolves no provider; the call reads as own access
        return None


def provider_of(config: Any, model: str) -> str:
    if model.startswith("cli:"):
        return cli_provider(model[4:])
    if config is None:
        return ""
    try:
        return config.get_provider_name(model) or ""
    except Exception:  # noqa: BLE001
        return ""


class Meter:
    """Reads each engine's new usage rows, prices them and charges the account."""

    PATTERNS = ("usage-*.jsonl", "external-usage-*.jsonl")

    def __init__(
        self,
        store: AccountStore | None = None,
        prices: PriceTable | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.store = store or AccountStore()
        self.prices = prices or PriceTable()
        self.ledger = ledger or Ledger()

    @staticmethod
    def cursor_path(account: Account) -> Path:
        from raven.accounts.paths import user_dir

        return user_dir(account.id) / "meter.json"

    def _cursor(self, account: Account, telemetry: Path) -> tuple[_Cursor, bool]:
        path = self.cursor_path(account)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _Cursor({str(k): int(v) for k, v in raw.get("offsets", {}).items()}), False
        except (OSError, ValueError, AttributeError):
            pass
        # A meter starting on an engine that already ran (the server's own home)
        # bills from now on, not the history before accounts mode.
        offsets = {}
        for pattern in self.PATTERNS:
            for file in telemetry.glob(pattern):
                try:
                    offsets[file.name] = file.stat().st_size
                except OSError:
                    continue
        return _Cursor(offsets), True

    def _save_cursor(self, account: Account, cursor: _Cursor, present: set[str]) -> None:
        path = self.cursor_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        kept = {k: v for k, v in cursor.offsets.items() if k in present}
        atomic_replace(path, json.dumps({"offsets": kept, "at": time.time()}))

    def read_new(self, account: Account, home: Path) -> list[dict[str, Any]]:
        """The usage rows written since the last read, whole lines only, and advance the cursor."""
        telemetry = home / "telemetry"
        cursor, fresh = self._cursor(account, telemetry)
        rows: list[dict[str, Any]] = []
        present: set[str] = set()
        for pattern in self.PATTERNS:
            for file in sorted(telemetry.glob(pattern)):
                present.add(file.name)
                start = cursor.offsets.get(file.name, 0)
                try:
                    with file.open("rb") as handle:
                        handle.seek(start)
                        chunk = handle.read()
                except OSError:
                    continue
                end = chunk.rfind(b"\n")
                if end < 0:
                    continue
                cursor.offsets[file.name] = start + end + 1
                for line in chunk[: end + 1].splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("_type") is None and row.get("model"):
                        rows.append(row)
        if fresh or rows or present:
            self._save_cursor(account, cursor, present)
        return rows

    def price(
        self, account: Account, home: Path, rows: Iterable[dict[str, Any]], settings: HubSettings
    ) -> list[Charge]:
        table = self.prices.load()
        config = _engine_config(home)
        charges: dict[tuple[str, str, float, bool], Charge] = {}
        for row in rows:
            model = str(row.get("model") or "")
            provider = provider_of(config, model)
            share = settings.lent_access_share if provider and provider in account.lent else settings.own_access_share
            estimated = bool(row.get("estimated"))
            key = (model, provider, share, estimated)
            charge = charges.get(key) or charges.setdefault(key, Charge(model, provider, share, estimated))
            price, priced_as = lookup(table, model)
            charge.calls += 1
            charge.input_tokens += int(row.get("input_tokens") or 0)
            charge.output_tokens += int(row.get("output_tokens") or 0)
            charge.cache_read_tokens += int(row.get("cache_read_tokens") or 0)
            charge.cache_write_tokens += int(row.get("cache_write_tokens") or 0)
            charge.priced_as = priced_as
            if price is not None:
                usd = price_usd(row, price)
                charge.usd += usd
                charge.credits += usd * settings.credits_per_usd * share
        return list(charges.values())

    def charge(self, account: Account, home: Path, settings: HubSettings) -> list[Charge]:
        """Read, price and charge one account's new usage; the charges made."""
        rows = self.read_new(account, home)
        if not rows:
            return []
        charges = self.price(account, home, rows, settings)
        total = round(sum(c.credits for c in charges), 6)
        balance = self.store.adjust_balance(account.id, -total) if total else account.balance
        for charge in charges:
            detail = charge.to_wire()
            detail.pop("credits")
            self.ledger.record(account.id, "charge", -round(charge.credits, 6), balance, **detail)
        if any(c.priced_as is None for c in charges):
            logger.warning(
                "account {} used a model with no price: {}",
                account.username,
                [c.model for c in charges if c.priced_as is None],
            )
        return charges


class Ledger:
    """``accounts/ledger.jsonl``: every charge and top-up, append-only."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_root() / "ledger.jsonl"

    def record(self, account_id: str, kind: str, credits: float, balance: float, **detail: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), "account": account_id, "kind": kind, "credits": credits, "balance": balance, **detail}
        locked_append(self.path, [json.dumps(row, ensure_ascii=False, default=str)])

    def rows(self, account_id: str | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if account_id is None or row.get("account") == account_id:
                out.append(row)
                if len(out) >= limit:
                    break
        return out

    def unpriced_models(self, *, limit: int = 2000) -> list[str]:
        return sorted(
            {str(r.get("model")) for r in self.rows(limit=limit) if r.get("kind") == "charge" and not r.get("pricedAs")}
        )


def welcome_bonus(store: AccountStore, ledger: Ledger, account_id: str, settings: HubSettings) -> float:
    """Credit a new account with the welcome bonus, on the ledger like any top-up; the balance."""
    if settings.new_account_credits <= 0:
        return store.get(account_id).balance
    return top_up(store, ledger, account_id, settings.new_account_credits, actor="welcome bonus")


def top_up(
    store: AccountStore, ledger: Ledger, account_id: str, credits: float, *, actor: str, note: str = ""
) -> float:
    """Add (or, negative, take back) credits; the new balance."""
    if not credits:
        raise ValueError("a top-up is a non-zero number of credits")
    balance = store.adjust_balance(account_id, credits)
    ledger.record(account_id, "topup", round(float(credits), 6), balance, actor=actor, note=note)
    return balance


__all__ = [
    "DEFAULT_PRICES",
    "Charge",
    "Ledger",
    "Meter",
    "Price",
    "PriceTable",
    "cli_provider",
    "lookup",
    "price_usd",
    "provider_of",
    "top_up",
    "welcome_bonus",
]

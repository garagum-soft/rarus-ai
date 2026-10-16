"""Accounts, roles and permissions, passwords, sign-in sessions and the audit log.

Three roles and no more: the **superadmin** (the first account, who can do
everything and is the only one who makes or unmakes administrators), an
**admin** (whatever permissions the superadmin granted, from :data:`PERMISSIONS`),
and a **user**. Every account is separate from every other: its own engine, its
own keys and subscriptions, its own agents.

Passwords are kept as scrypt hashes only. A sign-in session is a random token
in an HttpOnly cookie; only its SHA-256 is written down. The audit log is
append-only and records who did what to whom, never a password or a key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from raven.accounts.paths import accounts_root
from raven.utils.atomic_io import atomic_replace, locked_append, write_transaction

ROLES = ("superadmin", "admin", "user")

PERMISSIONS: dict[str, str] = {
    "points.topup": "top up an account's credits",
    "users.manage": "create, disable and enable accounts, and lend the server's model access to one",
    "prices.edit": "set the price of each model and the credit rate",
    "settings.edit": "registration, Google Sign-In, the public address and the disk allowance",
    "audit.read": "read the audit log and every account's usage",
}

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 256

# scrypt at n=2^14, r=8: 16 MiB and tens of milliseconds per check -- the
# parameters the standard library's own documentation shows, slow enough to make
# a stolen file expensive to guess at and quick enough for a sign-in.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32

SESSION_TTL_S = 30 * 24 * 3600
_SEEN_EVERY_S = 600


class AccountError(ValueError):
    """A request about accounts that cannot be granted, in words a person can act on."""


# -- passwords -------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    check_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN
    )
    enc = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${enc(salt).decode()}${enc(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AccountError(f"a password needs at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"a password holds at most {MAX_PASSWORD} characters")


def normal_username(name: str) -> str:
    username = (name or "").strip().lower()
    if not _USERNAME.match(username):
        raise AccountError(
            "a username is 2 to 32 characters: latin letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    return username


# -- accounts --------------------------------------------------------------------------------


@dataclass
class Account:
    id: str
    username: str
    role: str = "user"
    display_name: str = ""
    email: str = ""
    password_hash: str = ""
    google_sub: str = ""
    permissions: list[str] = field(default_factory=list)
    balance: float = 0.0
    """Credits. Charged after the work it pays for, so it can go below zero by what was running."""
    lent: list[str] = field(default_factory=list)
    """Model access the server lends this account: provider names from the server's own config
    (their keys are copied into the account's engine) and ``claude_subscription`` /
    ``openai_codex`` for the server's subscriptions. Charged at the full price; the account's
    own keys and subscriptions at a tenth (:mod:`raven.accounts.billing`)."""
    disabled: bool = False
    created_at: float = 0.0
    server_home: bool = False
    """The engine runs on the server's own home rather than one under ``accounts/users``."""
    page_port: int = 0
    health_port: int = 0

    def can(self, permission: str) -> bool:
        if self.disabled:
            return False
        if self.role == "superadmin":
            return True
        return self.role == "admin" and permission in self.permissions

    @property
    def is_admin(self) -> bool:
        return self.role in ("superadmin", "admin")

    def public(self) -> dict[str, Any]:
        """What a page may see of this account: everything but the password hash."""
        data = asdict(self)
        data.pop("password_hash", None)
        data["has_password"] = bool(self.password_hash)
        data["google_linked"] = bool(self.google_sub)
        data.pop("google_sub", None)
        return data


_FIELDS = {f.name for f in fields(Account)}


class AccountStore:
    """``accounts.json``: every account, read and written whole under its lock."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_root() / "accounts.json"

    def _read(self) -> list[Account]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = raw.get("accounts") if isinstance(raw, dict) else None
        return [
            Account(**{k: v for k, v in row.items() if k in _FIELDS}) for row in rows or [] if isinstance(row, dict)
        ]

    def _write(self, accounts: list[Account]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "accounts": [asdict(a) for a in accounts]}
        atomic_replace(self.path, json.dumps(payload, ensure_ascii=False, indent=2), mode=0o600)

    def all(self) -> list[Account]:
        return self._read()

    def count(self) -> int:
        return len(self._read())

    def get(self, account_id: str) -> Account | None:
        return next((a for a in self._read() if a.id == account_id), None)

    def by_username(self, name: str) -> Account | None:
        username = (name or "").strip().lower()
        return next((a for a in self._read() if a.username == username), None)

    def by_google(self, sub: str) -> Account | None:
        return next((a for a in self._read() if sub and a.google_sub == sub), None)

    def by_email(self, email: str) -> Account | None:
        email = (email or "").strip().lower()
        return next((a for a in self._read() if email and a.email.lower() == email), None)

    def superadmin(self) -> Account | None:
        return next((a for a in self._read() if a.role == "superadmin"), None)

    def create(
        self,
        username: str,
        password: str | None = None,
        *,
        role: str = "user",
        email: str = "",
        google_sub: str = "",
        display_name: str = "",
        balance: float = 0.0,
        server_home: bool = False,
        first_only: bool = False,
    ) -> Account:
        """A new account. ``first_only``: refused unless there is no account yet (the first visitor)."""
        if first_only and self._read():
            raise AccountError("the server already has its administrator")
        username = normal_username(username)
        if role not in ROLES:
            raise AccountError(f"unknown role {role!r}")
        password_hash = hash_password(password) if password else ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            accounts = self._read()
            if first_only and accounts:
                raise AccountError("the server already has its administrator")
            if role == "superadmin" and any(a.role == "superadmin" for a in accounts):
                raise AccountError("there is one superadmin, and the server has it")
            if any(a.username == username for a in accounts):
                raise AccountError(f"the username {username!r} is taken")
            if email and any(a.email.lower() == email.lower() for a in accounts):
                raise AccountError("an account with that e-mail exists; sign in to it instead")
            account = Account(
                id=secrets.token_hex(6),
                username=username,
                role=role,
                display_name=(display_name or username).strip()[:64],
                email=email.strip(),
                password_hash=password_hash,
                google_sub=google_sub,
                balance=float(balance),
                created_at=time.time(),
                server_home=server_home,
            )
            self._write([*accounts, account])
        return account

    def update(self, account_id: str, change: Callable[[Account], None]) -> Account:
        """Apply ``change`` to one account under the lock and save it."""
        with write_transaction(self.path):
            accounts = self._read()
            account = next((a for a in accounts if a.id == account_id), None)
            if account is None:
                raise AccountError("no such account")
            change(account)
            if account.role not in ROLES:
                raise AccountError(f"unknown role {account.role!r}")
            account.permissions = sorted({p for p in account.permissions if p in PERMISSIONS})
            self._write(accounts)
        return account

    def adjust_balance(self, account_id: str, delta: float) -> float:
        def change(account: Account) -> None:
            account.balance = round(account.balance + float(delta), 6)

        return self.update(account_id, change).balance


# -- sign-in sessions ------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """``sessions.json``: the signed-in browsers, by the hash of their cookie."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_root() / "sessions.json"

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def _write(self, rows: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.path, json.dumps(rows, indent=1), mode=0o600)

    def issue(self, account_id: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            rows = {k: v for k, v in self._read().items() if v.get("expires", 0) > now}
            rows[_token_hash(token)] = {
                "account": account_id,
                "created": now,
                "expires": now + SESSION_TTL_S,
                "seen": now,
            }
            self._write(rows)
        return token

    def resolve(self, token: str | None, *, now: float | None = None) -> str | None:
        """The account a cookie signs in, or None for a missing, unknown or expired one."""
        if not token:
            return None
        now = time.time() if now is None else now
        key = _token_hash(token)
        row = self._read().get(key)
        if row is None or row.get("expires", 0) <= now:
            return None
        if now - float(row.get("seen", 0)) > _SEEN_EVERY_S:
            with write_transaction(self.path):
                rows = self._read()
                if key in rows:
                    rows[key]["seen"] = now
                    self._write(rows)
        return str(row.get("account") or "") or None

    def revoke(self, token: str | None) -> None:
        if not token or not self.path.exists():
            return
        with write_transaction(self.path):
            rows = self._read()
            if rows.pop(_token_hash(token), None) is not None:
                self._write(rows)

    def revoke_account(self, account_id: str) -> int:
        if not self.path.exists():
            return 0
        with write_transaction(self.path):
            rows = self._read()
            kept = {k: v for k, v in rows.items() if v.get("account") != account_id}
            if len(kept) != len(rows):
                self._write(kept)
        return len(rows) - len(kept)


# -- the audit log ---------------------------------------------------------------------------


class AuditLog:
    """``audit.jsonl``: who did what to which account, append-only."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_root() / "audit.jsonl"

    def record(self, actor: str, action: str, target: str = "", **detail: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), "actor": actor, "action": action, "target": target, **detail}
        locked_append(self.path, [json.dumps(row, ensure_ascii=False, default=str)])

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows


# -- sign-in throttling ----------------------------------------------------------------------


class Throttle:
    """Failed sign-ins, per address and per username, in a sliding window; in memory.

    Five failures for one username or twenty from one address within fifteen
    minutes close sign-in for that key until the oldest failure ages out.
    """

    def __init__(self, *, per_name: int = 5, per_address: int = 20, window_s: float = 900.0) -> None:
        self.per_name, self.per_address, self.window_s = per_name, per_address, window_s
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        seen = self._failures.setdefault(key, deque())
        while seen and now - seen[0] > self.window_s:
            seen.popleft()
        return seen

    def blocked(self, username: str, address: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (
            len(self._recent(f"u:{username}", now)) >= self.per_name
            or len(self._recent(f"a:{address}", now)) >= self.per_address
        )

    def failed(self, username: str, address: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._recent(f"u:{username}", now).append(now)
        self._recent(f"a:{address}", now).append(now)

    def succeeded(self, username: str) -> None:
        self._failures.pop(f"u:{username}", None)


__all__ = [
    "MIN_PASSWORD",
    "PERMISSIONS",
    "ROLES",
    "Account",
    "AccountError",
    "AccountStore",
    "AuditLog",
    "SessionStore",
    "Throttle",
    "check_password",
    "hash_password",
    "normal_username",
    "verify_password",
]

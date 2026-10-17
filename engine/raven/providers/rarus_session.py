"""The Rarus sign-in: the agent key the server hands out, and where it is kept.

Rarus reaches its models through the alembox.com agent API (spec section 18).
Signing in through the browser ends with one secret, an ``sk-ab-...`` agent key,
and this module is the only place that stores it. On Windows the key is sealed
with DPAPI, so the file is useless on another account or machine; elsewhere
(development and tests) it is kept as plain text under an owner-only directory.
``RARUS_SECRET_STORE=plain`` forces the plain form.

Nothing here talks to the network. ``rarus_login`` runs the sign-in and
``rarus_client`` asks the server who the key belongs to.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from raven.config.paths import get_oauth_dir
from raven.utils.atomic_io import atomic_replace

#: The site root; the agent API lives under ``/api/agent``. Overridable for a
#: staging server and for the tests, which run against a local stand-in.
SITE_ENV = "RARUS_SITE"
DEFAULT_SITE = "https://alembox.com"
API_PATH = "/api/agent/v1"

STORE_ENV = "RARUS_SECRET_STORE"

_SEALED = "dpapi:"
_PLAIN = "plain:"


@dataclass(frozen=True)
class Session:
    key: str
    email: str = ""
    label: str = ""
    saved_at: float = 0.0


def site_root() -> str:
    """The site the sign-in page and the API live on, without a trailing slash."""
    return (os.environ.get(SITE_ENV, "").strip() or DEFAULT_SITE).rstrip("/")


def site_from_api_base(api_base: str | None) -> str:
    """The site an ``api_base`` such as ``https://alembox.com/api/agent/v1`` belongs to."""
    base = (api_base or "").strip().rstrip("/")
    if base.endswith(API_PATH):
        return base[: -len(API_PATH)].rstrip("/") or site_root()
    return site_root() if not base else base


def default_api_base() -> str:
    return site_root() + API_PATH


def token_path() -> Path:
    return get_oauth_dir() / "rarus.json"


def _use_dpapi() -> bool:
    return sys.platform == "win32" and os.environ.get(STORE_ENV, "").strip().lower() != "plain"


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "Rarus AI", None, None, None, 0, ctypes.byref(target))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
    if not ok:
        raise OSError(ctypes.GetLastError(), "DPAPI refused the data")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def seal(secret: str) -> str:
    raw = secret.encode("utf-8")
    if _use_dpapi():
        return _SEALED + base64.b64encode(_dpapi(raw, protect=True)).decode("ascii")
    return _PLAIN + base64.b64encode(raw).decode("ascii")


def unseal(stored: str) -> str | None:
    """The secret behind ``stored``, or None when it cannot be opened here."""
    try:
        if stored.startswith(_SEALED):
            if sys.platform != "win32":
                return None
            return _dpapi(base64.b64decode(stored[len(_SEALED) :]), protect=False).decode("utf-8")
        if stored.startswith(_PLAIN):
            return base64.b64decode(stored[len(_PLAIN) :]).decode("utf-8")
    except (OSError, ValueError):
        return None
    return None


def save_session(key: str, *, email: str = "", label: str = "") -> Session:
    session = Session(key=key, email=email, label=label, saved_at=time.time())
    body = {
        "version": 1,
        "key": seal(key),
        "email": email,
        "label": label,
        "saved_at": session.saved_at,
    }
    path = token_path()
    atomic_replace(path, json.dumps(body, indent=2), mode=0o600)
    return session


def load_session() -> Session | None:
    try:
        raw = json.loads(token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
        return None
    key = unseal(raw["key"])
    if not key:
        return None
    return Session(
        key=key,
        email=str(raw.get("email") or ""),
        label=str(raw.get("label") or ""),
        saved_at=float(raw.get("saved_at") or 0.0),
    )


def load_key() -> str | None:
    session = load_session()
    return session.key if session else None


def delete_session() -> None:
    """Forget the key on this computer. The server-side key is revoked on the site."""
    token_path().unlink(missing_ok=True)


def describe(session: Session) -> dict[str, object]:
    """The part of a session that is safe to show: never the key itself."""
    out = asdict(session)
    out.pop("key")
    out["key_prefix"] = session.key[:12]
    return out


__all__ = [
    "API_PATH",
    "DEFAULT_SITE",
    "SITE_ENV",
    "Session",
    "default_api_base",
    "delete_session",
    "describe",
    "load_key",
    "load_session",
    "save_session",
    "seal",
    "site_from_api_base",
    "site_root",
    "token_path",
    "unseal",
]

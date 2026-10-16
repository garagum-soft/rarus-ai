"""The account gateway's own settings, which the administrators edit in its admin page.

Kept in ``accounts/settings.json`` (mode 0600, since it holds the Google client
secret). A page is never sent the secret, only whether one is set.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from raven.accounts.paths import accounts_root
from raven.utils.atomic_io import atomic_replace, write_transaction


@dataclass
class HubSettings:
    public_url: str = ""
    """The address people open, e.g. ``https://raven.example.com`` -- Google Sign-In
    redirects back to ``<public_url>/_hub/google/callback``, so it needs one (Google
    accepts ``http://localhost`` and https addresses only)."""
    registration_open: bool = True
    google_client_id: str = ""
    google_client_secret: str = ""
    disk_quota_gb: float = 50.0
    """Per account; administrators have none."""
    credits_per_usd: float = 100.0
    """Credits charged per US dollar of model price."""
    own_access_share: float = 0.1
    """The share of the price charged for a call made on the account's own key or subscription."""
    lent_access_share: float = 1.0
    """The share charged for a call made on access the server lends the account."""
    new_account_credits: float = 10000.0
    """The welcome bonus: credits every new account starts with, however it was made; 0 gives none."""
    engine_port_base: int = 18900
    """The first port handed to an account's engine; each engine takes two."""

    @property
    def google_ready(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.public_url)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("google_client_secret", None)
        data["google_client_secret_set"] = bool(self.google_client_secret)
        data["google_ready"] = self.google_ready
        return data


_FIELDS = {f.name: f for f in fields(HubSettings)}


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_root() / "settings.json"

    def load(self) -> HubSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return HubSettings(**{k: v for k, v in raw.items() if k in _FIELDS}) if isinstance(raw, dict) else HubSettings()

    def save(self, settings: HubSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.path, json.dumps(asdict(settings), ensure_ascii=False, indent=2), mode=0o600)

    def update(self, changes: dict[str, Any]) -> HubSettings:
        """Apply the known keys of ``changes``, typed like the field they set; unknown keys are refused."""
        unknown = sorted(set(changes) - set(_FIELDS))
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            current = asdict(self.load())
            for key, value in changes.items():
                default = getattr(HubSettings, key, None)
                current[key] = _typed(key, value, default)
            settings = HubSettings(**current)
            if settings.public_url and not settings.public_url.startswith(("http://", "https://")):
                raise ValueError("the public address starts with https:// (or http://localhost)")
            settings.public_url = settings.public_url.rstrip("/")
            if settings.disk_quota_gb <= 0 or settings.credits_per_usd <= 0:
                raise ValueError("the disk allowance and the credit rate are positive numbers")
            if not 0 <= settings.own_access_share <= 1 or not 0 <= settings.lent_access_share <= 10:
                raise ValueError("a share is a fraction of the price, 0 to 1 (up to 10 for lent access)")
            self.save(settings)
        return settings


def _typed(key: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return "" if value is None else str(value).strip()


__all__ = ["HubSettings", "SettingsStore"]

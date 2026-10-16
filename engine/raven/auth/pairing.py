"""Pairing a remote device with this Raven: a one-time ticket traded for a device token.

The owner asks for a ticket on the machine Raven runs on (``raven remote pair``).
The ticket is short-lived and good once: a device that presents it in time gets
a token of its own, and from then on reaches the gateway with that token in
``X-Raven-Device-Token`` -- through whatever tunnel or LAN route carries it.
A wrong, expired or already used ticket gets nothing. Only hashes are kept, so
the file never holds a credential that works, and the owner can see the paired
devices and revoke any of them.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from raven.utils.atomic_io import atomic_replace

TICKET_TTL_S = 300
_FILE_MODE = 0o600


class PairingError(PermissionError):
    """A ticket that does not pair: wrong, expired or used."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class Device:
    name: str
    token_hash: str
    paired_at: float
    last_seen: float = 0.0


class PairingStore:
    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"tickets": {}, "devices": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.path, json.dumps(data, indent=2), mode=_FILE_MODE)

    def issue_ticket(self, ttl_s: int = TICKET_TTL_S) -> tuple[str, int]:
        """A new one-time ticket and how many seconds it is good for."""
        data = self._read()
        now = self._clock()
        data["tickets"] = {h: exp for h, exp in data["tickets"].items() if exp > now}
        ticket = secrets.token_urlsafe(16)
        data["tickets"][_hash(ticket)] = now + ttl_s
        self._write(data)
        return ticket, ttl_s

    def redeem(self, ticket: str, device: str) -> str:
        """Trade a ticket for a device token; the ticket is spent either way it is checked."""
        device = device.strip()
        if not device:
            raise PairingError("name the device")
        data = self._read()
        expires = data["tickets"].pop(_hash(ticket), None)
        self._write(data)
        if expires is None:
            raise PairingError("this ticket is wrong or was already used")
        if expires <= self._clock():
            raise PairingError("this ticket expired; ask for a new one with `raven remote pair`")
        if any(d["name"] == device for d in data["devices"]):
            raise PairingError(f"a device named {device!r} is already paired; revoke it first or pick another name")
        token = secrets.token_urlsafe(32)
        data["devices"].append(asdict(Device(name=device, token_hash=_hash(token), paired_at=self._clock())))
        self._write(data)
        return token

    def device_for(self, token: str | None) -> str | None:
        """The paired device a token belongs to, or ``None``."""
        if not token:
            return None
        wanted = _hash(token)
        data = self._read()
        for row in data["devices"]:
            if secrets.compare_digest(row["token_hash"], wanted):
                return str(row["name"])
        return None

    def devices(self) -> list[Device]:
        return [Device(**row) for row in self._read()["devices"]]

    def revoke(self, device: str) -> bool:
        data = self._read()
        kept = [row for row in data["devices"] if row["name"] != device]
        if len(kept) == len(data["devices"]):
            return False
        data["devices"] = kept
        self._write(data)
        return True


def default_store() -> PairingStore:
    from raven.home import raven_home

    return PairingStore(raven_home() / "remote" / "pairing.json")


__all__ = ["TICKET_TTL_S", "Device", "PairingError", "PairingStore", "default_store"]

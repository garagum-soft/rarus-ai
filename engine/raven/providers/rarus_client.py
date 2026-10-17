"""The two read-only calls Rarus makes to the alembox.com agent API besides chat.

``whoami`` says whose key it is and how many credits are left; ``models`` lists
what the server will serve. Both take the key as a bearer token and answer with
a :class:`RarusError` whose ``code`` the interface can word without parsing text.
"""

from __future__ import annotations

from typing import Any

import httpx

from raven.providers.rarus_session import API_PATH, site_root

_TIMEOUT_S = 15.0


class RarusError(Exception):
    """The server or the network refused a call; ``code`` names why."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


#: HTTP statuses the agent API uses on purpose, and what each means to a person.
_STATUS_CODES = {
    401: ("unauthorized", "the key was revoked or is not valid; sign in again"),
    402: ("no_credits", "no credits left on the account"),
    403: ("blocked", "the account is blocked"),
}


def _base(site: str | None) -> str:
    return (site or site_root()).rstrip("/") + API_PATH


def _get(path: str, key: str, *, site: str | None, client: httpx.Client | None) -> Any:
    url = _base(site) + path
    try:
        if client is not None:
            response = client.get(url, headers={"Authorization": f"Bearer {key}"})
        else:
            with httpx.Client(timeout=_TIMEOUT_S) as owned:
                response = owned.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise RarusError("network", f"could not reach the server: {exc}") from exc
    if response.status_code in _STATUS_CODES:
        code, text = _STATUS_CODES[response.status_code]
        raise RarusError(code, text, status=response.status_code)
    if response.status_code >= 400:
        raise RarusError("server", f"the server answered {response.status_code}", status=response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise RarusError("server", "the server answered with something that is not JSON") from exc


def whoami(key: str, *, site: str | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    """``{email, balance, key_prefix, ...}`` for the key; the balance is in credits."""
    data = _get("/whoami", key, site=site, client=client)
    if not isinstance(data, dict):
        raise RarusError("server", "unexpected answer from the account endpoint")
    return data


def models(key: str, *, site: str | None = None, client: httpx.Client | None = None) -> list[str]:
    """The model ids the server serves for this key."""
    data = _get("/models", key, site=site, client=client)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RarusError("server", "unexpected answer from the model list")
    return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]


__all__ = ["RarusError", "models", "whoami"]

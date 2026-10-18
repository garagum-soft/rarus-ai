"""Update client (spec 7.8 / 18.5): ask a manifest, fetch the installer, check it, keep a way back.

The manifest lives on the owner's site (``DEFAULT_MANIFEST_URL``); ``RARUS_UPDATE_URL`` replaces
the address and an empty value turns checking off. The site is members-only, so the signed-in
person's account key rides along, and only to the hosts in ``ALLOWED_HOSTS``. A manifest looks
like::

    {"version": "0.2.0", "url": "https://sanlykatip.com/api/desktop/rarus/download",
     "sha256": "<64 hex>", "notes": "..."}

The installer address must be https and on an allowed host (or the manifest's own host). An
installer without a matching SHA-256 is never handed on. The previous installer stays in
``updates/previous`` so a bad release can be rolled back by running it again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from rarus_app import __version__
from rarus_app.paths import app_home

URL_ENV = "RARUS_UPDATE_URL"
DEFAULT_MANIFEST_URL = "https://sanlykatip.com/api/desktop/rarus/latest.json"
ALLOWED_HOSTS = frozenset(
    {
        "sanlykatip.com",
        "www.sanlykatip.com",
        "alembox.com",
        "www.alembox.com",
        "rarus-ai.com",
        "www.rarus-ai.com",
    }
)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_MAX_INSTALLER_BYTES = 1_500_000_000


class UpdateError(RuntimeError):
    """The update could not be checked or fetched; the message says why."""


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str
    notes: str = ""


def manifest_url() -> str:
    """The configured address; an empty ``RARUS_UPDATE_URL`` means "do not check"."""
    configured = os.environ.get(URL_ENV)
    return DEFAULT_MANIFEST_URL if configured is None else configured.strip()


def _headers(address: str) -> dict[str, str]:
    """The account key for the site's own hosts, nothing for any other address."""
    if urlparse(address).hostname not in ALLOWED_HOSTS:
        return {}
    from raven.providers.rarus_session import load_key

    key = load_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def version_key(value: str) -> tuple[int, ...]:
    """``1.2.3`` / ``v1.2.3`` / ``1.2.3rc1`` as ``(1, 2, 3)``; raises ValueError if unreadable."""
    parts = []
    for piece in value.strip().lstrip("vV").split(".")[:3]:
        digits = re.match(r"\d+", piece)
        if digits is None:
            raise ValueError(f"not a version: {value!r}")
        parts.append(int(digits.group()))
    if len(parts) != 3:
        raise ValueError(f"not a version: {value!r}")
    return tuple(parts)


def parse_manifest(data: object, *, manifest_host: str = "") -> Release:
    if not isinstance(data, dict):
        raise UpdateError("the update manifest is not an object")
    version, url, digest = (str(data.get(k) or "") for k in ("version", "url", "sha256"))
    try:
        version_key(version)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    if urlparse(url).scheme != "https":
        raise UpdateError("the installer address must be https")
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS and host != manifest_host:
        raise UpdateError(f"the installer is on a host the program does not trust: {host or url}")
    if not _SHA256.fullmatch(digest):
        raise UpdateError("the manifest has no valid sha256: refusing to offer an unverified installer")
    return Release(version=version, url=url, sha256=digest.lower(), notes=str(data.get("notes") or ""))


def check(*, current: str = __version__, url: str | None = None, client: httpx.Client | None = None) -> Release | None:
    """The newer release, or None when there is none or no manifest address is configured."""
    address = url if url is not None else manifest_url()
    if not address:
        return None
    headers = _headers(address)
    if urlparse(address).hostname in ALLOWED_HOSTS and not headers:
        return None  # the site is members-only: nobody is signed in, so there is nobody to ask for
    try:
        reply = (client or httpx).get(address, headers=headers, timeout=10.0)
        reply.raise_for_status()
        data = reply.json()
        if isinstance(data, dict) and data.get("available") is False:
            return None  # the site has nothing published (yet)
        release = parse_manifest(data, manifest_host=urlparse(address).hostname or "")
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not reach the update server: {exc}") from exc
    except ValueError as exc:
        raise UpdateError("the update manifest is not valid JSON") from exc
    return release if version_key(release.version) > version_key(current) else None


def updates_dir() -> Path:
    path = app_home() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(release: Release, *, client: httpx.Client | None = None, allow_http: bool = False) -> Path:
    """Fetch the installer next to the old ones; delete it and raise unless its SHA-256 matches."""
    if urlparse(release.url).scheme != "https" and not allow_http:
        raise UpdateError("the installer address must be https")
    target = updates_dir() / f"RarusAI-{release.version}-setup.exe"
    partial = target.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with (client or httpx).stream(
            "GET", release.url, headers=_headers(release.url), timeout=60.0, follow_redirects=True
        ) as reply:
            reply.raise_for_status()
            with partial.open("wb") as out:
                for chunk in reply.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > _MAX_INSTALLER_BYTES:
                        raise UpdateError("the installer is larger than expected")
                    digest.update(chunk)
                    out.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"could not download the installer: {exc}") from exc
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != release.sha256:
        partial.unlink(missing_ok=True)
        raise UpdateError("the installer does not match its sha256: it was deleted")
    partial.replace(target)
    return target


def remember_current(installer: Path, version: str) -> Path:
    """Keep the installer of the version being replaced, so ``rollback`` has something to run."""
    previous = updates_dir() / "previous"
    previous.mkdir(exist_ok=True)
    for old in previous.glob("*.exe"):
        old.unlink()
    kept = previous / f"RarusAI-{version}-setup.exe"
    shutil.copy2(installer, kept)
    (previous / "version.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return kept


def rollback_installer() -> Path | None:
    """The installer of the version before the last update, if one was kept."""
    found = sorted((updates_dir() / "previous").glob("*.exe"))
    return found[0] if found else None

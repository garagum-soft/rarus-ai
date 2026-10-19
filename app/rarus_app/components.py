"""Components: what Rarus AI downloads from its own server after it is installed.

Owner, 2026-10-19: like Alem AI's first start -- a list of what will be downloaded, with switches;
some cannot be switched off, some are on by default and can be, and the parts that belong to the
program itself are fetched in the background without being shown. Everything comes from our server.

The list ships with the program (``components.json`` beside this module, written by
packaging/components/build.py). Each entry names one zip with its size and SHA-256, and a group:

* ``hidden``   -- part of the program: always installed, in the background, never listed;
* ``locked``   -- listed with its switch on and fixed;
* ``optional`` -- listed, on unless the person switches it off;
* ``off``      -- listed, off unless the person switches it on.

A component that ``requires`` another brings it along. The zips come from the site's open mirror
(no sign-in: the list is shown before signing in), only over https from the program's own hosts;
a broken download resumes where it stopped, and nothing is unpacked unless its SHA-256 matches the
list. A component lands in ``<home>/components/<id>/<sha12>/``, and rarus_app/component_paths.py
puts it on the path of every process the program starts from then on -- so a new component takes
effect when the engine next starts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from rarus_app.component_paths import MARK_FILE, STATE_FILE, components_dir, read_state
from rarus_app.updater import ALLOWED_HOSTS

CATALOG_FILE = "components.json"
#: Replaces the server address the list names (a staging mirror, the tests).
URL_ENV = "RARUS_COMPONENTS_URL"
GROUPS = ("hidden", "locked", "optional", "off")
DOWNLOADS = ".downloads"
_ATTEMPTS = 5


class ComponentError(RuntimeError):
    """A component could not be fetched or installed; the message says why."""


@dataclass(frozen=True)
class Component:
    id: str
    setup: str
    file: str
    sha256: str
    size: int
    unpacked: int = 0
    requires: tuple[str, ...] = ()
    title: dict[str, str] = field(default_factory=dict)
    about: dict[str, str] = field(default_factory=dict)

    @property
    def folder(self) -> str:
        return f"{self.id}/{self.sha256[:12]}"

    def text(self, lang: str) -> dict[str, str]:
        pick = lambda names: names.get(lang) or names.get("en") or names.get("ru") or self.id  # noqa: E731
        return {"title": pick(self.title), "about": pick(self.about) if self.about else ""}


@dataclass(frozen=True)
class Catalogue:
    base_url: str
    components: tuple[Component, ...]

    def get(self, cid: str) -> Component | None:
        return next((c for c in self.components if c.id == cid), None)


def _parse(data: Any) -> Catalogue:
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        return Catalogue("", ())
    found = []
    for raw in data["components"]:
        try:
            item = Component(
                id=str(raw["id"]),
                setup=str(raw["setup"]),
                file=str(raw["file"]),
                sha256=str(raw["sha256"]).lower(),
                size=int(raw["size"]),
                unpacked=int(raw.get("unpacked") or 0),
                requires=tuple(str(r) for r in raw.get("requires") or ()),
                title=dict(raw.get("title") or {}),
                about=dict(raw.get("about") or {}),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if item.setup in GROUPS and len(item.sha256) == 64 and "/" not in item.file and ".." not in item.file:
            found.append(item)
    return Catalogue(str(data.get("base_url") or ""), tuple(found))


def load_catalogue(path: Path | None = None) -> Catalogue:
    """The list the program shipped with; empty when it has none (a checkout that was never built)."""
    try:
        source = path or Path(str(resources.files("rarus_app").joinpath(CATALOG_FILE)))
        return _parse(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Catalogue("", ())


# -- what is wanted and what is there ----------------------------------------------------------


def write_state(state: dict[str, Any], root: Path | None = None) -> None:
    root = root or components_dir()
    root.mkdir(parents=True, exist_ok=True)
    temp = root / (STATE_FILE + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(root / STATE_FILE)


def wanted(catalogue: Catalogue, choices: dict[str, bool]) -> list[Component]:
    """What should be installed: hidden and locked always, the rest as chosen, plus what they require."""
    ids = set()
    for item in catalogue.components:
        default = item.setup in ("hidden", "locked", "optional")
        on = default if item.setup in ("hidden", "locked") else bool(choices.get(item.id, default))
        if on:
            ids.add(item.id)
    grew = True
    while grew:
        grew = False
        for item in catalogue.components:
            if item.id in ids:
                for need in item.requires:
                    if need not in ids and catalogue.get(need) is not None:
                        ids.add(need)
                        grew = True
    return [c for c in catalogue.components if c.id in ids]


def is_installed(item: Component, state: dict[str, Any], root: Path | None = None) -> bool:
    entry = (state.get("components") or {}).get(item.id)
    if not isinstance(entry, dict) or entry.get("sha256") != item.sha256:
        return False
    return ((root or components_dir()) / item.folder / MARK_FILE).is_file()


def base_url(catalogue: Catalogue) -> str:
    configured = os.environ.get(URL_ENV, "").strip()
    url = (configured or catalogue.base_url).rstrip("/") + "/"
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        if not (configured and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
            raise ComponentError(f"components are only fetched over https from the program's own site, not {url}")
    return url


# -- fetching and unpacking one ----------------------------------------------------------------


Progress = Callable[[int, int], None]


def download(
    item: Component,
    url: str,
    *,
    root: Path | None = None,
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    attempts: int = _ATTEMPTS,
    pause: Callable[[float], None] = time.sleep,
) -> Path:
    """Fetch one zip into ``.downloads``, resuming a part already there; raises unless its SHA-256 matches."""
    folder = (root or components_dir()) / DOWNLOADS
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / item.file
    if target.is_file() and _sha256(target) == item.sha256:
        return target
    partial = folder / (item.file + ".part")
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=False)
    try:
        last_error = ""
        for attempt in range(attempts):
            if cancelled():
                raise ComponentError("cancelled")
            have = partial.stat().st_size if partial.is_file() else 0
            if have > item.size:
                partial.unlink()
                have = 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with client.stream("GET", url + item.file, headers=headers) as reply:
                    if reply.status_code == 416 and have == item.size:
                        break
                    if reply.status_code not in (200, 206):
                        raise ComponentError(f"the server answered {reply.status_code} for {item.file}")
                    if reply.status_code == 200 and have:
                        have = 0  # the server ignored the range: start over
                    with partial.open("ab" if have else "wb") as out:
                        # Chunks as they arrive, so a broken connection keeps what came.
                        for chunk in reply.iter_bytes():
                            if cancelled():
                                raise ComponentError("cancelled")
                            have += len(chunk)
                            if have > item.size:
                                raise ComponentError(f"{item.file} is larger than the list says")
                            out.write(chunk)
                            if progress is not None:
                                progress(have, item.size)
                break
            except httpx.HTTPError as exc:
                last_error = str(exc) or type(exc).__name__
                pause(min(2**attempt, 20))
        else:
            raise ComponentError(f"could not download {item.file}: {last_error}")
    finally:
        if own:
            client.close()
    if _sha256(partial) != item.sha256:
        partial.unlink(missing_ok=True)
        raise ComponentError(f"{item.file} does not match its SHA-256: it was deleted")
    partial.replace(target)
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(item: Component, archive: Path, *, root: Path | None = None) -> Path:
    """Unpack a verified zip into its own folder, refusing any name that would land outside it."""
    root = root or components_dir()
    final = root / item.folder
    temp = root / item.id / f".{item.sha256[:12]}.tmp"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    base = temp.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            path = (temp / member.filename).resolve()
            if member.filename.startswith(("/", "\\")) or (path != base and base not in path.parents):
                shutil.rmtree(temp, ignore_errors=True)
                raise ComponentError(f"{item.file} holds a path outside its folder: {member.filename}")
        zipped.extractall(temp)
    if not (temp / MARK_FILE).is_file():
        shutil.rmtree(temp, ignore_errors=True)
        raise ComponentError(f"{item.file} is not a component (no {MARK_FILE})")
    shutil.rmtree(final, ignore_errors=True)
    temp.replace(final)
    return final


def sweep(catalogue: Catalogue, root: Path | None = None) -> None:
    """Delete component folders the state no longer names, and downloads nothing needs.

    Run before the engine starts: a folder the running engine has loaded cannot be removed on
    Windows, so an old version is left behind by an update and goes on the next start.
    """
    root = root or components_dir()
    if not root.is_dir():
        return
    state = read_state(root)
    keep = {str(e.get("dir")) for e in (state.get("components") or {}).values() if isinstance(e, dict)}
    for item_dir in root.iterdir():
        if not item_dir.is_dir() or item_dir.name.startswith("."):
            continue
        for version in item_dir.iterdir():
            if not version.name.startswith(".") and f"{item_dir.name}/{version.name}" not in keep:
                shutil.rmtree(version, ignore_errors=True)
    files = {c.file for c in catalogue.components}
    for leftover in (root / DOWNLOADS).glob("*") if (root / DOWNLOADS).is_dir() else ():
        if leftover.name not in files and leftover.name.removesuffix(".part") not in files:
            leftover.unlink(missing_ok=True)


# -- the queue the window watches ---------------------------------------------------------------


class Installer:
    """Downloads and unpacks components one after another on a worker thread.

    ``status()`` is what the start page and the settings page poll. One failure does not stop the
    queue; the failed ones are listed with their reason, and ``start`` again retries just those.
    """

    def __init__(
        self,
        catalogue: Catalogue,
        *,
        root: Path | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self.catalogue = catalogue
        self._root = root
        self._client_factory = client_factory
        self._pause = pause
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._running = False
        self._cancel = False
        self._thread: threading.Thread | None = None
        self.installed_any = False

    @property
    def root(self) -> Path:
        return self._root or components_dir()

    def status(self) -> dict[str, Any]:
        with self._lock:
            items = [dict(i) for i in self._items]
            running = self._running
        total = sum(i["size"] for i in items)
        done = sum(i["size"] if i["state"] == "done" else i["received"] for i in items)
        return {
            "state": "working" if running else ("done" if items else "idle"),
            "items": items,
            "received": done,
            "total": total,
            "failed": [i["id"] for i in items if i["state"] == "failed"],
            "installed_any": self.installed_any,
        }

    def busy(self) -> bool:
        with self._lock:
            return self._running

    def start(self, items: list[Component]) -> dict[str, Any]:
        """Queue these components; while a queue is running, its status is the answer and nothing changes."""
        with self._lock:
            already = self._running
            if not already:
                self._items = [
                    {"id": c.id, "setup": c.setup, "size": c.size, "received": 0, "rate": 0, "state": "queued", "error": ""}
                    for c in items
                ]
                self._running = bool(items)
                self._cancel = False
        if not already and items:
            self._thread = threading.Thread(target=self._work, args=(items,), name="rarus-components", daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self) -> None:
        with self._lock:
            self._cancel = True

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _set(self, cid: str, **fields: Any) -> None:
        with self._lock:
            for item in self._items:
                if item["id"] == cid:
                    item.update(fields)

    def _work(self, items: list[Component]) -> None:
        try:
            url = base_url(self.catalogue)
        except ComponentError as exc:
            for item in items:
                self._set(item.id, state="failed", error=str(exc))
            with self._lock:
                self._running = False
            return
        for item in items:
            if self._cancel:
                self._set(item.id, state="failed", error="cancelled")
                continue
            self._set(item.id, state="downloading")
            started, last = time.monotonic(), [time.monotonic(), 0]

            def progress(received: int, total: int, cid: str = item.id) -> None:
                now = time.monotonic()
                if now - last[0] >= 0.5 or received == total:
                    rate = int((received - last[1]) / max(now - last[0], 1e-3)) if received > last[1] else 0
                    last[0], last[1] = now, received
                    self._set(cid, received=received, rate=rate)

            try:
                client = self._client_factory() if self._client_factory else None
                try:
                    archive = download(
                        item, url, root=self.root, client=client, progress=progress,
                        cancelled=lambda: self._cancel, pause=self._pause,
                    )
                finally:
                    if client is not None:
                        client.close()
                self._set(item.id, state="installing", received=item.size)
                unpack(item, archive, root=self.root)
                self._record(item)
                archive.unlink(missing_ok=True)
                self.installed_any = True
                self._set(item.id, state="done", seconds=round(time.monotonic() - started, 1))
            except (ComponentError, OSError, zipfile.BadZipFile) as exc:
                self._set(item.id, state="failed", error=str(exc) or type(exc).__name__)
        with self._lock:
            self._running = False

    def _record(self, item: Component) -> None:
        state = read_state(self.root)
        entries = state.setdefault("components", {})
        old = entries.get(item.id)
        entries[item.id] = {"dir": item.folder, "sha256": item.sha256, "enabled": True}
        write_state(state, self.root)
        if isinstance(old, dict) and old.get("dir") and old.get("dir") != item.folder:
            shutil.rmtree(self.root / str(old["dir"]), ignore_errors=True)  # may be in use: sweep() retries


def remove(cid: str, root: Path | None = None) -> None:
    """Switch a component off: drop it from the state; its folder goes now, or at the next start."""
    root = root or components_dir()
    state = read_state(root)
    entry = (state.get("components") or {}).pop(cid, None)
    write_state(state, root)
    if isinstance(entry, dict) and entry.get("dir"):
        shutil.rmtree(root / str(entry["dir"]), ignore_errors=True)

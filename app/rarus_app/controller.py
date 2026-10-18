"""What the window asks of the shell. No GUI here, so all of it can be tested."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rarus_app import __version__, account, autostart, updater
from rarus_app.engine import Engine, EngineError
from rarus_app.paths import app_home

# Written when the first-run wizard is finished; its absence means the wizard is still due.
ONBOARDED_MARK = "onboarded"


class Controller:
    """State machine behind the start page: starting -> sign-in (if needed) -> ready."""

    def __init__(self, *, engine_factory: Callable[[], Engine] = Engine, open_browser: bool = True) -> None:
        self._engine_factory = engine_factory
        self._open_browser = open_browser
        self._engine: Engine | None = None
        self._engine_error = ""
        self._engine_ready = threading.Event()
        self._sign_in: account.SignIn | None = None
        self._lock = threading.Lock()
        self._update: dict[str, Any] = {"state": "idle"}
        self._update_release: updater.Release | None = None
        self._update_path: Path | None = None

    # -- engine ----------------------------------------------------------------

    def start_engine(self, *, wait: bool = False) -> None:
        """Start the engine in the background (the window is already on screen)."""

        def work() -> None:
            try:
                account.first_run_language()
                engine = self._engine_factory()
                engine.start()
                with self._lock:
                    self._engine = engine
            except (EngineError, OSError) as exc:
                with self._lock:
                    self._engine_error = str(exc)
            finally:
                self._engine_ready.set()

        thread = threading.Thread(target=work, name="rarus-engine-start", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def stop_engine(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.stop()
        if self._sign_in is not None:
            self._sign_in.cancel()

    def engine_alive(self) -> bool:
        with self._lock:
            return self._engine is not None and self._engine.alive()

    # -- what the start page polls --------------------------------------------

    def boot(self) -> dict[str, Any]:
        """``{"phase": "starting"|"error"|"signin"|"ready", ...}`` for the start page."""
        if not self._engine_ready.is_set():
            return {"phase": "starting"}
        with self._lock:
            engine, error = self._engine, self._engine_error
        if engine is None:
            return {"phase": "error", "error": error or "the engine did not start"}
        current = account.status(refresh=False)
        if not current.signed_in:
            return {"phase": "signin"}
        try:
            return {"phase": "ready", "url": engine.auth_url()}
        except EngineError as exc:
            return {"phase": "error", "error": str(exc)}

    # -- account ---------------------------------------------------------------

    def account(self) -> dict[str, Any]:
        return account.status().as_dict()

    def sign_in_start(self) -> dict[str, Any]:
        if self._sign_in is not None:
            self._sign_in.cancel()
        self._sign_in = account.SignIn(open_browser=self._open_browser)
        return {"url": self._sign_in.start()}

    def sign_in_outcome(self) -> dict[str, Any]:
        outcome = self._sign_in.outcome() if self._sign_in is not None else "cancelled"
        return {"outcome": outcome}

    def sign_in_cancel(self) -> None:
        if self._sign_in is not None:
            self._sign_in.cancel()

    def sign_out(self) -> dict[str, Any]:
        account.sign_out()
        return {"ok": True}

    # -- first run --------------------------------------------------------------

    def first_run(self) -> bool:
        """True until the page's first-run wizard has been finished once."""
        return not (app_home() / ONBOARDED_MARK).exists()

    def onboarded(self) -> None:
        mark = app_home() / ONBOARDED_MARK
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("1", encoding="utf-8")

    # -- settings of the shell itself -------------------------------------------

    def autostart_enabled(self) -> bool:
        return autostart.is_enabled()

    def set_autostart(self, enabled: bool) -> bool:
        autostart.set_enabled(bool(enabled))
        return autostart.is_enabled()

    def update_check(self) -> dict[str, Any]:
        try:
            release = updater.check()
        except updater.UpdateError as exc:
            return {"available": False, "current": __version__, "error": str(exc)}
        if release is None:
            return {"available": False, "current": __version__}
        return {"available": True, "current": __version__, "version": release.version, "notes": release.notes}

    # -- update: fetch in the background, then run the installer ----------------

    def update_start(self) -> dict[str, Any]:
        """Check, then download and verify the newer installer on a worker thread; poll ``update_status``."""
        with self._lock:
            if self._update["state"] in ("checking", "downloading", "ready"):
                return self._update_status_locked()
            self._update = {"state": "checking"}
            self._update_release = None
            self._update_path = None
        threading.Thread(target=self._update_work, name="rarus-update", daemon=True).start()
        return self.update_status()

    def _update_work(self) -> None:
        try:
            release = updater.check()
            if release is None:
                self._set_update(state="current")
                return
            self._set_update(state="downloading", version=release.version, received=0, total=None)
            self._keep_running_installer()

            def progress(received: int, total: int | None) -> None:
                self._set_update(received=received, total=total)

            path = updater.download(release, progress=progress)
        except updater.UpdateError as exc:
            self._set_update(state="error", error=str(exc))
            return
        except OSError as exc:
            self._set_update(state="error", error=str(exc))
            return
        with self._lock:
            self._update_release, self._update_path = release, path
        self._set_update(state="ready")

    def _keep_running_installer(self) -> None:
        """If this version came from an earlier in-app update, keep its installer as the way back."""
        mine = updater.updates_dir() / f"RarusAI-{__version__}-setup.exe"
        try:
            if mine.is_file():
                updater.remember_current(mine, __version__)
        except OSError:
            pass

    def _set_update(self, **fields: Any) -> None:
        with self._lock:
            self._update.update(fields)

    def _update_status_locked(self) -> dict[str, Any]:
        info = self._update
        received, total = info.get("received"), info.get("total")
        percent = int(received * 100 / total) if isinstance(received, int) and total else None
        out: dict[str, Any] = {"state": info["state"], "current": __version__, "percent": percent}
        for key in ("version", "error"):
            if info.get(key):
                out[key] = info[key]
        return out

    def update_status(self) -> dict[str, Any]:
        with self._lock:
            return self._update_status_locked()

    def update_apply(self) -> dict[str, Any]:
        """Run the verified installer; the caller closes the program right after a ``{"ok": True}``."""
        with self._lock:
            ready = self._update["state"] == "ready"
            release, path = self._update_release, self._update_path
        if not ready or release is None or path is None:
            return {"ok": False, "error": "no update is ready"}
        try:
            updater.launch_installer(path, release.sha256)
        except updater.UpdateError as exc:
            self._set_update(state="error", error=str(exc))
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "version": release.version}

"""What the window asks of the shell. No GUI here, so all of it can be tested."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from rarus_app import account, autostart, updater
from rarus_app.engine import Engine, EngineError


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

    # -- engine ----------------------------------------------------------------

    def start_engine(self, *, wait: bool = False) -> None:
        """Start the engine in the background (the window is already on screen)."""

        def work() -> None:
            try:
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
            return {"available": False, "error": str(exc)}
        if release is None:
            return {"available": False}
        return {"available": True, "version": release.version, "notes": release.notes}

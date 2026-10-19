"""The window and the tray: the only module that needs a GUI toolkit.

``pywebview`` shows the page. It is imported here and nowhere else, so everything else in the
shell runs (and is tested) without it. Closing the window hides it to the tray when a tray is
available; "Quit" in the tray menu ends the program and its engine.
"""

from __future__ import annotations

import threading
from importlib import resources
from pathlib import Path
from typing import Any

from rarus_app import APP_NAME
from rarus_app.controller import Controller

#: How long the page has to read the update answer before the program closes under it.
QUIT_DELAY_S = 1.5


class Bridge:
    """The object the start page reaches as ``pywebview.api``: thin, and no secrets cross it."""

    def __init__(self, controller: Controller, quit_app: Any) -> None:
        self._controller = controller
        self._quit = quit_app
        self._window: Any = None

    def attach(self, window: Any) -> None:
        """The window this bridge serves; private, so pywebview does not offer it to the page."""
        self._window = window

    def boot(self) -> dict[str, Any]:
        return self._controller.boot()

    def account(self) -> dict[str, Any]:
        return self._controller.account()

    def sign_in_start(self) -> dict[str, Any]:
        return self._controller.sign_in_start()

    def sign_in_outcome(self) -> dict[str, Any]:
        return self._controller.sign_in_outcome()

    def sign_in_cancel(self) -> None:
        self._controller.sign_in_cancel()

    def sign_out(self) -> dict[str, Any]:
        return self._controller.sign_out()

    def first_run(self) -> bool:
        return self._controller.first_run()

    def onboarded(self) -> None:
        self._controller.onboarded()

    def autostart_enabled(self) -> bool:
        return self._controller.autostart_enabled()

    def set_autostart(self, enabled: bool) -> bool:
        return self._controller.set_autostart(enabled)

    def update_check(self) -> dict[str, Any]:
        return self._controller.update_check()

    def update_start(self) -> dict[str, Any]:
        return self._controller.update_start()

    def update_status(self) -> dict[str, Any]:
        return self._controller.update_status()

    def update_apply(self) -> dict[str, Any]:
        """Run the installer, then leave: the page gets its answer first, the program closes a moment later."""
        result = self._controller.update_apply()
        if result.get("ok"):
            threading.Timer(QUIT_DELAY_S, self._quit).start()
        return result

    def components_view(self, lang: str = "ru") -> dict[str, Any]:
        return self._controller.components_view(str(lang or "ru"))

    def components_install(self, choices: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._controller.components_install(choices if isinstance(choices, dict) else {})

    def components_status(self) -> dict[str, Any]:
        return self._controller.components_status()

    def components_cancel(self) -> None:
        self._controller.components_cancel()

    def components_finish(self) -> dict[str, Any]:
        return self._controller.components_finish()

    def components_restart(self) -> dict[str, Any]:
        """Restart the engine so it loads what was installed, and take the window back to the start page.

        The start page waits for the engine and opens the page again; the engine page cannot do this
        itself, because the engine it is talking to is the one going away.
        """
        self._controller.restart_engine()
        if self._window is not None:
            self._window.load_url(str(start_page()))
        return {"ok": True}

    def quit(self) -> None:
        self._quit()


def start_page() -> Path:
    return Path(str(resources.files("rarus_app").joinpath("web", "start.html")))


def run_window(controller: Controller, *, hidden: bool = False) -> int:
    """Block on the GUI loop until the program quits. Returns the exit code."""
    import webview  # imported here: the shell's tests and ``--engine`` mode never need it

    state: dict[str, Any] = {"quitting": False, "tray": None}

    def quit_app() -> None:
        state["quitting"] = True
        tray = state["tray"]
        if tray is not None:
            tray.stop()
        for window in list(webview.windows):
            window.destroy()

    bridge = Bridge(controller, quit_app)
    window = webview.create_window(
        APP_NAME,
        url=str(start_page()),
        js_api=bridge,
        width=1280,
        height=820,
        min_size=(900, 600),
        hidden=hidden,
        background_color="#0f1115",
    )

    def on_closing() -> bool:
        if state["quitting"] or state["tray"] is None:
            return True
        window.hide()
        return False

    window.events.closing += on_closing
    bridge.attach(window)

    state["tray"] = _make_tray(show=window.show, quit_app=quit_app)
    # On the first start the start page lists the components first and starts the engine itself
    # once they are answered (Controller.components_finish), so the engine loads them right away.
    if not controller.components_pending():
        controller.start_engine()
    webview.start()
    return 0


def _make_tray(*, show: Any, quit_app: Any) -> Any | None:
    """A tray icon with Open / Quit, or None when ``pystray`` or Pillow is missing."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        # The program's globe (tools/dev/make_icons.py); a drawn disc only if the file is missing.
        image = Image.open(Path(__file__).with_name("rarus-icon.png")).convert("RGBA")
    except OSError:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=(212, 161, 14, 255))
    menu = pystray.Menu(
        pystray.MenuItem("Открыть Rarus AI", lambda *_: show(), default=True),
        pystray.MenuItem("Выход", lambda *_: quit_app()),
    )
    icon = pystray.Icon("RarusAI", image, APP_NAME, menu)
    icon.run_detached()
    return icon

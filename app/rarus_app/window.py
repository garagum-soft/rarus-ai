"""The window and the tray: the only module that needs a GUI toolkit.

``pywebview`` shows the page. It is imported here and nowhere else, so everything else in the
shell runs (and is tested) without it. Closing the window hides it to the tray when a tray is
available; "Quit" in the tray menu ends the program and its engine.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from rarus_app import APP_NAME
from rarus_app.controller import Controller


class Bridge:
    """The object the start page reaches as ``pywebview.api``: thin, and no secrets cross it."""

    def __init__(self, controller: Controller, quit_app: Any) -> None:
        self._controller = controller
        self._quit = quit_app

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

    window = webview.create_window(
        APP_NAME,
        url=str(start_page()),
        js_api=Bridge(controller, quit_app),
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

    state["tray"] = _make_tray(show=window.show, quit_app=quit_app)
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
    image = Image.new("RGBA", (64, 64), (15, 17, 21, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, 54, 54), fill=(91, 140, 255, 255))
    menu = pystray.Menu(
        pystray.MenuItem("Открыть Rarus AI", lambda *_: show(), default=True),
        pystray.MenuItem("Выход", lambda *_: quit_app()),
    )
    icon = pystray.Icon("RarusAI", image, APP_NAME, menu)
    icon.run_detached()
    return icon

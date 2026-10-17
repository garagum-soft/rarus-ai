"""Start with Windows (spec 7.3): one value under the current user's ``Run`` key, off by default."""

from __future__ import annotations

import sys
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RarusAI"
FLAG = "--background"


class RunStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class RegistryStore:
    """The current user's Run key, through ``winreg``."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def command_line() -> str:
    """What Windows runs at sign-in: the program, started minimized to the tray."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {FLAG}'
    return f'"{sys.executable}" -m rarus_app {FLAG}'


def is_enabled(store: RunStore | None = None) -> bool:
    return (store or RegistryStore()).get(VALUE_NAME) is not None


def set_enabled(enabled: bool, store: RunStore | None = None) -> None:
    store = store or RegistryStore()
    if enabled:
        store.set(VALUE_NAME, command_line())
    else:
        store.delete(VALUE_NAME)

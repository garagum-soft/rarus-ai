"""One copy at a time, and the start-with-Windows switch."""

from __future__ import annotations

import sys

import pytest

from rarus_app import autostart
from rarus_app.single_instance import AlreadyRunningError, SingleInstance


class TestSingleInstance:
    def test_a_second_copy_is_refused_while_the_first_holds_the_lock(self, tmp_path):
        lock = tmp_path / "app.lock"
        with SingleInstance(lock_file=lock):
            with pytest.raises(AlreadyRunningError):
                SingleInstance(lock_file=lock).acquire()

    def test_the_lock_is_free_again_after_release(self, tmp_path):
        lock = tmp_path / "app.lock"
        with SingleInstance(lock_file=lock):
            pass
        with SingleInstance(lock_file=lock):
            pass

    @pytest.mark.skipif(sys.platform != "win32", reason="named mutexes are a Windows facility")
    def test_on_windows_the_named_mutex_refuses_a_second_holder(self):
        name = "Local\\RarusAITest"
        with SingleInstance(name=name):
            with pytest.raises(AlreadyRunningError):
                SingleInstance(name=name).acquire()
        with SingleInstance(name=name):
            pass


class FakeStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class TestAutostart:
    def test_it_is_off_until_switched_on(self):
        assert not autostart.is_enabled(FakeStore())

    def test_switching_on_writes_a_background_command(self):
        store = FakeStore()
        autostart.set_enabled(True, store)
        assert autostart.is_enabled(store)
        assert store.values[autostart.VALUE_NAME].endswith(autostart.FLAG)

    def test_switching_off_removes_it_and_is_harmless_twice(self):
        store = FakeStore()
        autostart.set_enabled(True, store)
        autostart.set_enabled(False, store)
        autostart.set_enabled(False, store)
        assert not autostart.is_enabled(store)

    @pytest.mark.skipif(sys.platform != "win32", reason="the registry is a Windows facility")
    def test_the_registry_store_round_trips(self, monkeypatch):
        monkeypatch.setattr(autostart, "VALUE_NAME", "RarusAITest")
        store = autostart.RegistryStore()
        try:
            autostart.set_enabled(True, store)
            assert autostart.is_enabled(store)
        finally:
            autostart.set_enabled(False, store)
        assert not autostart.is_enabled(store)

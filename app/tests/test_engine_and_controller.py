"""The engine launcher against a stand-in engine, and the start-page state machine."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from raven.providers import rarus_session

from rarus_app import account
from rarus_app import engine as engine_mod
from rarus_app.controller import Controller

FAKE = str(Path(__file__).with_name("fake_engine.py"))
KEY = "sk-ab-" + "0123456789abcdef" * 3


def _engine(*extra: str, timeout: float = 20.0) -> engine_mod.Engine:
    port = engine_mod.free_port()
    return engine_mod.Engine(command=[sys.executable, FAKE, str(port), *extra], port=port, start_timeout_s=timeout)


class TestEngine:
    def test_it_starts_waits_for_health_and_mints_a_one_time_url(self):
        eng = _engine()
        try:
            state = eng.start()
            assert state.port == eng.port
            assert eng.alive()
            url = eng.auth_url()
            assert url == f"http://127.0.0.1:{eng.port}/auth#nonce-123"
        finally:
            eng.stop()
        assert not eng.alive()

    def test_stop_ends_the_process_and_removes_its_state_file(self):
        eng = _engine()
        eng.start()
        eng.stop()
        assert engine_mod.read_state() is None

    def test_an_engine_that_dies_at_once_is_reported_not_waited_for(self):
        eng = _engine("--die")
        started = time.monotonic()
        with pytest.raises(engine_mod.EngineError, match="exited"):
            eng.start()
        assert time.monotonic() - started < 15

    def test_an_engine_that_never_answers_times_out(self):
        eng = engine_mod.Engine(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            port=engine_mod.free_port(),
            start_timeout_s=1.0,
        )
        with pytest.raises(engine_mod.EngineError, match="in time"):
            eng.start()
        assert not eng.alive()

    def test_a_stale_state_file_is_not_believed(self):
        stale = engine_mod.serve_state_path()
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"port": 1, "token": "old", "pid": 1}')
        eng = _engine()
        try:
            state = eng.start()
            assert state.token != "old"
        finally:
            eng.stop()

    def test_the_auth_url_needs_a_running_engine(self):
        with pytest.raises(engine_mod.EngineError):
            _engine().auth_url()

    def test_the_command_runs_the_engine_through_the_shell_module(self):
        cmd = engine_mod.engine_command(1234, python="py")
        assert cmd == ["py", "-m", "rarus_app", "--engine", "serve", "--port", "1234"]

    def test_the_read_state_ignores_garbage(self, tmp_path):
        bad = tmp_path / "serve.json"
        bad.write_text("{nope")
        assert engine_mod.read_state(bad) is None


class TestController:
    def test_before_the_engine_is_up_the_page_is_told_to_wait(self):
        ctl = Controller(engine_factory=_engine)
        assert ctl.boot() == {"phase": "starting"}

    def test_a_signed_out_person_is_sent_to_sign_in(self):
        ctl = Controller(engine_factory=_engine, open_browser=False)
        try:
            ctl.start_engine(wait=True)
            assert ctl.boot() == {"phase": "signin"}
        finally:
            ctl.stop_engine()

    def test_a_signed_in_person_goes_straight_to_the_page(self):
        rarus_session.save_session(KEY, email="me@example.com")
        ctl = Controller(engine_factory=_engine, open_browser=False)
        try:
            ctl.start_engine(wait=True)
            state = ctl.boot()
            assert state["phase"] == "ready"
            assert state["url"].endswith("/auth#nonce-123")
        finally:
            ctl.stop_engine()

    def test_an_engine_that_fails_is_shown_as_an_error(self):
        ctl = Controller(engine_factory=lambda: _engine("--die"), open_browser=False)
        ctl.start_engine(wait=True)
        state = ctl.boot()
        assert state["phase"] == "error"
        assert "exited" in state["error"]

    def test_signing_out_returns_the_page_to_sign_in(self):
        rarus_session.save_session(KEY)
        ctl = Controller(engine_factory=_engine, open_browser=False)
        try:
            ctl.start_engine(wait=True)
            assert ctl.boot()["phase"] == "ready"
            ctl.sign_out()
            assert ctl.boot() == {"phase": "signin"}
        finally:
            ctl.stop_engine()

    def test_a_sign_in_that_was_never_started_reads_as_cancelled(self):
        assert Controller(open_browser=False).sign_in_outcome() == {"outcome": "cancelled"}


class TestAccount:
    def test_signed_out_status(self):
        assert account.status().as_dict() == {"signed_in": False, "email": "", "balance": None, "error": ""}

    def test_the_balance_comes_from_the_server(self, monkeypatch):
        rarus_session.save_session(KEY, email="old@example.com")
        monkeypatch.setattr(account.rarus_client, "whoami", lambda key, **_: {"email": "me@example.com", "balance": 12})
        status = account.status()
        assert (status.signed_in, status.email, status.balance) == (True, "me@example.com", 12)

    def test_a_revoked_key_signs_the_person_out(self, monkeypatch):
        rarus_session.save_session(KEY)

        def refuse(key, **_):
            raise account.rarus_client.RarusError("unauthorized", "no", status=401)

        monkeypatch.setattr(account.rarus_client, "whoami", refuse)
        status = account.status()
        assert not status.signed_in and status.error == "unauthorized"
        assert rarus_session.load_key() is None

    def test_a_network_failure_keeps_the_sign_in_and_reports_it(self, monkeypatch):
        rarus_session.save_session(KEY, email="me@example.com")

        def down(key, **_):
            raise account.rarus_client.RarusError("network", "down")

        monkeypatch.setattr(account.rarus_client, "whoami", down)
        status = account.status()
        assert status.signed_in and status.error == "network" and status.balance is None

    def test_sign_in_ends_with_done_and_the_default_model_set(self, monkeypatch):
        calls: list[str] = []

        class Attempt:
            def start(self):
                return "https://alembox.com/app-login#x"

            def wait(self):
                rarus_session.save_session(KEY)

            def cancel(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(account.rarus_login, "BrowserLogin", lambda **_: Attempt())
        monkeypatch.setattr(account, "make_default", lambda: calls.append("default"))
        attempt = account.SignIn(open_browser=False)
        attempt.start()
        for _ in range(100):
            if attempt.outcome() is not None:
                break
            time.sleep(0.05)
        assert attempt.outcome() == "done"
        assert calls == ["default"]

    def test_a_failed_sign_in_reports_its_code(self, monkeypatch):
        class Attempt:
            def start(self):
                return "u"

            def wait(self):
                raise account.rarus_login.LoginError("keys", "ten keys")

            def cancel(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(account.rarus_login, "BrowserLogin", lambda **_: Attempt())
        attempt = account.SignIn(open_browser=False)
        attempt.start()
        for _ in range(100):
            if attempt.outcome() is not None:
                break
            time.sleep(0.05)
        assert attempt.outcome() == "keys"


class TestFirstRunLanguage:
    def test_a_fresh_install_starts_in_russian(self):
        import json

        from raven.config.loader import get_config_path

        account.first_run_language()
        assert json.loads(get_config_path().read_text(encoding="utf-8"))["language"] == "ru"

    def test_a_language_already_chosen_is_left_alone(self):
        import json

        from raven.config.loader import get_config_path
        from raven.config.update import set_language

        set_language("en")
        account.first_run_language()
        assert json.loads(get_config_path().read_text(encoding="utf-8"))["language"] == "en"


class TestThePort:
    def test_the_same_port_is_chosen_while_it_is_free(self):
        assert engine_mod.choose_port() == engine_mod.PREFERRED_PORT

    def test_another_port_is_chosen_when_it_is_taken(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", engine_mod.PREFERRED_PORT))
            holder.listen()
            port = engine_mod.choose_port()
        assert port != engine_mod.PREFERRED_PORT


class TestFirstRun:
    def test_the_wizard_is_due_until_it_has_been_finished(self):
        ctl = Controller(engine_factory=_engine, open_browser=False)
        assert ctl.first_run() is True
        ctl.onboarded()
        assert ctl.first_run() is False

    def test_the_note_survives_a_new_controller(self):
        Controller(engine_factory=_engine, open_browser=False).onboarded()
        assert Controller(engine_factory=_engine, open_browser=False).first_run() is False

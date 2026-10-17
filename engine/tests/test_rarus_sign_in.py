"""The Rarus sign-in: key storage, the browser flow against a stand-in server, the account calls."""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from raven.providers import rarus_client, rarus_login, rarus_session

KEY = "sk-ab-" + "0123456789abcdef" * 3


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """A home of the test's own: on Windows ``Path.home()`` ignores the suite's ``HOME``."""
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "rarus-home"))
    monkeypatch.setenv(rarus_session.STORE_ENV, "plain")


class FakeSite:
    """A stand-in for the alembox.com endpoints the sign-in uses."""

    def __init__(self) -> None:
        self.claims: list[dict] = []
        self.claim_status = 200
        self.balance = 42
        self.whoami_status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/agent/app-code/claim":
                    outer.claims.append(body)
                    if outer.claim_status != 200:
                        self._json(outer.claim_status, {"detail": "no"})
                    else:
                        self._json(200, {"key": KEY})
                else:
                    self._json(404, {})

            def do_GET(self) -> None:  # noqa: N802
                auth = self.headers.get("Authorization", "")
                if self.path == "/api/agent/v1/whoami":
                    if outer.whoami_status != 200:
                        self._json(outer.whoami_status, {"detail": "no"})
                    else:
                        self._json(200, {"email": "me@example.com", "balance": outer.balance, "key_prefix": KEY[:12]})
                elif self.path == "/api/agent/v1/models":
                    if auth != f"Bearer {KEY}":
                        self._json(401, {"detail": "invalid agent key"})
                    else:
                        self._json(200, {"object": "list", "data": [{"id": "deepseek-flash"}, {"id": "auto"}]})
                else:
                    self._json(404, {})

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    fake = FakeSite()
    yield fake
    fake.close()


def _come_back(url: str, *, state: str | None = None, code: str = "one-time-code") -> httpx.Response:
    """Do what the sign-in page does: send the browser to the program's loopback address."""
    fragment = parse_qs(urlparse(url).fragment)
    port = fragment["port"][0]
    chosen = state if state is not None else fragment["state"][0]
    return httpx.get(
        f"http://127.0.0.1:{port}{rarus_login.CALLBACK_PATH}",
        params={"code": code, "state": chosen},
        follow_redirects=False,
    )


class TestTheKeyStore:
    def test_a_key_round_trips_and_is_not_written_in_clear(self):
        rarus_session.save_session(KEY, email="me@example.com")

        stored = rarus_session.token_path().read_text(encoding="utf-8")
        assert KEY not in stored
        assert rarus_session.load_key() == KEY
        assert rarus_session.load_session().email == "me@example.com"

    def test_nothing_is_signed_in_until_a_key_is_saved(self):
        assert rarus_session.load_key() is None

    def test_deleting_the_session_signs_out(self):
        rarus_session.save_session(KEY)
        rarus_session.delete_session()
        assert rarus_session.load_key() is None

    def test_a_damaged_file_reads_as_signed_out(self):
        rarus_session.token_path().write_text("{not json", encoding="utf-8")
        assert rarus_session.load_key() is None

    def test_the_description_never_carries_the_key(self):
        shown = rarus_session.describe(rarus_session.save_session(KEY, email="a@b"))
        assert KEY not in json.dumps(shown)
        assert shown["key_prefix"] == KEY[:12]

    @pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows facility")
    def test_on_windows_the_key_is_sealed_with_dpapi(self, monkeypatch):
        monkeypatch.delenv(rarus_session.STORE_ENV)
        sealed = rarus_session.seal(KEY)
        assert sealed.startswith("dpapi:")
        assert KEY not in sealed
        assert rarus_session.unseal(sealed) == KEY

    def test_the_site_is_found_from_the_api_address(self):
        assert rarus_session.site_from_api_base("https://alembox.com/api/agent/v1/") == "https://alembox.com"
        assert rarus_session.site_from_api_base(None) == rarus_session.site_root()


class TestThePkceValues:
    def test_a_challenge_is_43_url_safe_characters(self):
        challenge = rarus_login.challenge_of(rarus_login.new_verifier())
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)

    def test_a_verifier_is_in_the_range_the_server_accepts(self):
        verifier = rarus_login.new_verifier()
        assert re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier)

    def test_the_challenge_is_the_rfc_example(self):
        # RFC 7636 appendix B
        assert (
            rarus_login.challenge_of("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
            == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )


class TestTheBrowserSignIn:
    def test_the_page_address_carries_what_the_page_validates(self, site):
        attempt = rarus_login.BrowserLogin(site=site.url)
        url = attempt.start()
        attempt.cancel()
        attempt.close()

        fragment = parse_qs(urlparse(url).fragment)
        assert urlparse(url).path == "/app-login"
        assert 1024 <= int(fragment["port"][0]) <= 65535
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", fragment["state"][0])
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", fragment["challenge"][0])

    def test_a_good_callback_stores_the_key_and_sends_the_browser_back(self, site):
        attempt = rarus_login.BrowserLogin(site=site.url, label="Rarus AI test")
        url = attempt.start()

        reply = _come_back(url)
        session = attempt.wait(5)

        assert reply.status_code == 302
        assert reply.headers["location"] == f"{site.url}/app-login#done=1"
        assert session.key == KEY
        assert session.email == "me@example.com"
        assert rarus_session.load_key() == KEY
        claim = site.claims[0]
        assert claim["code"] == "one-time-code"
        assert claim["label"] == "Rarus AI test"
        assert rarus_login.challenge_of(claim["verifier"]) == parse_qs(urlparse(url).fragment)["challenge"][0]

    def test_a_wrong_state_is_refused_and_does_not_end_the_sign_in(self, site):
        attempt = rarus_login.BrowserLogin(site=site.url)
        url = attempt.start()

        stray = _come_back(url, state="x" * 32)
        assert stray.headers["location"].endswith("#error=state")
        assert site.claims == []

        _come_back(url)
        assert attempt.wait(5).key == KEY

    def test_ten_keys_on_the_account_is_reported_as_keys(self, site):
        site.claim_status = 409
        attempt = rarus_login.BrowserLogin(site=site.url)
        url = attempt.start()

        reply = _come_back(url)
        with pytest.raises(rarus_login.LoginError) as raised:
            attempt.wait(5)

        assert reply.headers["location"].endswith("#error=keys")
        assert raised.value.code == "keys"
        assert rarus_session.load_key() is None

    def test_a_blocked_account_is_reported_as_blocked(self, site):
        site.claim_status = 403
        attempt = rarus_login.BrowserLogin(site=site.url)
        url = attempt.start()
        _come_back(url)
        with pytest.raises(rarus_login.LoginError) as raised:
            attempt.wait(5)
        assert raised.value.code == "blocked"

    def test_a_browser_that_never_comes_back_times_out(self, site):
        attempt = rarus_login.BrowserLogin(site=site.url)
        attempt.start()
        with pytest.raises(rarus_login.LoginError) as raised:
            attempt.wait(0.2)
        assert raised.value.code == "timeout"

    def test_only_the_callback_path_is_served(self, site):
        attempt = rarus_login.BrowserLogin(site=site.url)
        url = attempt.start()
        port = parse_qs(urlparse(url).fragment)["port"][0]
        try:
            assert httpx.get(f"http://127.0.0.1:{port}/").status_code == 404
        finally:
            attempt.cancel()
            attempt.close()

    def test_login_opens_no_browser_when_told_not_to(self, site, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(rarus_login.webbrowser, "open", opened.append)
        seen: list[str] = []

        def on_url(url: str) -> None:
            seen.append(url)
            threading.Thread(target=_come_back, args=(url,), daemon=True).start()

        session = rarus_login.login(site=site.url, open_browser=False, on_url=on_url, timeout_s=5)

        assert session.key == KEY
        assert opened == []
        assert seen


class TestTheAccountCalls:
    def test_whoami_reports_the_balance(self, site):
        assert rarus_client.whoami(KEY, site=site.url)["balance"] == 42

    def test_the_model_list_is_the_servers(self, site):
        assert rarus_client.models(KEY, site=site.url) == ["deepseek-flash", "auto"]

    def test_a_revoked_key_is_named_so(self, site):
        with pytest.raises(rarus_client.RarusError) as raised:
            rarus_client.models("sk-ab-wrong", site=site.url)
        assert raised.value.code == "unauthorized"

    @pytest.mark.parametrize(("status", "code"), [(402, "no_credits"), (403, "blocked"), (500, "server")])
    def test_statuses_have_names(self, site, status, code):
        site.whoami_status = status
        with pytest.raises(rarus_client.RarusError) as raised:
            rarus_client.whoami(KEY, site=site.url)
        assert raised.value.code == code

    def test_an_unreachable_server_is_a_network_error(self):
        with pytest.raises(rarus_client.RarusError) as raised:
            rarus_client.whoami(KEY, site="http://127.0.0.1:1")
        assert raised.value.code == "network"

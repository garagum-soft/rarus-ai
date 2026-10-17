"""The update client: a manifest, a checked download, and a way back."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rarus_app import updater

INSTALLER = b"MZ" + b"installer-bytes" * 100
GOOD = hashlib.sha256(INSTALLER).hexdigest()


class Server:
    """Serves a manifest at /manifest.json and an installer at /setup.exe over plain http."""

    def __init__(self, manifest: dict | None = None, payload: bytes = INSTALLER) -> None:
        self.manifest = manifest
        self.payload = payload
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/manifest.json" and outer.manifest is not None:
                    body = json.dumps(outer.manifest).encode()
                elif self.path == "/setup.exe":
                    body = outer.payload
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    made: list[Server] = []

    def make(**kwargs) -> Server:
        s = Server(**kwargs)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def _manifest(base: str = "https://updates.example.test", version: str = "0.2.0", sha: str = GOOD) -> dict:
    return {"version": version, "url": f"{base}/setup.exe", "sha256": sha, "notes": "fixes"}


class TestVersions:
    @pytest.mark.parametrize(("a", "b"), [("0.2.0", "0.1.9"), ("1.0.0", "0.9.9"), ("v1.2.3", "1.2.2"), ("0.10.0", "0.9.0")])
    def test_ordering(self, a, b):
        assert updater.version_key(a) > updater.version_key(b)

    def test_a_release_candidate_equals_its_release(self):
        assert updater.version_key("1.2.3rc1") == updater.version_key("1.2.3")

    @pytest.mark.parametrize("bad", ["", "x.y.z", "1.2", "latest"])
    def test_unreadable_versions_raise(self, bad):
        with pytest.raises(ValueError):
            updater.version_key(bad)


class TestManifest:
    def test_a_good_manifest_parses(self):
        release = updater.parse_manifest(_manifest())
        assert release.version == "0.2.0"
        assert release.sha256 == GOOD

    @pytest.mark.parametrize("missing", ["sha256", "url", "version"])
    def test_a_missing_field_is_refused(self, missing):
        data = _manifest()
        data.pop(missing)
        with pytest.raises(updater.UpdateError):
            updater.parse_manifest(data)

    def test_a_bad_digest_is_refused_so_nothing_unverified_is_offered(self):
        with pytest.raises(updater.UpdateError, match="sha256"):
            updater.parse_manifest(_manifest(sha="abc"))

    def test_an_http_installer_address_is_refused(self):
        with pytest.raises(updater.UpdateError, match="https"):
            updater.parse_manifest(_manifest(base="http://example.test"))

    def test_a_manifest_that_is_not_an_object_is_refused(self):
        with pytest.raises(updater.UpdateError):
            updater.parse_manifest(["nope"])


class TestCheck:
    def test_with_no_address_nobody_is_asked(self):
        assert updater.check(current="0.1.0") is None

    def test_a_newer_release_is_reported(self, server):
        s = server(manifest=_manifest())
        release = updater.check(current="0.1.0", url=f"{s.base}/manifest.json")
        assert release is not None and release.version == "0.2.0"

    def test_the_same_version_is_not_an_update(self, server):
        s = server(manifest=_manifest(version="0.1.0"))
        assert updater.check(current="0.1.0", url=f"{s.base}/manifest.json") is None

    def test_an_unreachable_server_is_an_error_the_caller_can_show(self):
        with pytest.raises(updater.UpdateError):
            updater.check(current="0.1.0", url="http://127.0.0.1:1/manifest.json")

    def test_the_address_comes_from_the_environment(self, server, monkeypatch):
        s = server(manifest=_manifest())
        monkeypatch.setenv(updater.URL_ENV, f"{s.base}/manifest.json")
        assert updater.check(current="0.1.0") is not None


class TestDownload:
    def test_a_matching_installer_is_kept(self, server):
        s = server()
        release = updater.Release("0.2.0", f"{s.base}/setup.exe", GOOD)
        path = updater.download(release, allow_http=True)
        assert path.read_bytes() == INSTALLER
        assert path.name == "RarusAI-0.2.0-setup.exe"

    def test_a_tampered_installer_is_deleted_and_refused(self, server):
        s = server(payload=INSTALLER + b"evil")
        release = updater.Release("0.2.0", f"{s.base}/setup.exe", GOOD)
        with pytest.raises(updater.UpdateError, match="does not match"):
            updater.download(release, allow_http=True)
        assert list(updater.updates_dir().glob("*.exe")) == []
        assert list(updater.updates_dir().glob("*.part")) == []

    def test_http_is_refused_unless_a_test_allows_it(self, server):
        s = server()
        with pytest.raises(updater.UpdateError, match="https"):
            updater.download(updater.Release("0.2.0", f"{s.base}/setup.exe", GOOD))

    def test_a_missing_installer_is_an_error(self, server):
        s = server()
        with pytest.raises(updater.UpdateError):
            updater.download(updater.Release("0.2.0", f"{s.base}/nope.exe", GOOD), allow_http=True)


class TestRollback:
    def test_nothing_to_roll_back_to_before_an_update(self):
        assert updater.rollback_installer() is None

    def test_the_replaced_version_is_kept_and_only_one(self, tmp_path):
        first = tmp_path / "a.exe"
        first.write_bytes(b"one")
        second = tmp_path / "b.exe"
        second.write_bytes(b"two")
        updater.remember_current(first, "0.1.0")
        updater.remember_current(second, "0.2.0")
        kept = updater.rollback_installer()
        assert kept is not None and kept.name == "RarusAI-0.2.0-setup.exe"
        assert len(list((updater.updates_dir() / "previous").glob("*.exe"))) == 1

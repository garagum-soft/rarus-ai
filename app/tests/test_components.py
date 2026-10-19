"""Components: the list, the choices, the download that resumes and checks itself, and the queue."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from rarus_app import component_paths, components
from rarus_app.components import Catalogue, Component, ComponentError
from rarus_app.controller import Controller

BASE = "https://rarus-ai.com/api/mirror/rarus/components/"


def _zip(cid: str, files: dict[str, bytes] | None = None, *, mark: bool = True) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        if mark:
            archive.writestr("component.json", json.dumps({"id": cid, "env": {}}))
        for name, data in (files or {f"site/{cid}_pkg/__init__.py": b"VALUE = 1\n"}).items():
            archive.writestr(name, data)
    return out.getvalue()


def _component(cid: str, data: bytes, setup: str = "optional", requires: tuple[str, ...] = ()) -> Component:
    digest = hashlib.sha256(data).hexdigest()
    return Component(
        id=cid, setup=setup, file=f"{cid}-{digest[:12]}.zip", sha256=digest, size=len(data), requires=requires,
        title={"ru": cid.upper(), "en": cid}, about={"ru": "о " + cid},
    )


class Mirror:
    """The site's open mirror: whole files, ranges, and a connection that can break once."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.break_after: dict[str, int] = {}
        self.ignore_range = False
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((name, request.headers.get("range", "")))
        data = self.files.get(name)
        if data is None:
            return httpx.Response(404)
        start = 0
        if request.headers.get("range") and not self.ignore_range:
            start = int(request.headers["range"].split("=")[1].split("-")[0])
            if start >= len(data):
                return httpx.Response(416)
        body = data[start:]
        cut = self.break_after.pop(name, None)
        if cut is not None:
            return httpx.Response(206 if start else 200, stream=_Broken(body[:cut]))
        return httpx.Response(206 if start else 200, content=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class _Broken(httpx.SyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self):
        yield self.data
        raise httpx.ReadError("connection reset")


def _catalogue(*items: Component) -> Catalogue:
    return Catalogue(BASE, tuple(items))


# -- the list ------------------------------------------------------------------------------------


def test_the_shipped_list_is_read_and_bad_entries_are_dropped(tmp_path) -> None:
    good = {"id": "browser", "setup": "optional", "file": "browser-1.zip", "sha256": "a" * 64, "size": 10}
    path = tmp_path / "components.json"
    path.write_text(
        json.dumps(
            {
                "base_url": BASE,
                "components": [
                    good,
                    {**good, "id": "bad-group", "setup": "maybe"},
                    {**good, "id": "bad-sha", "sha256": "abc"},
                    {**good, "id": "bad-name", "file": "../x.zip"},
                    {"id": "no-size"},
                ],
            }
        ),
        encoding="utf-8",
    )
    catalogue = components.load_catalogue(path)
    assert [c.id for c in catalogue.components] == ["browser"]
    assert catalogue.base_url == BASE


def test_a_program_without_a_list_has_no_components(tmp_path) -> None:
    assert components.load_catalogue(tmp_path / "missing.json").components == ()


def test_what_is_wanted_follows_the_groups_the_choices_and_what_they_require() -> None:
    data = b"x"
    cat = _catalogue(
        _component("textkit", data, "hidden"),
        _component("memory", data, "locked"),
        _component("browser", data, "optional"),
        _component("design", data, "optional", requires=("browser",)),
        _component("messengers", data, "off"),
    )
    ids = lambda choices: [c.id for c in components.wanted(cat, choices)]  # noqa: E731
    assert ids({}) == ["textkit", "memory", "browser", "design"]
    assert ids({"memory": False, "textkit": False}) == ["textkit", "memory", "browser", "design"]
    assert ids({"design": False, "browser": False, "messengers": True}) == ["textkit", "memory", "messengers"]
    assert ids({"browser": False}) == ["textkit", "memory", "browser", "design"]


@pytest.mark.parametrize(
    "url", ["http://rarus-ai.com/x/", "https://evil.example/x/", "ftp://rarus-ai.com/x/", "http://192.168.1.5/x/"]
)
def test_components_come_only_over_https_from_the_programs_own_site(monkeypatch, url) -> None:
    monkeypatch.setenv(components.URL_ENV, url)
    with pytest.raises(ComponentError):
        components.base_url(_catalogue())


def test_a_local_test_mirror_may_be_plain_http(monkeypatch) -> None:
    monkeypatch.setenv(components.URL_ENV, "http://127.0.0.1:8000/m")
    assert components.base_url(_catalogue()) == "http://127.0.0.1:8000/m/"


# -- one download ---------------------------------------------------------------------------------


def test_a_broken_download_resumes_where_it_stopped(tmp_path) -> None:
    data = _zip("browser") * 20
    item = _component("browser", data)
    mirror = Mirror({item.file: data})
    mirror.break_after[item.file] = 1000
    seen = []

    path = components.download(
        item, BASE, root=tmp_path, client=mirror.client(), progress=lambda r, t: seen.append(r), pause=lambda s: None
    )

    assert path.read_bytes() == data
    assert mirror.requests == [(item.file, ""), (item.file, "bytes=1000-")]
    assert seen[-1] == len(data)


def test_a_server_that_ignores_the_range_starts_the_file_over(tmp_path) -> None:
    data = _zip("browser") * 5
    item = _component("browser", data)
    mirror = Mirror({item.file: data})
    mirror.break_after[item.file] = 300
    mirror.ignore_range = True

    path = components.download(item, BASE, root=tmp_path, client=mirror.client(), pause=lambda s: None)

    assert path.read_bytes() == data


def test_a_file_that_does_not_match_its_sha256_is_deleted(tmp_path) -> None:
    data = _zip("browser")
    item = _component("browser", data)
    mirror = Mirror({item.file: data[:-1] + b"!"})

    with pytest.raises(ComponentError, match="SHA-256"):
        components.download(item, BASE, root=tmp_path, client=mirror.client(), pause=lambda s: None)
    assert list((tmp_path / components.DOWNLOADS).iterdir()) == []


def test_a_server_that_keeps_failing_is_reported_after_the_last_attempt(tmp_path) -> None:
    item = _component("browser", b"data")
    mirror = Mirror({})

    with pytest.raises(ComponentError, match="404"):
        components.download(item, BASE, root=tmp_path, client=mirror.client(), pause=lambda s: None)


def test_unpacking_refuses_a_path_outside_the_component(tmp_path) -> None:
    data = _zip("evil", {"../../outside.txt": b"x"})
    item = _component("evil", data)
    archive = tmp_path / "evil.zip"
    archive.write_bytes(data)

    with pytest.raises(ComponentError, match="outside"):
        components.unpack(item, archive, root=tmp_path / "components")
    assert not (tmp_path / "outside.txt").exists()


def test_unpacking_needs_the_component_mark(tmp_path) -> None:
    data = _zip("odd", mark=False)
    item = _component("odd", data)
    archive = tmp_path / "odd.zip"
    archive.write_bytes(data)

    with pytest.raises(ComponentError, match="not a component"):
        components.unpack(item, archive, root=tmp_path / "components")


# -- the queue ------------------------------------------------------------------------------------


def _installer(mirror: Mirror, *items: Component) -> components.Installer:
    return components.Installer(_catalogue(*items), client_factory=mirror.client, pause=lambda s: None)


def _wait(installer: components.Installer, timeout: float = 10.0) -> dict:
    installer.wait(timeout)
    status = installer.status()
    assert status["state"] != "working"
    return status


def test_the_queue_installs_records_and_the_next_process_sees_the_packages(monkeypatch) -> None:
    good, other = _zip("browser"), _zip("design")
    browser, design = _component("browser", good), _component("design", other)
    mirror = Mirror({browser.file: good, design.file: other})
    installer = _installer(mirror, browser, design)

    status = installer.start([browser, design])
    status = _wait(installer)

    assert [i["state"] for i in status["items"]] == ["done", "done"]
    assert status["received"] == status["total"] == len(good) + len(other)
    state = component_paths.read_state(component_paths.components_dir())
    assert state["components"]["browser"] == {"dir": browser.folder, "sha256": browser.sha256, "enabled": True}
    assert not list((component_paths.components_dir() / components.DOWNLOADS).glob("*.zip"))
    monkeypatch.setattr(sys, "path", ["bundle"])
    assert len(component_paths.activate()) == 2
    assert sys.path[0].endswith(str(Path(browser.folder) / "site"))


def test_one_failure_does_not_stop_the_others_and_a_retry_takes_only_it() -> None:
    good, bad = _zip("browser"), _zip("design")
    browser, design = _component("browser", good), _component("design", bad)
    mirror = Mirror({browser.file: good})
    installer = _installer(mirror, browser, design)

    installer.start([design, browser])
    status = _wait(installer)
    assert [(i["id"], i["state"]) for i in status["items"]] == [("design", "failed"), ("browser", "done")]
    assert status["failed"] == ["design"]

    mirror.files[design.file] = bad
    installer.start([design])
    assert _wait(installer)["failed"] == []


def test_a_new_version_replaces_the_old_folder() -> None:
    old_data, new_data = _zip("browser", {"site/a.py": b"1"}), _zip("browser", {"site/a.py": b"2"})
    old, new = _component("browser", old_data), _component("browser", new_data)
    mirror = Mirror({old.file: old_data, new.file: new_data})
    root = component_paths.components_dir()

    installer = _installer(mirror, old)
    installer.start([old])
    _wait(installer)
    installer = _installer(mirror, new)
    installer.start([new])
    _wait(installer)

    assert not (root / old.folder).exists()
    assert (root / new.folder / "site" / "a.py").read_text() == "2"


def test_sweep_removes_folders_the_state_does_not_name_but_keeps_one_being_unpacked() -> None:
    root = component_paths.components_dir()
    (root / "browser" / "aaaaaaaaaaaa").mkdir(parents=True)
    (root / "browser" / ".bbbbbbbbbbbb.tmp").mkdir(parents=True)
    (root / "browser" / "cccccccccccc").mkdir(parents=True)
    components.write_state({"components": {"browser": {"dir": "browser/cccccccccccc", "sha256": "c" * 64}}})

    components.sweep(_catalogue())

    assert sorted(p.name for p in (root / "browser").iterdir()) == [".bbbbbbbbbbbb.tmp", "cccccccccccc"]


# -- the controller: the first start and the settings --------------------------------------------


class _Engine:
    started = 0
    stopped = 0

    def __init__(self) -> None:
        pass

    def start(self) -> None:
        _Engine.started += 1

    def stop(self) -> None:
        _Engine.stopped += 1

    def alive(self) -> bool:
        return True


def _controller(mirror: Mirror, *items: Component) -> Controller:
    _Engine.started = _Engine.stopped = 0
    return Controller(engine_factory=_Engine, open_browser=False, installer=_installer(mirror, *items))


def _until(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_the_first_start_lists_the_components_then_starts_the_engine_once_they_are_answered() -> None:
    data = {cid: _zip(cid) for cid in ("textkit", "memory", "browser", "design", "messengers")}
    items = [
        _component("textkit", data["textkit"], "hidden"),
        _component("memory", data["memory"], "locked"),
        _component("browser", data["browser"], "optional"),
        _component("design", data["design"], "optional", requires=("browser",)),
        _component("messengers", data["messengers"], "off"),
    ]
    mirror = Mirror({c.file: data[c.id] for c in items})
    ctl = _controller(mirror, *items)

    view = ctl.components_view("ru")
    assert view["pending"] is True
    assert [(i["id"], i["on"], i["title"]) for i in view["items"]] == [
        ("memory", True, "MEMORY"), ("browser", True, "BROWSER"), ("design", True, "DESIGN"),
        ("messengers", False, "MESSENGERS"),
    ]
    assert view["hidden_bytes"] == len(data["textkit"])

    ctl.components_install({"design": False, "memory": False, "messengers": False})
    _until(lambda: ctl.components_status()["state"] == "done")
    installed = set(component_paths.read_state(component_paths.components_dir())["components"])
    assert installed == {"textkit", "memory", "browser"}

    assert ctl.components_finish() == {"ok": True, "restarted": False}
    _until(lambda: _Engine.started == 1)
    assert ctl.components_pending() is False


def test_switching_a_component_off_later_removes_it_and_restarts_the_engine() -> None:
    data = _zip("browser")
    browser = _component("browser", data)
    mirror = Mirror({browser.file: data})
    ctl = _controller(mirror, browser)
    ctl.components_install({})
    _until(lambda: ctl.components_status()["state"] == "done")
    ctl.components_finish()
    _until(lambda: _Engine.started == 1)

    ctl.components_install({"browser": False})
    assert "browser" not in component_paths.read_state(component_paths.components_dir())["components"]
    assert ctl.components_finish()["restarted"] is True
    _until(lambda: _Engine.started == 2)
    assert _Engine.stopped == 1

"""A household on one server: docker/family runs one Raven per member, each with its own port, data and sign-in.

``family.sh`` is run for real by bash, with a stand-in ``docker`` that records
what it was asked to do: the script's own logic -- names, ports, data
directories, the compose project each member runs as, the sign-in link -- is
what is under test, not Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FAMILY = ROOT / "docker" / "family"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records each call with the environment family.sh hands Compose, and answers
# the two questions the script asks: a sign-in link, and whether a container runs.
{
  printf 'CALL'
  printf ' %s' "$@"
  printf ' | port=%s data=%s env=%s bind=%s image=%s\n' \
    "${RAVEN_WEB_PORT:-}" "${FAMILY_DATA:-}" "${FAMILY_MEMBER_ENV:-}" "${FAMILY_BIND:-}" "${RAVEN_IMAGE:-}"
} >>"$DOCKER_LOG"
case " $* " in
  *" docker-entrypoint.sh signin "*) printf 'http://localhost:%s/auth#nonce-%s\n' "$RAVEN_WEB_PORT" "$RAVEN_WEB_PORT" ;;
  *" inspect "*) case "$*" in *raven-alice-raven-1*) echo running ;; *) exit 1 ;; esac ;;
esac
"""


def _bash() -> str:
    for candidate in (
        shutil.which("bash"),
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ):
        if candidate and Path(candidate).exists() and "System32" not in candidate:
            return candidate
    pytest.skip("no bash to run family.sh with")


class Household:
    def __init__(self, tmp_path: Path) -> None:
        self.bash = _bash()
        self.members = tmp_path / "members"
        self.data = tmp_path / "data"
        self.log = tmp_path / "docker.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "docker"
        fake.write_bytes(FAKE_DOCKER.encode())
        fake.chmod(0o755)
        self.env = {
            **os.environ,
            "FAMILY_MEMBERS_DIR": self.members.as_posix(),
            "FAMILY_DATA_DIR": self.data.as_posix(),
            "FAMILY_HOST": "192.168.1.10",
            "DOCKER_LOG": self.log.as_posix(),
            "TZ": "Europe/Moscow",
        }
        self.path_prefix = bin_dir.as_posix()

    def run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        script = (FAMILY / "family.sh").as_posix()
        quoted = " ".join(f"'{a}'" for a in args)
        # A non-login bash on Windows starts without /usr/bin on PATH; the
        # script runs under the very bash this is, with the stand-in docker first.
        to_unix = f'$(/usr/bin/cygpath -u "{self.path_prefix}" 2>/dev/null || echo "{self.path_prefix}")'
        command = f'export PATH="{to_unix}:/usr/bin:/bin:$PATH"; "$BASH" "{script}" {quoted}'
        return subprocess.run(
            [self.bash, "-c", command],
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def home(tmp_path: Path) -> Household:
    return Household(tmp_path)


def test_each_member_gets_a_port_an_env_file_and_a_data_directory_of_their_own(home: Household) -> None:
    alice = home.run("add", "alice")
    bob = home.run("add", "bob")
    chosen = home.run("add", "kid-1", "--port", "19000")

    assert alice.returncode == 0, alice.stderr
    assert bob.returncode == 0 and chosen.returncode == 0, bob.stderr + chosen.stderr
    assert "added alice on port 18801" in alice.stdout and "added bob on port 18802" in bob.stdout
    env = (home.members / "alice.env").read_text(encoding="utf-8")
    assert "RAVEN_WEB_PORT=18801" in env and "TZ=Europe/Moscow" in env
    assert "# RAVEN_API_KEY=" in env, "a key is the member's to add, never filled in"
    assert (home.data / "alice").is_dir() and (home.data / "bob").is_dir()
    assert "RAVEN_WEB_PORT=19000" in (home.members / "kid-1.env").read_text(encoding="utf-8")
    assert home.calls() == [], "adding a member starts nothing"


def test_a_bad_add_is_refused_and_writes_nothing(home: Household) -> None:
    home.run("add", "alice")

    again = home.run("add", "alice")
    bad_name = home.run("add", "Alice Smith")
    taken = home.run("add", "bob", "--port", "18801")
    low = home.run("add", "bob", "--port", "80")
    sized = home.run("add", "carol", "--size", "20G")
    bad_size = home.run("add", "carol", "--size", "lots")

    assert again.returncode != 0 and "already exists" in again.stderr
    assert bad_name.returncode != 0 and "lower-case" in bad_name.stderr
    assert taken.returncode != 0 and "already alice's" in taken.stderr
    assert low.returncode != 0 and "1024 to 65535" in low.stderr
    if os.name == "nt" or os.geteuid() != 0:  # the disk image needs root
        assert sized.returncode != 0 and "needs root" in sized.stderr and "sudo" in sized.stderr
    assert bad_size.returncode != 0 and "M or G" in bad_size.stderr
    assert sorted(p.name for p in home.members.iterdir()) == ["alice.env"]
    assert sorted(p.name for p in home.data.iterdir()) == ["alice"]


def test_up_runs_every_member_as_their_own_compose_project(home: Household) -> None:
    home.run("add", "alice")
    home.run("add", "bob")

    up = home.run("up")
    one = home.run("up", "bob")
    unknown = home.run("up", "zed")

    assert up.returncode == 0, up.stderr
    started = [c for c in home.calls() if c.startswith("CALL compose")]
    assert len(started) == 3
    assert started[0].startswith("CALL compose -p raven-alice -f ")
    assert "docker/family/compose.yml up -d | port=18801" in started[0]
    assert f"data={home.data.as_posix()}/alice" in started[0]
    assert f"env={home.members.as_posix()}/alice.env" in started[0]
    assert "bind=0.0.0.0 image=raven:family" in started[0]
    assert "-p raven-bob" in started[1] and "port=18802" in started[1]
    assert "-p raven-bob" in started[2]
    assert "alice: http://192.168.1.10:18801" in up.stdout and "bob: http://192.168.1.10:18802" in up.stdout
    assert unknown.returncode != 0 and "no member 'zed'" in unknown.stderr


def test_signin_gives_a_link_on_the_server_s_address_for_that_member_only(home: Household) -> None:
    home.run("add", "alice")
    home.run("add", "bob")

    link = home.run("signin", "bob")

    assert link.returncode == 0, link.stderr
    assert "http://192.168.1.10:18802/auth#nonce-18802" in link.stdout
    assert "localhost" not in link.stdout
    (call,) = home.calls()
    assert "-p raven-bob" in call and "exec -T raven docker-entrypoint.sh signin" in call


def test_list_shows_each_member_s_port_state_and_disk_use(home: Household) -> None:
    home.run("add", "alice")
    home.run("add", "bob")
    (home.data / "alice" / "notes.txt").write_text("x" * 5000, encoding="utf-8")

    listed = home.run("list")

    rows = {line.split()[0]: line.split() for line in listed.stdout.splitlines()[1:]}
    assert listed.stdout.splitlines()[0].split() == ["MEMBER", "PORT", "STATE", "USED", "LIMIT"]
    assert rows["alice"][1:3] == ["18801", "running"] and rows["alice"][4] == "none"
    assert rows["bob"][1:3] == ["18802", "stopped"]


def test_remove_keeps_the_data_unless_purged_and_confirmed(home: Household) -> None:
    home.run("add", "alice")
    home.run("add", "bob")

    kept = home.run("remove", "alice")
    wrong = home.run("remove", "bob", "--purge", stdin="yes\n")
    still_there = (home.members / "bob.env").exists() and (home.data / "bob").is_dir()
    purged = home.run("remove", "bob", "--purge", stdin="bob\n")

    assert kept.returncode == 0 and (home.data / "alice").is_dir() and not (home.members / "alice.env").exists()
    assert "their data stays" in kept.stdout
    assert wrong.returncode != 0 and "not deleted" in wrong.stderr
    assert still_there, "a wrong answer leaves the member exactly as they were"
    assert purged.returncode == 0 and not (home.data / "bob").exists() and not (home.members / "bob.env").exists()
    assert sum(" down " in c for c in home.calls()) == 2, "the refused purge stopped nothing"


def test_build_makes_one_shared_image_with_the_claude_extra(home: Household) -> None:
    built = home.run("build")

    assert built.returncode == 0, built.stderr
    (call,) = home.calls()
    assert call.startswith("CALL build -t raven:family -f ")
    assert "--build-arg RAVEN_EXTRAS=channels,tools,claude" in call
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "\nclaude = [" in pyproject, "the extra the family image asks for exists"


def test_the_member_compose_file_keeps_members_apart_and_off_auto_login() -> None:
    spec = yaml.safe_load((FAMILY / "compose.yml").read_text(encoding="utf-8"))
    raven = spec["services"]["raven"]

    assert raven["ports"] == ["${FAMILY_BIND:-0.0.0.0}:${RAVEN_WEB_PORT:?family.sh sets the member's port}:80"]
    env = raven["environment"]
    assert env["RAVEN_AUTO_LOGIN"] == "0", "a port on the home network never hands out a session"
    # The cookie is named after the engine's port; per-member ports keep two
    # sign-ins on one tablet from overwriting each other.
    assert env["RAVEN_PAGE_PORT"] == "${RAVEN_WEB_PORT}"
    assert env["CLAUDE_CONFIG_DIR"].startswith("/data/")
    assert raven["volumes"] == ["${FAMILY_DATA:?family.sh sets the member's data directory}:/data"]
    assert [f["path"] for f in raven["env_file"]] == [
        "../.env",
        "${FAMILY_MEMBER_ENV:?family.sh sets the member's env file}",
    ]
    assert "name" not in spec, "the project name is family.sh's -p, one per member"


def test_members_settings_and_data_are_never_committed() -> None:
    for path in ("docker/family/members/alice.env", "docker/family/data/alice/config.json"):
        done = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", path], cwd=ROOT, capture_output=True, check=False
        )
        if done.returncode not in (0, 1):
            pytest.skip("not a git work tree")
        assert done.returncode == 0, f"git would stage {path}"


def test_members_settings_and_data_never_reach_the_image_build() -> None:
    """`family.sh build` builds from this checkout, where every member's data lives."""
    rules = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert "docker/family/data" in rules and "docker/family/members" in rules
    # Docker applies the last matching rule: nothing after them may re-include either.
    after = rules[rules.index("docker/family/data") + 1 :]
    assert not [r for r in after if r.startswith("!docker/family")]

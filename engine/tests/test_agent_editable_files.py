"""The web editor's files: a fixed list, hash-checked saves under the writers' locks, every save a revision."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from raven.agent import editable_files as ef
from raven.agent.instructions import INSTRUCTION_FILES, InstructionHistory


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "agent_memory" / "profile").mkdir(parents=True)
    (tmp_path / "user_memory" / "profile").mkdir(parents=True)
    (tmp_path / "agent_memory" / "profile" / "soul.md").write_text("I am Raven.\n", encoding="utf-8")
    (tmp_path / "user_memory" / "profile" / "user.md").write_text("# User\nLikes tea.\n", encoding="utf-8")
    return tmp_path


def test_only_the_listed_files_are_editable_and_described(home: Path) -> None:
    rows = {r["key"]: r for r in ef.describe(home)}

    assert set(INSTRUCTION_FILES) <= set(rows)
    assert rows["agent_memory/profile/soul.md"] == {
        "key": "agent_memory/profile/soul.md",
        "kind": "instruction",
        "exists": True,
        "size": 12,
        "sha256": ef.sha256("I am Raven.\n"),
        "head": 0,
        "revisions": 0,
    }
    assert rows["TOOLS.md"]["exists"] is False and rows["TOOLS.md"]["sha256"] == ef.sha256("")
    assert rows["user_memory/profile/user.md"]["kind"] == "memory"
    for outside in ("config.json", "../secret.txt", "sessions/tui/x.jsonl", "/etc/passwd", "skills/x/SKILL.md"):
        with pytest.raises(ef.EditError, match="not an editable file"):
            ef.read(home, outside)
        with pytest.raises(ef.EditError):
            ef.save(home, outside, "x", expected_sha256=ef.sha256(""))


def test_a_save_writes_records_the_old_and_new_text_and_answers_the_new_hash(home: Path) -> None:
    opened = ef.read(home, "agent_memory/profile/soul.md")

    saved = ef.save(home, opened["key"], "I am Raven, and I am brief.\n", expected_sha256=opened["sha256"], note="tone")
    again = ef.read(home, opened["key"])

    assert (home / "agent_memory/profile/soul.md").read_text(encoding="utf-8") == "I am Raven, and I am brief.\n"
    assert saved == {"key": opened["key"], "sha256": again["sha256"], "head": 2, "saved": True}
    assert [(r["rev"], r["author"]) for r in again["revisions"]] == [(2, "web"), (1, "baseline")]
    assert again["revisions"][0]["note"] == "tone"
    assert ef.revision_text(home, opened["key"], 1) == "I am Raven.\n"
    assert "+I am Raven, and I am brief." in ef.diff(home, opened["key"], 1)
    unchanged = ef.save(home, opened["key"], again["text"], expected_sha256=again["sha256"])
    assert unchanged["saved"] is False and unchanged["head"] == 2


def test_a_file_that_changed_since_it_was_opened_is_refused_and_left_alone(home: Path) -> None:
    opened = ef.read(home, "agent_memory/profile/soul.md")
    (home / "agent_memory/profile/soul.md").write_text("The agent rewrote this.\n", encoding="utf-8")

    with pytest.raises(ef.EditConflictError) as caught:
        ef.save(home, opened["key"], "my edit\n", expected_sha256=opened["sha256"])

    assert caught.value.current_sha256 == ef.sha256("The agent rewrote this.\n")
    assert (home / "agent_memory/profile/soul.md").read_text(encoding="utf-8") == "The agent rewrote this.\n"


def test_a_change_made_on_disk_between_saves_is_kept_in_the_history(home: Path) -> None:
    key = "agent_memory/profile/soul.md"
    first = ef.read(home, key)
    ef.save(home, key, "v1\n", expected_sha256=first["sha256"])
    (home / key).write_text("v1, edited by the agent\n", encoding="utf-8")
    opened = ef.read(home, key)

    ef.save(home, key, "v2\n", expected_sha256=opened["sha256"])

    texts = [ef.revision_text(home, key, r["rev"]) for r in reversed(ef.read(home, key)["revisions"])]
    assert texts == ["I am Raven.\n", "v1\n", "v1, edited by the agent\n", "v2\n"]


def test_rollback_puts_an_old_revision_back_as_a_new_one(home: Path) -> None:
    key = "agent_memory/profile/soul.md"
    opened = ef.read(home, key)
    ef.save(home, key, "worse\n", expected_sha256=opened["sha256"])
    now = ef.read(home, key)

    stale = pytest.raises(ef.EditConflictError)
    with stale:
        ef.rollback(home, key, 1, expected_sha256=opened["sha256"])
    rolled = ef.rollback(home, key, 1, expected_sha256=now["sha256"])

    assert (home / key).read_text(encoding="utf-8") == "I am Raven.\n"
    assert rolled["head"] == 3 and rolled["sha256"] == ef.sha256("I am Raven.\n")
    newest = ef.read(home, key)["revisions"][0]
    assert newest["action"] == "rollback" and newest["restored_from"] == 1
    with pytest.raises(ef.EditError, match="no revision 9"):
        ef.rollback(home, key, 9, expected_sha256=rolled["sha256"])


def test_a_new_memory_file_is_created_and_the_size_limit_holds(home: Path) -> None:
    created = ef.save(home, "HEARTBEAT.md", "- water the plants\n", expected_sha256=ef.sha256(""))

    assert created["saved"] and (home / "HEARTBEAT.md").read_text(encoding="utf-8") == "- water the plants\n"
    with pytest.raises(ef.EditError, match="the limit is"):
        ef.save(home, "HEARTBEAT.md", "x" * (ef.MAX_BYTES + 1), expected_sha256=created["sha256"])
    with pytest.raises(ef.EditError, match="NUL"):
        ef.save(home, "HEARTBEAT.md", "a\x00b", expected_sha256=created["sha256"])


def test_a_profile_save_waits_for_the_consolidators_lock(home: Path) -> None:
    """user.md is rewritten under MemoryStore.locked(); a save takes the same lock."""
    from raven.memory_engine.consolidate.consolidator import MemoryStore

    store = MemoryStore(home)
    opened = ef.read(home, "user_memory/profile/user.md")
    finished: list[float] = []

    def save_in_background() -> None:
        ef.save(home, opened["key"], "# User\nLikes coffee.\n", expected_sha256=opened["sha256"])
        finished.append(time.monotonic())

    with store.locked():
        worker = threading.Thread(target=save_in_background)
        worker.start()
        time.sleep(0.4)
        released_at = time.monotonic()
        assert finished == [], "the save went ahead while the consolidator held the profile"
    worker.join(timeout=10)

    assert finished and finished[0] >= released_at
    assert store.read_long_term() == "# User\nLikes coffee.\n"


def test_the_default_history_still_tracks_only_the_instruction_files(home: Path) -> None:
    """F36 is unchanged: the agent's file tools and `raven instructions` keep the three files."""
    plain = InstructionHistory(home)

    assert plain.is_tracked("TOOLS.md") and not plain.is_tracked("user_memory/profile/user.md")
    assert ef.history(home).is_tracked("user_memory/profile/user.md")


# -- over RPC ----------------------------------------------------------------------------


def test_the_rpc_methods_list_get_save_and_refuse_a_stale_save(home: Path, monkeypatch) -> None:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods import instructions as rpc

    monkeypatch.setattr(rpc, "_agent_home", lambda: home)
    dispatcher = Dispatcher()
    rpc.register_instructions_methods(dispatcher)

    async def call(method: str, params: dict) -> dict:
        frame = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        return frame

    async def scenario():
        listed = await call("instructions.list", {})
        got = await call("instructions.get", {"key": "agent_memory/profile/soul.md"})
        saved = await call(
            "instructions.save",
            {"key": "agent_memory/profile/soul.md", "text": "new\n", "expected_sha256": got["result"]["sha256"]},
        )
        stale = await call(
            "instructions.save",
            {"key": "agent_memory/profile/soul.md", "text": "other\n", "expected_sha256": got["result"]["sha256"]},
        )
        outside = await call("instructions.get", {"key": "config.json"})
        diff = await call("instructions.diff", {"key": "agent_memory/profile/soul.md", "a": 1})
        rolled = await call(
            "instructions.rollback",
            {"key": "agent_memory/profile/soul.md", "rev": 1, "expected_sha256": saved["result"]["sha256"]},
        )
        return listed, got, saved, stale, outside, diff, rolled

    listed, got, saved, stale, outside, diff, rolled = asyncio.run(scenario())

    assert any(f["key"] == "TOOLS.md" for f in listed["result"]["files"])
    assert got["result"]["text"] == "I am Raven.\n"
    assert saved["result"]["saved"] is True
    assert stale["error"]["code"] == -32023
    assert stale["error"]["data"]["current_sha256"] == saved["result"]["sha256"]
    assert outside["error"]["code"] == -32011 and "not an editable file" in outside["error"]["data"]["detail"]
    assert "changed since it was opened" in stale["error"]["data"]["detail"]
    assert "+new" in diff["result"]["diff"]
    assert rolled["result"]["saved"] is True and (home / "agent_memory/profile/soul.md").read_text() == "I am Raven.\n"

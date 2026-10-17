"""Russian is a language of the page, the config and the agent's replies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven import i18n
from raven.config.schema import Config
from raven.context_engine.segments import render

CATALOGUE = Path(__file__).resolve().parent.parent / "i18n" / "messages.json"
# Shell commands shown as examples: the same in every language.
COMMAND_EXAMPLES = {"gui.set.prm.deny_ph"}


@pytest.fixture(autouse=True)
def _restore_language():
    before = i18n.current_language()
    yield
    i18n.set_language(before)


def test_the_config_accepts_russian():
    assert Config(language="ru").language == "ru"


def test_the_config_still_rejects_an_unknown_language():
    with pytest.raises(ValueError):
        Config(language="xx")


def test_russian_is_a_selectable_language():
    i18n.set_language("ru")
    assert i18n.current_language() == "ru"
    i18n.set_language("xx")
    assert i18n.current_language() == "en"


def test_command_line_messages_fall_back_to_english_in_russian():
    i18n.set_language("ru")
    assert i18n.t("Back") == "Back"


def test_the_agent_is_told_to_reply_in_russian():
    i18n.set_language("ru")
    assert "Russian" in render.reply_language()


class TestThePageCatalogue:
    @staticmethod
    def _ui() -> dict:
        return json.loads(CATALOGUE.read_text(encoding="utf-8"))["ui"]

    def test_every_key_has_a_russian_text(self):
        missing = [k for k, v in self._ui().items() if not v.get("ru")]
        assert missing == []

    def test_russian_keeps_every_placeholder_of_the_english(self):
        import re

        ph = re.compile(r"\{\{[^}]*\}\}|\{\w+\}")
        broken = [
            k
            for k, v in self._ui().items()
            if sorted(ph.findall(v.get("en", ""))) != sorted(ph.findall(v.get("ru", "")))
        ]
        assert broken == []

    def test_russian_texts_are_russian_not_a_copy_of_the_english(self):
        ui = self._ui()
        same = [
            k for k, v in ui.items() if v["ru"] == v.get("en") and any(c.isalpha() and c.isascii() for c in v["ru"])
        ]
        # names, units and symbols may legitimately stay the same; sentences may not
        sentences = [k for k in same if len(ui[k]["ru"].split()) > 3 and k not in COMMAND_EXAMPLES]
        assert sentences == []

    def test_the_product_is_not_called_raven_in_russian_text(self):
        offenders = [k for k, v in self._ui().items() if "Raven" in v["ru"]]
        assert offenders == []

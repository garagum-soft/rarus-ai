"""A Rarus agent key never reaches a log line (spec 12.1)."""

from __future__ import annotations

import pytest
from loguru import logger

from raven.cli._log_file import redirect_loguru_to_file
from raven.security.redact import redact

KEY = "sk-ab-" + "0123456789abcdef" * 3


@pytest.mark.parametrize(
    "text",
    [
        f"key is {KEY}",
        f"Authorization: Bearer {KEY}",
        f'{{"key": "{KEY}"}}',
        f"curl -H 'Authorization: Bearer {KEY}' https://alembox.com/api/agent/v1/models",
        f"RARUS_KEY={KEY}",
    ],
)
def test_the_key_is_redacted_wherever_it_appears(text):
    assert KEY not in redact(text)
    assert KEY[6:] not in redact(text)


def test_a_log_line_carrying_the_key_is_written_without_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "home"))
    path = redirect_loguru_to_file("secrets-test.log", terminal_level=None)
    try:
        logger.warning(f"sign-in finished with {KEY}")
        logger.complete()
    finally:
        logger.remove()
    written = path.read_text(encoding="utf-8")
    assert "sign-in finished" in written
    assert KEY not in written

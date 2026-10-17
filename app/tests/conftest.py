from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """Every test gets its own home; on Windows ``Path.home()`` ignores ``HOME``, so say it outright."""
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "rarus-home"))
    monkeypatch.setenv("RARUS_SECRET_STORE", "plain")
    monkeypatch.delenv("RARUS_UPDATE_URL", raising=False)

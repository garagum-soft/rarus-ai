"""Accounts mode, the parts that are not the server: accounts, sessions, prices, metering, the gate, estimates.

Every test runs on the per-test ``RAVEN_HOME`` the suite's conftest sets, so the
state lands under ``<tmp>/accounts``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.accounts import billing, estimate, gate
from raven.accounts.billing import Ledger, Meter, Price, PriceTable, lookup, price_usd, top_up
from raven.accounts.engines import apply_lent_providers, env_for, lendable
from raven.accounts.paths import accounts_root, engine_home, user_home
from raven.accounts.settings import HubSettings, SettingsStore
from raven.accounts.store import (
    Account,
    AccountError,
    AccountStore,
    AuditLog,
    SessionStore,
    Throttle,
    hash_password,
    verify_password,
)
from raven.home import raven_home


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """A server home of this test's own: on Windows the default would be the real ~/.raven."""
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.delenv(gate.ENV_ACCOUNT, raising=False)
    monkeypatch.delenv("RAVEN_ACCOUNTS_ROOT", raising=False)
    gate._cache.update(path=None, mtime=None, value=None)


# -- accounts --------------------------------------------------------------------------------


def test_state_lives_under_the_servers_home() -> None:
    assert accounts_root() == raven_home() / "accounts"


def test_passwords_are_kept_as_scrypt_hashes_only() -> None:
    stored = hash_password("correct horse")

    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not verify_password("anything", "garbage")
    with pytest.raises(AccountError):
        hash_password("short")


def test_the_first_account_is_the_superadmin_and_only_once() -> None:
    store = AccountStore()
    first = store.create("Alice", "password1", role="superadmin", server_home=True, first_only=True)

    assert first.username == "alice" and first.role == "superadmin" and first.server_home
    with pytest.raises(AccountError, match="already has its administrator"):
        store.create("mallory", "password1", role="superadmin", first_only=True)
    with pytest.raises(AccountError, match="one superadmin"):
        store.create("mallory", "password1", role="superadmin")
    with pytest.raises(AccountError, match="taken"):
        store.create("ALICE", "password2")
    with pytest.raises(AccountError, match="username"):
        store.create("no spaces", "password2")
    assert "password_hash" not in first.public() and first.public()["has_password"] is True


def test_permissions_by_role() -> None:
    store = AccountStore()
    root = store.create("root", "password1", role="superadmin")
    admin = store.create("ops", "password1", role="admin")
    user = store.create("bob", "password1")

    admin = store.update(admin.id, lambda a: setattr(a, "permissions", ["points.topup", "not.a.permission"]))

    assert admin.permissions == ["points.topup"], "an unknown permission is dropped"
    assert root.can("settings.edit") and root.can("anything")
    assert admin.can("points.topup") and not admin.can("users.manage")
    assert not user.can("points.topup")
    disabled = store.update(admin.id, lambda a: setattr(a, "disabled", True))
    assert not disabled.can("points.topup")


def test_balances_move_and_are_recorded() -> None:
    store = AccountStore()
    user = store.create("bob", "password1")
    ledger = Ledger()

    balance = top_up(store, ledger, user.id, 250, actor="root", note="first")
    store.adjust_balance(user.id, -100.5)

    assert balance == 250 and store.get(user.id).balance == pytest.approx(149.5)
    (row,) = ledger.rows(user.id)
    assert row["kind"] == "topup" and row["credits"] == 250 and row["actor"] == "root"
    with pytest.raises(ValueError):
        top_up(store, ledger, user.id, 0, actor="root")


def test_sessions_resolve_expire_and_revoke() -> None:
    sessions = SessionStore()
    token = sessions.issue("a1", now=1000.0)
    other = sessions.issue("a1", now=1000.0)

    assert sessions.resolve(token, now=1001.0) == "a1"
    assert sessions.resolve("forged", now=1001.0) is None
    assert sessions.resolve(token, now=1000.0 + 31 * 24 * 3600) is None, "expired after thirty days"
    raw = json.loads(sessions.path.read_text(encoding="utf-8"))
    assert token not in json.dumps(raw), "only the hash of a cookie is written down"
    sessions.revoke(token)
    assert sessions.resolve(token, now=1002.0) is None and sessions.resolve(other, now=1002.0) == "a1"
    assert sessions.revoke_account("a1") == 1 and sessions.resolve(other, now=1003.0) is None


def test_failed_sign_ins_close_the_door_for_a_while() -> None:
    throttle = Throttle(per_name=3, per_address=5, window_s=60)
    for _ in range(3):
        throttle.failed("bob", "1.2.3.4", now=0)

    assert throttle.blocked("bob", "9.9.9.9", now=1)
    assert not throttle.blocked("carol", "1.2.3.4", now=1)
    assert not throttle.blocked("bob", "1.2.3.4", now=61), "the window slides"
    throttle.failed("x", "1.2.3.4", now=62)
    throttle.failed("y", "1.2.3.4", now=62)
    throttle.failed("z", "1.2.3.4", now=62)
    throttle.failed("w", "1.2.3.4", now=62)
    throttle.failed("v", "1.2.3.4", now=62)
    assert throttle.blocked("anyone", "1.2.3.4", now=63), "too many from one address"


def test_the_audit_log_appends() -> None:
    log = AuditLog()
    log.record("root", "topup", "bob", credits=10)
    log.record("root", "settings", "", registration_open=False)

    rows = log.tail()
    assert [r["action"] for r in rows] == ["topup", "settings"] and rows[0]["credits"] == 10


# -- settings --------------------------------------------------------------------------------


def test_settings_are_typed_validated_and_hide_the_secret() -> None:
    store = SettingsStore()
    settings = store.update(
        {
            "registration_open": "false",
            "disk_quota_gb": "20",
            "google_client_id": "id.apps",
            "google_client_secret": "s3cret",
            "public_url": "https://raven.example.com/",
        }
    )

    assert settings.registration_open is False and settings.disk_quota_gb == 20.0
    assert settings.public_url == "https://raven.example.com" and settings.google_ready
    public = settings.public()
    assert "google_client_secret" not in public and public["google_client_secret_set"] is True
    assert "s3cret" not in json.dumps(public)
    with pytest.raises(ValueError, match="unknown"):
        store.update({"nope": 1})
    with pytest.raises(ValueError):
        store.update({"public_url": "raven.example.com"})
    with pytest.raises(ValueError):
        store.update({"own_access_share": 2})
    assert HubSettings().disk_quota_gb == 50 and HubSettings().own_access_share == 0.1
    assert HubSettings().new_account_credits == 10000, "the welcome bonus by default"


# -- prices and metering ---------------------------------------------------------------------


def test_a_price_is_found_exactly_without_the_provider_prefix_or_by_the_default() -> None:
    table = {
        "claude-sonnet-4-5": Price(3, 15, 0.3),
        "GPT-5": Price(1.25, 10, 0.125),
        "*": Price(9, 9, 9),
        "cli:*": Price(2, 8, 0),
    }

    assert lookup(table, "claude-sonnet-4-5")[1] == "claude-sonnet-4-5"
    assert lookup(table, "anthropic/claude-sonnet-4-5")[1] == "claude-sonnet-4-5"
    assert lookup(table, "gpt-5")[1] == "GPT-5"
    assert lookup(table, "mystery")[1] == "*"
    assert lookup(table, "cli:codex")[1] == "cli:*"
    assert lookup({}, "mystery") == (None, None)


def test_a_calls_price_counts_cache_hits_at_their_own_rate() -> None:
    row = {"input_tokens": 1_000_000, "output_tokens": 500_000, "cache_read_tokens": 2_000_000, "cache_write_tokens": 0}

    assert price_usd(row, Price(3, 15, 0.3)) == pytest.approx(3 + 7.5 + 0.6)


def test_a_fresh_server_has_placeholder_prices_so_nothing_is_free_by_accident() -> None:
    PriceTable().ensure()

    assert set(PriceTable().load()) == {"*", "cli:*"}


def _usage(home: Path, *rows: dict, name: str | None = None) -> Path:
    path = home / "telemetry" / (name or f"usage-{time.strftime('%Y-%m-%d')}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _engine_config(home: Path, providers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"providers": providers}), encoding="utf-8")


def test_the_meter_reads_new_whole_lines_only_and_starts_at_the_end_of_history() -> None:
    store = AccountStore()
    user = store.create("bob", "password1")
    home = user_home(user.id)
    path = _usage(home, {"model": "old", "input_tokens": 5})
    meter = Meter(store)

    assert meter.read_new(user, home) == [], "history before the meter started is not billed"
    _usage(home, {"model": "m", "input_tokens": 10}, {"_type": "tool_call", "name": "x"})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"model": "half')
    rows = meter.read_new(user, home)
    assert [r["model"] for r in rows] == ["m"], "tool-call rows and a half-written line are left"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('", "input_tokens": 1}\n')
    assert [r["model"] for r in meter.read_new(user, home)] == ["half"]


def test_lent_access_costs_the_full_price_and_own_access_a_tenth() -> None:
    store = AccountStore()
    user = store.create("bob", "password1", balance=100)
    user = store.update(user.id, lambda a: setattr(a, "lent", ["openai"]))
    home = user_home(user.id)
    _engine_config(home, {"openai": {"apiKey": "k"}, "anthropic": {"apiKey": "own"}})
    PriceTable().save({"gpt-4o": Price(2, 8, 1), "claude-sonnet-4-5": Price(2, 8, 1), "*": Price(0, 0, 0)})
    meter = Meter(store)
    meter.read_new(user, home)  # start the cursor
    _usage(
        home,
        {"model": "gpt-4o", "input_tokens": 1_000_000, "output_tokens": 0},
        {"model": "claude-sonnet-4-5", "input_tokens": 1_000_000, "output_tokens": 0},
    )

    charges = meter.charge(store.get(user.id), home, HubSettings(credits_per_usd=100))

    by_model = {c.model: c for c in charges}
    assert by_model["gpt-4o"].provider == "openai" and by_model["gpt-4o"].share == 1.0
    assert by_model["gpt-4o"].credits == pytest.approx(200)
    assert by_model["claude-sonnet-4-5"].share == 0.1 and by_model["claude-sonnet-4-5"].credits == pytest.approx(20)
    assert store.get(user.id).balance == pytest.approx(100 - 220)
    assert {r["model"] for r in Ledger().rows(user.id)} == {"gpt-4o", "claude-sonnet-4-5"}


def test_an_external_agents_estimate_is_charged_and_marked() -> None:
    store = AccountStore()
    user = store.create("bob", "password1", balance=10)
    home = user_home(user.id)
    PriceTable().save({"cli:*": Price(10, 10, 0), "*": Price(0, 0, 0)})
    meter = Meter(store)
    meter.read_new(user, home)
    _usage(
        home,
        {"model": "cli:claude-code", "input_tokens": 50_000, "output_tokens": 50_000, "estimated": True},
        name="external-usage-2026-10-15.jsonl",
    )

    (charge,) = meter.charge(store.get(user.id), home, HubSettings(credits_per_usd=100))

    assert charge.estimated and charge.provider == "claude_subscription" and charge.priced_as == "cli:*"
    assert charge.credits == pytest.approx(1.0 * 100 * 0.1)
    assert Ledger().rows(user.id)[0]["estimated"] is True


def test_a_model_with_no_price_costs_nothing_and_is_listed() -> None:
    store = AccountStore()
    user = store.create("bob", "password1", balance=5)
    home = user_home(user.id)
    PriceTable().save({"gpt-4o": Price(1, 1, 1)})
    meter = Meter(store)
    meter.read_new(user, home)
    _usage(home, {"model": "mystery-model", "input_tokens": 10})

    meter.charge(store.get(user.id), home, HubSettings())

    assert store.get(user.id).balance == 5
    assert Ledger().unpriced_models() == ["mystery-model"]


# -- the gate --------------------------------------------------------------------------------


def _req(origin: str = "user", deliver_text=None):
    return SimpleNamespace(origin=origin, deliver_text=deliver_text)


def test_outside_accounts_mode_the_gate_is_never_read(monkeypatch) -> None:
    gate.write_gate(raven_home(), blocked=True, reason="no credits")

    assert gate.blocked_reason() is None
    assert gate.refusal_for_turn(_req()) is None and gate.refusal_for_spawn() is None


def test_an_accounts_engine_refuses_new_work_while_blocked(monkeypatch) -> None:
    monkeypatch.setenv(gate.ENV_ACCOUNT, "a1")
    assert gate.write_gate(raven_home(), blocked=True, reason=gate.NO_CREDITS) is True
    assert gate.write_gate(raven_home(), blocked=True, reason=gate.NO_CREDITS) is False, "unchanged: not rewritten"

    assert gate.refusal_for_turn(_req("user")) == gate.NO_CREDITS
    assert gate.refusal_for_turn(_req("cron")) == gate.NO_CREDITS
    assert gate.refusal_for_turn(_req("subagent")) is None, "results of running work come back"
    assert gate.refusal_for_turn(_req("user", deliver_text="hi")) is None
    assert gate.refusal_for_spawn() == gate.NO_CREDITS

    from raven.token_wise import usage_context

    with usage_context.bind("web:s1"):
        assert gate.refusal_for_spawn() is None, "a spawn inside a running turn finishes with it"

    gate.write_gate(raven_home(), blocked=False)
    assert gate.refusal_for_turn(_req()) is None


# -- what external agents spent --------------------------------------------------------------


def test_tokens_are_counted_and_estimates_marked() -> None:
    tokens, method = estimate.count_tokens("hello world, this is a test")
    estimated = estimate.usage_row("claude-code", "write a poem", "roses are red", session_key="s", kind="cli")
    reported = estimate.usage_row("gemini", "q", "a", session_key="s", kind="acp", tokens_in=120, tokens_out=30)

    assert tokens > 0 and method in ("tokenizer", "chars-words")
    assert estimated["estimated"] is True and estimated["input_tokens"] > 0 and estimated["words_out"] == 3
    assert estimated["model"] == "cli:claude-code"
    assert reported["estimated"] is False and (reported["input_tokens"], reported["output_tokens"]) == (120, 30)


def test_which_runs_already_record_their_own_usage() -> None:
    def backend(kind: str, command: str = "") -> SimpleNamespace:
        return SimpleNamespace(kind=kind, command=command)

    assert estimate.records_its_own_usage(backend("raven-loop"))
    assert estimate.records_its_own_usage(backend("openai"))
    assert estimate.records_its_own_usage(backend("acp", "python C:/agents/raven-research/run.py --acp"))
    assert estimate.records_its_own_usage(backend("acp", "python -m raven acp --config x"))
    assert not estimate.records_its_own_usage(backend("acp", "gemini --experimental-acp"))
    assert not estimate.records_its_own_usage(backend("cli", "claude -p {prompt}"))


def test_external_usage_is_written_only_by_an_accounts_engine(monkeypatch) -> None:
    cli = SimpleNamespace(kind="cli", command="claude -p")
    path = estimate.external_usage_path()

    estimate.note_external_usage(cli, "claude-code", "task", "answer", session_key="s")
    assert not path.exists(), "a Raven that is not an account's engine writes nothing"

    monkeypatch.setenv(gate.ENV_ACCOUNT, "a1")
    estimate.note_external_usage(cli, "claude-code", "task", "answer", session_key="s")
    estimate.note_external_usage(SimpleNamespace(kind="raven-loop"), "Raven", "t", "a", session_key="s")
    (row,) = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert row["agent"] == "claude-code" and row["estimated"] is True


# -- engines: environment and lent access ----------------------------------------------------


def test_an_accounts_engine_gets_none_of_the_servers_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "server-key")
    monkeypatch.setenv("GITHUB_TOKEN", "server-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "server-oauth")
    monkeypatch.setenv("PATH_LIKE_SETTING", "kept")
    account = Account(id="a1", username="bob")
    home = tmp_path / "home"

    env = env_for(account, home, "tok", server_home=tmp_path / "server")

    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["PATH_LIKE_SETTING"] == "kept"
    assert env["RAVEN_HOME"] == str(home) and env[gate.ENV_ACCOUNT] == "a1" and env["RAVEN_SERVE_TOKEN"] == "tok"
    assert env["CLAUDE_CONFIG_DIR"] == str(home / "cli" / "claude") and env["CODEX_HOME"] == str(home / "cli" / "codex")


def test_a_lent_subscription_points_the_engine_at_the_servers_login(tmp_path) -> None:
    account = Account(id="a1", username="bob", lent=["claude_subscription", "openai_codex"])
    server = tmp_path / "server"

    env = env_for(account, tmp_path / "home", "tok", server_home=server)

    assert "CLAUDE_CONFIG_DIR" not in env and "CODEX_HOME" not in env
    assert env["CHATGPT_TOKEN_DIR"] == str(server / "oauth" / "chatgpt")


def test_the_administrators_own_engine_keeps_its_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "server-key")
    admin = Account(id="r", username="root", role="superadmin", server_home=True)

    env = env_for(admin, raven_home(), "tok")

    assert env["OPENAI_API_KEY"] == "server-key"
    assert env.get("CLAUDE_CONFIG_DIR") == os.environ.get("CLAUDE_CONFIG_DIR"), "its own logins, as before"
    assert engine_home(admin) == raven_home()


def test_lending_copies_the_servers_provider_and_withdrawing_keeps_the_accounts_own(tmp_path) -> None:
    server = tmp_path / "server"
    _engine_config(server, {"openai": {"apiKey": "server-openai"}, "groq": {"apiKey": "server-groq"}})
    store = AccountStore()
    user = store.create("bob", "password1")
    home = user_home(user.id)
    _engine_config(home, {"anthropic": {"apiKey": "bobs-own"}})

    assert lendable(server) == ["groq", "openai", "claude_subscription", "openai_codex"]
    lent = store.update(user.id, lambda a: setattr(a, "lent", ["openai", "groq"]))
    assert apply_lent_providers(lent, home, server_home=server) == ["groq", "openai"]
    providers = json.loads((home / "config.json").read_text(encoding="utf-8"))["providers"]
    assert providers["openai"]["apiKey"] == "server-openai" and providers["anthropic"]["apiKey"] == "bobs-own"

    # Bob replaces the lent groq key with his own; withdrawing groq must not delete his.
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    config["providers"]["groq"] = {"apiKey": "bobs-groq"}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    withdrawn = store.update(user.id, lambda a: setattr(a, "lent", []))
    apply_lent_providers(withdrawn, home, server_home=server)
    providers = json.loads((home / "config.json").read_text(encoding="utf-8"))["providers"]
    assert "openai" not in providers and providers["groq"]["apiKey"] == "bobs-groq"


def test_a_local_recognizer_is_shared_and_a_keyed_one_is_not(tmp_path) -> None:
    from raven.accounts.engines import apply_server_transcription

    server = tmp_path / "server"
    server.mkdir()
    local = {
        "providers": {"gigaam": {"apiBase": "http://127.0.0.1:8805/v1"}},
        "transcription": {"provider": "gigaam", "language": "ru"},
    }
    (server / "config.json").write_text(json.dumps(local), encoding="utf-8")
    bob, carol = Account(id="b1", username="bob"), Account(id="c1", username="carol")
    bob_home, carol_home = tmp_path / "bob", tmp_path / "carol"
    _engine_config(carol_home, {})
    carol_cfg = json.loads((carol_home / "config.json").read_text(encoding="utf-8"))
    carol_cfg["transcription"] = {"provider": "openai"}
    (carol_home / "config.json").write_text(json.dumps(carol_cfg), encoding="utf-8")

    assert apply_server_transcription(bob, bob_home, server_home=server) is True
    assert apply_server_transcription(carol, carol_home, server_home=server) is False, "her own choice stays"
    copied = json.loads((bob_home / "config.json").read_text(encoding="utf-8"))
    assert copied["transcription"]["provider"] == "gigaam"
    assert copied["providers"]["gigaam"] == {"apiBase": "http://127.0.0.1:8805/v1"}

    keyed = {
        "providers": {"groq": {"apiKey": "server-key", "apiBase": "https://api.groq.com/openai/v1"}},
        "transcription": {"provider": "groq"},
    }
    (server / "config.json").write_text(json.dumps(keyed), encoding="utf-8")
    dave_home = tmp_path / "dave"
    assert apply_server_transcription(Account(id="d1", username="dave"), dave_home, server_home=server) is False
    assert not (dave_home / "config.json").exists(), "a recognizer behind the server's key is never copied"


def test_billing_module_names_cli_subscriptions() -> None:
    assert billing.cli_provider("claude-code") == "claude_subscription"
    assert billing.cli_provider("codex") == "openai_codex"
    assert billing.cli_provider("gemini") == "cli"

"""The Rarus provider: how routing sees it, when it is usable, and where its key comes from."""

from __future__ import annotations

import pytest

from raven.config.schema import Config
from raven.providers import rarus_client, rarus_session
from raven.providers.auth import MissingCredentialsError, credential_status
from raven.providers.factory import make_provider
from raven.providers.rarus_provider import RarusProvider
from raven.providers.registry import find_by_name

KEY = "sk-ab-" + "fedcba9876543210" * 3


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """A home of the test's own: on Windows ``Path.home()`` ignores the suite's ``HOME``."""
    monkeypatch.setenv("RARUS_HOME", str(tmp_path / "rarus-home"))
    monkeypatch.setenv(rarus_session.STORE_ENV, "plain")


def _config() -> Config:
    config = Config()
    config.agents.defaults.model = "rarus/deepseek-flash"
    config.agents.defaults.provider = "rarus"
    return config


class TestTheRegistryEntry:
    def test_the_spec_is_a_signed_in_gateway_over_the_openai_wire(self):
        spec = find_by_name("rarus")
        assert spec is not None
        assert spec.client == "rarus"
        assert spec.is_oauth
        assert spec.via_driver == "openai"
        assert spec.strip_model_prefix
        assert spec.default_api_base == "https://alembox.com/api/agent/v1"

    def test_the_section_exists_with_no_key_in_it(self):
        assert Config().providers.rarus.api_key == ""


class TestWhenItIsUsable:
    def test_it_is_unusable_until_a_sign_in_exists(self):
        status = credential_status("rarus", Config().providers.rarus, include_external=True)
        assert not status.ok
        assert "rarus provider login rarus" in str(
            status.missing[0].remedy if hasattr(status.missing[0], "remedy") else status
        )

    def test_it_is_usable_after_signing_in(self):
        rarus_session.save_session(KEY)
        assert credential_status("rarus", Config().providers.rarus, include_external=True).ok

    def test_signing_out_makes_it_unusable_again(self):
        rarus_session.save_session(KEY)
        rarus_session.delete_session()
        assert not credential_status("rarus", Config().providers.rarus, include_external=True).ok


class TestTheProviderObject:
    def test_the_factory_builds_it_from_a_signed_in_config(self):
        rarus_session.save_session(KEY)
        provider = make_provider(_config())
        assert isinstance(provider, RarusProvider)
        assert provider.api_base == "https://alembox.com/api/agent/v1"

    def test_the_factory_refuses_a_config_that_is_not_signed_in(self):
        with pytest.raises(MissingCredentialsError):
            make_provider(_config())

    async def test_a_turn_without_a_sign_in_names_the_remedy(self):
        provider = RarusProvider(default_model="rarus/deepseek-flash")
        with pytest.raises(MissingCredentialsError) as raised:
            await provider.chat([{"role": "user", "content": "hi"}])
        assert "provider login rarus" in raised.value.remedy

    async def test_a_turn_uses_the_stored_key_and_not_the_config(self, monkeypatch):
        rarus_session.save_session(KEY)
        provider = RarusProvider(default_model="rarus/deepseek-flash")
        seen: list[str | None] = []

        async def fake_chat(self, *args, **kwargs):
            seen.append(self.api_key)
            return "answer"

        monkeypatch.setattr("raven.providers.litellm_provider.LiteLLMProvider.chat", fake_chat)

        assert await provider.chat([{"role": "user", "content": "hi"}]) == "answer"
        assert seen == [KEY]

    async def test_the_key_is_read_again_each_turn_so_a_sign_out_takes_effect(self, monkeypatch):
        rarus_session.save_session(KEY)
        provider = RarusProvider(default_model="rarus/deepseek-flash")

        async def fake_chat(self, *args, **kwargs):
            return "answer"

        monkeypatch.setattr("raven.providers.litellm_provider.LiteLLMProvider.chat", fake_chat)
        assert await provider.chat([]) == "answer"

        rarus_session.delete_session()
        with pytest.raises(MissingCredentialsError):
            await provider.chat([])


class TestTheConnectionCheck:
    def test_not_signed_in_is_reported_as_a_missing_sign_in(self):
        from raven.config.update_providers import _probe_rarus

        assert _probe_rarus(api_base=None)["status"] == "oauth_token_missing"

    def test_a_signed_in_key_lists_the_servers_models(self, monkeypatch):
        from raven.config.update_providers import _probe_rarus

        rarus_session.save_session(KEY)
        monkeypatch.setattr(rarus_client, "models", lambda key, site=None, client=None: ["deepseek-flash", "auto"])

        result = _probe_rarus(api_base="https://alembox.com/api/agent/v1")

        assert result["ok"]
        assert result["model_ids"] == ["deepseek-flash", "auto"]

    @pytest.mark.parametrize(
        ("code", "status"),
        [("unauthorized", "invalid_key"), ("no_credits", "quota"), ("network", "network_error")],
    )
    def test_refusals_are_translated(self, monkeypatch, code, status):
        from raven.config.update_providers import _probe_rarus

        rarus_session.save_session(KEY)

        def refuse(key, site=None, client=None):
            raise rarus_client.RarusError(code, "no")

        monkeypatch.setattr(rarus_client, "models", refuse)

        result = _probe_rarus(api_base=None)

        assert not result["ok"]
        assert result["status"] == status

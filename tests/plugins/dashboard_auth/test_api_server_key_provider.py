"""Tests for the ApiServerKeyTokenProvider plugin (dashboard token auth backed by the
existing API_SERVER_KEY, registered narrowly for the Settings/Memory/Skills/Cron JSON routes).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import plugins.dashboard_auth.api_server_key as api_key_plugin
from hermes_cli.dashboard_auth import assert_protocol_compliance
from hermes_cli.dashboard_auth.token_auth import clear_token_routes, is_token_route


@pytest.fixture(scope="module")
def plugin():
    return api_key_plugin


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    clear_token_routes()
    yield
    clear_token_routes()


class TestProvider:
    def test_protocol_compliant(self, plugin):
        assert_protocol_compliance(plugin.ApiServerKeyTokenProvider)

    def test_supports_token_true_supports_session_false(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="secret-key")
        assert provider.supports_token is True
        assert provider.supports_session is False
        assert provider.supports_password is False

    def test_construction_requires_nonempty_key(self, plugin):
        with pytest.raises(ValueError):
            plugin.ApiServerKeyTokenProvider(api_key="")

    def test_verify_token_accepts_matching_key(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="the-real-key")
        principal = provider.verify_token(token="the-real-key")
        assert principal is not None
        assert principal.provider == "api_server_key"

    def test_verify_token_rejects_wrong_key(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="the-real-key")
        assert provider.verify_token(token="wrong-key") is None

    def test_verify_token_rejects_empty_token(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="the-real-key")
        assert provider.verify_token(token="") is None

    def test_oauth_methods_raise_not_implemented(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="the-real-key")
        with pytest.raises(NotImplementedError):
            provider.start_login(redirect_uri="https://example.com/auth/callback")
        with pytest.raises(NotImplementedError):
            provider.complete_login(
                code="c", state="s", code_verifier="v",
                redirect_uri="https://example.com/auth/callback")

    def test_verify_session_returns_none(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="the-real-key")
        assert provider.verify_session(access_token="anything") is None

    def test_revoke_session_is_silent(self, plugin):
        provider = plugin.ApiServerKeyTokenProvider(api_key="the-real-key")
        provider.revoke_session(refresh_token="anything")  # must not raise


class TestRegister:
    def test_skips_when_no_api_server_key(self, plugin, monkeypatch):
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_not_called()
        assert plugin.LAST_SKIP_REASON
        assert not is_token_route("/api/model/info")

    def test_registers_with_env_api_server_key(self, plugin, monkeypatch):
        monkeypatch.setenv("API_SERVER_KEY", "env-key-value")
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_called_once()
        registered = ctx.register_dashboard_auth_provider.call_args[0][0]
        assert registered.verify_token(token="env-key-value") is not None
        assert plugin.LAST_SKIP_REASON == ""

    def test_registers_narrow_route_allowlist_only(self, plugin, monkeypatch):
        """Regression: this provider must widen the Bearer-auth surface to an explicit,
        small set of JSON routes — never a prefix/wildcard that would also cover session
        pages, WS ticket minting, or unrelated mutation endpoints."""
        monkeypatch.setenv("API_SERVER_KEY", "env-key-value")
        plugin.register(MagicMock())
        assert is_token_route("/api/model/info")
        assert is_token_route("/api/skills")
        assert is_token_route("/api/cron/jobs")
        assert is_token_route("/api/sessions")
        # Never widened to unrelated dashboard surfaces.
        assert not is_token_route("/api/auth/ws-ticket")
        assert not is_token_route("/")
        assert not is_token_route("/api/env/reveal")

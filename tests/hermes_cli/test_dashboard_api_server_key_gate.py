"""End-to-end guard: a native client (Hermes Android, hermes-desktop) holding a valid
API_SERVER_KEY must reach the Dashboard's JSON API routes (/api/model/info, /api/skills,
/api/cron/jobs, ...) on a GATED dashboard (auth_required=True, no browser cookie) — the exact
scenario this provider was written to close. Exercises the real FastAPI app, the real
token_auth_middleware + gated_auth_middleware stack, and the real api_server_key plugin's
register() entry point — not a mocked provider.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import plugins.dashboard_auth.api_server_key as api_key_plugin
from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.token_auth import clear_token_routes


class _RecordingCtx:
    def __init__(self):
        self.provider = None

    def register_dashboard_auth_provider(self, provider):
        self.provider = provider
        from hermes_cli.dashboard_auth import register_provider

        register_provider(provider)


@pytest.fixture
def gated_app_with_api_server_key(monkeypatch):
    """A gated dashboard with the api_server_key provider registered for real, no session
    cookie set — the exact shape a mobile client without a browser presents."""
    monkeypatch.setenv("API_SERVER_KEY", "the-real-api-server-key")
    clear_providers()
    clear_token_routes()
    ctx = _RecordingCtx()
    api_key_plugin.register(ctx)

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "srlmm004-hermes.costalavosapp.com.br"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(
        web_server.app, base_url="https://srlmm004-hermes.costalavosapp.com.br")
    yield client
    clear_providers()
    clear_token_routes()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


class TestApiServerKeyReachesGatedDashboardRoutes:
    def test_correct_bearer_key_reaches_model_info_with_no_cookie(
        self, gated_app_with_api_server_key):
        r = gated_app_with_api_server_key.get(
            "/api/model/info", headers={"Authorization": "Bearer the-real-api-server-key"})
        assert r.status_code == 200, r.text

    def test_wrong_bearer_key_still_401s(self, gated_app_with_api_server_key):
        r = gated_app_with_api_server_key.get(
            "/api/model/info", headers={"Authorization": "Bearer wrong-key"})
        assert r.status_code == 401

    def test_no_bearer_still_401s_gated_route(self, gated_app_with_api_server_key):
        r = gated_app_with_api_server_key.get("/api/model/info")
        assert r.status_code == 401

    def test_bearer_key_does_not_widen_unrelated_gated_endpoint(
        self, gated_app_with_api_server_key):
        """Regression: this provider must not become a skeleton key for the whole gated
        dashboard — only the explicitly registered routes accept it."""
        r = gated_app_with_api_server_key.post(
            "/api/env/reveal", json={"key": "OPENAI_API_KEY"},
            headers={"Authorization": "Bearer the-real-api-server-key"})
        assert r.status_code == 401

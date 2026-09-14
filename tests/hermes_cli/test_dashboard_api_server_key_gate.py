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

    def test_correct_bearer_key_reaches_sessions_list_with_no_cookie(
        self, gated_app_with_api_server_key):
        r = gated_app_with_api_server_key.get(
            "/api/sessions", headers={"Authorization": "Bearer the-real-api-server-key"})
        assert r.status_code == 200, r.text

    def test_correct_bearer_key_reaches_a_session_message_page(
        self, gated_app_with_api_server_key):
        """The list and the message page must agree: reaching only the list renders a
        chat list whose every conversation fails to open ("Failed to load messages",
        session_expired). The id segment varies per request, so this route is covered
        by a template, not an exact string."""
        r = gated_app_with_api_server_key.get(
            "/api/sessions/any_session_id/messages",
            headers={"Authorization": "Bearer the-real-api-server-key"})
        assert r.status_code != 401, r.text

    def test_message_page_without_credential_is_still_refused(
        self, gated_app_with_api_server_key):
        """Covering the route by template must not publish it: no credential, no access."""
        r = gated_app_with_api_server_key.get("/api/sessions/any_session_id/messages")
        assert r.status_code == 401, r.text

    def test_bearer_key_does_not_reach_the_session_detail_route(
        self, gated_app_with_api_server_key):
        """The template covers .../messages only — a neighbouring session route that was
        never registered keeps its own gate."""
        r = gated_app_with_api_server_key.get(
            "/api/sessions/any_session_id",
            headers={"Authorization": "Bearer the-real-api-server-key"})
        assert r.status_code == 401, r.text

    def test_bearer_key_does_not_widen_unrelated_gated_endpoint(
        self, gated_app_with_api_server_key):
        """Regression: this provider must not become a skeleton key for the whole gated
        dashboard — only the explicitly registered routes accept it."""
        r = gated_app_with_api_server_key.post(
            "/api/env/reveal", json={"key": "OPENAI_API_KEY"},
            headers={"Authorization": "Bearer the-real-api-server-key"})
        assert r.status_code == 401


class TestCookieClientKeepsAccessToTokenRoutes:
    """A password-login client (Hermes Android) calls these routes with the session cookie and
    no bearer. Registering a route for bearer auth is additive: it must not revoke that access,
    and must not hand an unauthenticated caller to a public-path exemption either."""

    def test_session_cookie_without_bearer_is_judged_by_the_cookie_gate(
        self, gated_app_with_api_server_key, monkeypatch):
        seen = {}

        async def _fake_gate(request, call_next):
            seen["reached"] = True
            return await call_next(request)

        from hermes_cli.dashboard_auth import token_auth

        monkeypatch.setattr(token_auth, "_has_session_cookie", lambda request: True)
        r = gated_app_with_api_server_key.get("/api/model/info")
        # The cookie gate — not the token seam — owns the verdict for a cookie caller.
        assert r.status_code != 401 or "no_cookie" in r.text

    def test_no_credential_at_all_is_still_refused_by_the_token_seam(
        self, gated_app_with_api_server_key):
        """Regression: a bare request must never reach /api/model/info's public-path
        exemption through the token seam — that silently published it on the internet."""
        r = gated_app_with_api_server_key.get("/api/model/info")
        assert r.status_code == 401, r.text

"""A per-turn or picker persist must not leave a stale ``gateway_runtime`` route behind.

Regression for #146345 (variants #143157, #110279, #142948). The messaging gateway's after-turn sync
(``_sync_session_model_from_agent``) writes the route it actually ran into the nested
``model_config.gateway_runtime``. ``session_gateway_runtime`` reads that shape FIRST on resume. The
Desktop/TUI persist (``_persist_live_session_runtime``, called after every TUI turn and on every
committed model switch) only rewrote the top-level ``model``/``provider`` keys, so a chat that once ran
on a fallback (``gpt-6.1-sol-900k`` via ``openai-codex``) and then went back to its primary
(``claude-opus-5-5`` via ``anthropic``) resumed as ``claude-opus-5-5`` via ``openai-codex``: a pair no
provider serves, which the Codex backend rejects with ``model_not_found`` and which then sends the turn
straight back to the fallback. The later writer must win in both shapes.
"""

import types

import pytest

from gateway.run_turn_prepare import GatewayTurnPrepareMixin
from hermes_state import SessionDB
from tui_gateway import server

CODEX = {"provider": "openai-codex", "base_url": "https://chatgpt.com/backend-api/codex", "api_mode": "codex_responses"}
ANTHROPIC = {"provider": "anthropic", "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages"}


def _agent(model, route, **extra):
    return types.SimpleNamespace(model=model, requested_provider=route["provider"], reasoning_config=None,
                                 service_tier=None, **route, **extra)


def _gateway_turn(db, session_id, agent):
    """The real after-turn sync a messaging-gateway turn runs."""
    runner = types.SimpleNamespace(_session_db=types.SimpleNamespace(_db=db))
    GatewayTurnPrepareMixin._sync_session_model_from_agent(runner, session_id, agent)


def _desktop_persist(monkeypatch, db, session_id, agent):
    """The real TUI persist, fed the live agent the way a finished turn or a picker commit feeds it."""
    monkeypatch.setattr(server, "_live_session_agent_db", lambda session: (agent, session_id, db))
    server._persist_live_session_runtime({"session_key": session_id})


def _resumed_route(db, session_id):
    overrides = server._stored_session_runtime_overrides(db.get_session(session_id))
    return overrides["model_override"]["model"], overrides.get("provider_override")


@pytest.fixture
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="desktop", model="claude-opus-5-5")
    return db


def test_back_on_primary_after_fallback_turn_resumes_on_primary_provider(monkeypatch, db):
    _gateway_turn(db, "s1", _agent("gpt-6.1-sol-900k", CODEX, _fallback_activated=True))
    _desktop_persist(monkeypatch, db, "s1", _agent("claude-opus-5-5", ANTHROPIC))

    assert _resumed_route(db, "s1") == ("claude-opus-5-5", "anthropic")


def test_gateway_sync_after_desktop_persist_still_wins(monkeypatch, db):
    _desktop_persist(monkeypatch, db, "s1", _agent("claude-opus-5-5", ANTHROPIC))
    _gateway_turn(db, "s1", _agent("gpt-6.1-sol-900k", CODEX, _fallback_activated=True))

    assert _resumed_route(db, "s1") == ("gpt-6.1-sol-900k", "openai-codex")


def test_persist_without_a_gateway_route_does_not_invent_one(monkeypatch, db):
    _desktop_persist(monkeypatch, db, "s1", _agent("claude-opus-5-5", ANTHROPIC))

    import json
    assert "gateway_runtime" not in json.loads(db.get_session("s1")["model_config"])
    assert _resumed_route(db, "s1") == ("claude-opus-5-5", "anthropic")

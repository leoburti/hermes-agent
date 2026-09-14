"""REST clients reading the ``data`` envelope must see the same rows as ``sessions``/``messages``.

The Android client (rusty4444/hermes-android) parses both session-listing and
message-page responses as ``json['data'] as List? ?? []``.  A missing key is not an
error there — it silently renders an empty chat list ("Nothing Here") on a fully
authenticated connection, which is indistinguishable from "this account has no
sessions".  These are contract tests between the two keys: whatever the dashboard
serves under its own key must also be reachable under ``data``.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def session_app(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id="chat-1", source="desktop")
        db.append_messages_batch("chat-1", [
            {"role": "user", "content": "primeira"},
            {"role": "assistant", "content": "resposta"},
        ])
    finally:
        db.close()

    from hermes_cli.web_routers.sessions import list_router, manage_router

    app = FastAPI()
    app.include_router(list_router)
    app.include_router(manage_router)
    return app


def test_session_listing_exposes_rows_under_both_keys(session_app):
    with TestClient(session_app) as client:
        body = client.get("/api/sessions?min_messages=0").json()

    # The dashboard SPA/Desktop key; the REST/mobile key. Same rows, not a copy
    # that can drift: a client reading either one sees the whole page.
    assert body["sessions"], "fixture session should be listed"
    assert body["data"] == body["sessions"]


def test_message_page_exposes_rows_under_both_keys(session_app):
    with TestClient(session_app) as client:
        body = client.get("/api/sessions/chat-1/messages").json()

    assert [row["content"] for row in body["messages"]] == ["primeira", "resposta"]
    assert body["data"] == body["messages"]

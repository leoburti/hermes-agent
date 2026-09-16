"""TurnRunner._clarify_callback_sync must hand send_clarify the stored session title.

_status_thread_metadata carries only thread routing, so without this wiring the
adapter never sees a title and the clarify prompt goes out unlabeled.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_send_clarify_receives_stored_session_title(monkeypatch):
    from gateway.run_turn_runner import TurnRunner

    sent = {}

    class _Adapter:
        def pause_typing_for_chat(self, chat_id): return None
        def resume_typing_for_chat(self, chat_id): return None

        async def send_clarify(self, **kwargs):
            sent.update(kwargs)

    thread_metadata = {"thread_id": "9"}
    db = SimpleNamespace(get_session_title=MagicMock(return_value="Deploy DirectData"))
    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        _status_adapter=_Adapter(), _status_chat_id="C1", _status_thread_metadata=thread_metadata,
        session_key="sk1", _session_db=db,
    )
    runner._schedule = lambda coro, _label: asyncio.run(coro)
    runner._close_native_stream_boundary = lambda *a, **k: None
    runner._stream_consumer = lambda: None
    monkeypatch.setattr("gateway.run._clarify_send_then_wait", lambda *a, **k: "[timeout]")

    runner._clarify_callback_sync("Which option?", ["a", "b"])

    assert sent["metadata"] == {"thread_id": "9", "session_title": "Deploy DirectData"}
    db.get_session_title.assert_called_once_with("sk1")
    assert thread_metadata == {"thread_id": "9"}  # the ctx's routing dict is copied, not mutated

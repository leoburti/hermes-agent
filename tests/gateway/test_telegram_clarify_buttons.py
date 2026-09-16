"""Tests for Telegram clarify prompts.

Choices render as a native, non-anonymous Telegram poll whose last option flips the
clarify into text capture; the vote arrives as a ``PollAnswer`` update. The legacy
``cl:`` inline-button callback stays wired for prompts sent before the poll switch.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from types import SimpleNamespace

import plugins.platforms.telegram.adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig

OTHER = "✏️ Outra — vou responder em texto"


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _poll_message(poll_id="poll-1", message_id=100, chat_type="private"):
    msg = MagicMock()
    msg.message_id = message_id
    msg.poll.id = poll_id
    msg.chat.type = chat_type
    return msg


async def _send_poll_clarify(adapter, choices, clarify_id="cid1", session_key="sk1", metadata=None, poll_id="poll-1"):
    adapter._bot.send_poll = AsyncMock(return_value=_poll_message(poll_id))
    return await adapter.send_clarify(
        chat_id="12345", question="Which option?", choices=choices,
        clarify_id=clarify_id, session_key=session_key, metadata=metadata,
    )


def _vote_update(option_ids, poll_id="poll-1", user_id=777):
    # Shape of telegram.Update carrying a PollAnswer (the telegram package is mocked in tests/gateway).
    answer = SimpleNamespace(poll_id=poll_id, option_ids=list(option_ids),
                             user=SimpleNamespace(id=user_id, first_name="Tester"), voter_chat=None)
    return SimpleNamespace(poll_answer=answer)


def _entry(clarify_id):
    from tools import clarify_gateway as cm
    with cm._lock:
        return cm._entries.get(clarify_id)


# ===========================================================================
# send_clarify — native poll render
# ===========================================================================

class TestTelegramSendClarifyPoll:

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_choices_send_nonanonymous_poll_with_other_option(self):
        from tools import clarify_gateway as cm
        cm.register("cid1", "sk1", "Which option?", ["alpha", "beta", "gamma"])
        adapter = _make_adapter()

        result = await _send_poll_clarify(adapter, ["alpha", "beta", "gamma"])

        assert result.success is True
        assert result.message_id == "100"
        adapter._bot.send_message.assert_not_called()
        kwargs = adapter._bot.send_poll.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert kwargs["question"] == "Which option?"
        assert kwargs["options"] == ["alpha", "beta", "gamma", OTHER]
        assert kwargs["is_anonymous"] is False
        assert kwargs["allows_multiple_answers"] is False
        state = adapter._clarify_polls["poll-1"]
        assert state["clarify_id"] == "cid1"
        assert state["session_key"] == "sk1"
        assert state["choices"] == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_multi_select_maps_to_allows_multiple_answers(self):
        from tools import clarify_gateway as cm
        cm.register("cidM", "skM", "Which envs?", ["dev", "staging", "prod"], multi_select=True)
        adapter = _make_adapter()

        await _send_poll_clarify(adapter, ["dev", "staging", "prod"], clarify_id="cidM", session_key="skM")

        assert adapter._bot.send_poll.call_args[1]["allows_multiple_answers"] is True

    @pytest.mark.asyncio
    async def test_session_title_from_metadata_labels_question(self):
        adapter = _make_adapter()

        await _send_poll_clarify(adapter, ["a", "b"], metadata={"session_title": "Deploy DirectData"})

        question = adapter._bot.send_poll.call_args[1]["question"]
        assert "Deploy DirectData" in question
        assert "Which option?" in question
        assert len(question) <= 300

    @pytest.mark.asyncio
    async def test_long_option_truncated_to_100_chars(self):
        adapter = _make_adapter()
        long_choice = "x" * 150

        await _send_poll_clarify(adapter, [long_choice, "short"])

        options = adapter._bot.send_poll.call_args[1]["options"]
        assert all(len(o) <= 100 for o in options)
        assert options[1] == "short"
        assert adapter._clarify_polls["poll-1"]["choices"][0] == long_choice

    @pytest.mark.asyncio
    async def test_more_than_nine_choices_rejected_without_sending(self):
        adapter = _make_adapter()
        adapter._bot.send_poll = AsyncMock()

        result = await adapter.send_clarify("12345", "Pick", [str(i) for i in range(10)], "cidX", "skX")

        assert result.success is False
        adapter._bot.send_poll.assert_not_called()
        adapter._bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_failure_falls_back_to_text_capture(self):
        from tools import clarify_gateway as cm
        cm.register("cidF", "skF", "Pick", ["alpha", "beta"])
        adapter = _make_adapter()
        adapter._bot.send_poll = AsyncMock(side_effect=RuntimeError("network down"))
        sent_msg = MagicMock()
        sent_msg.message_id = 101
        adapter._bot.send_message = AsyncMock(return_value=sent_msg)

        result = await adapter.send_clarify("12345", "Pick", ["alpha", "beta"], "cidF", "skF")

        assert result.success is True
        text = adapter._bot.send_message.call_args[1]["text"]
        assert "alpha" in text and "beta" in text
        assert _entry("cidF").awaiting_text is True
        assert adapter._clarify_polls == {}

    @pytest.mark.asyncio
    async def test_markup_in_question_sent_verbatim_as_plain_text(self):
        adapter = _make_adapter()
        adapter._bot.send_poll = AsyncMock(return_value=_poll_message())

        await adapter.send_clarify("12345", "<script>alert(1)</script>", ["a", "b"], "cid5", "sk5")

        # Poll questions are plain text (no parse mode): markup is shown literally, never interpreted.
        kwargs = adapter._bot.send_poll.call_args[1]
        assert kwargs["question"] == "<script>alert(1)</script>"
        assert "question_parse_mode" not in kwargs

    def test_poll_answer_handler_registered(self):
        adapter = _make_adapter()
        app = MagicMock()

        with patch.object(tg_adapter, "PollAnswerHandler", lambda cb: ("poll_answer", cb)):
            adapter._register_handlers(app)

        handlers = [c.args[0] for c in app.add_handler.call_args_list]
        assert ("poll_answer", adapter._handle_clarify_poll_answer) in handlers


# ===========================================================================
# PollAnswer — vote resolves the clarify
# ===========================================================================

class TestTelegramClarifyPollAnswer:

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_normal_vote_resolves_with_original_choice_text(self):
        from tools import clarify_gateway as cm
        long_choice = "y" * 150
        cm.register("cidA", "skA", "Pick", ["red", long_choice])
        adapter = _make_adapter()
        await _send_poll_clarify(adapter, ["red", long_choice], clarify_id="cidA", session_key="skA")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_clarify_poll_answer(_vote_update([1]), MagicMock())

        entry = _entry("cidA")
        assert entry.event.is_set()
        assert entry.response == long_choice
        assert "poll-1" not in adapter._clarify_polls

    @pytest.mark.asyncio
    async def test_other_vote_marks_awaiting_text(self):
        from tools import clarify_gateway as cm
        cm.register("cidO", "skO", "Pick", ["a", "b"])
        adapter = _make_adapter()
        await _send_poll_clarify(adapter, ["a", "b"], clarify_id="cidO", session_key="skO")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_clarify_poll_answer(_vote_update([2]), MagicMock())

        entry = _entry("cidO")
        assert entry.awaiting_text is True
        assert not entry.event.is_set()

    @pytest.mark.asyncio
    async def test_retracted_vote_ignored(self):
        from tools import clarify_gateway as cm
        cm.register("cidR", "skR", "Pick", ["a", "b"])
        adapter = _make_adapter()
        await _send_poll_clarify(adapter, ["a", "b"], clarify_id="cidR", session_key="skR")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_clarify_poll_answer(_vote_update([]), MagicMock())

        entry = _entry("cidR")
        assert not entry.event.is_set()
        assert entry.awaiting_text is False
        assert "poll-1" in adapter._clarify_polls

    @pytest.mark.asyncio
    async def test_unknown_poll_id_ignored(self):
        from tools import clarify_gateway as cm
        cm.register("cidU", "skU", "Pick", ["a", "b"])
        adapter = _make_adapter()
        await _send_poll_clarify(adapter, ["a", "b"], clarify_id="cidU", session_key="skU")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_clarify_poll_answer(_vote_update([0], poll_id="someone-elses-poll"), MagicMock())

        assert not _entry("cidU").event.is_set()
        assert "poll-1" in adapter._clarify_polls

    @pytest.mark.asyncio
    async def test_multi_select_vote_resolves_json_array_of_choices(self):
        from tools import clarify_gateway as cm
        cm.register("cidMS", "skMS", "Envs", ["dev", "staging", "prod"], multi_select=True)
        adapter = _make_adapter()
        await _send_poll_clarify(adapter, ["dev", "staging", "prod"], clarify_id="cidMS", session_key="skMS")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_clarify_poll_answer(_vote_update([0, 2]), MagicMock())

        assert json.loads(_entry("cidMS").response) == ["dev", "prod"]

    @pytest.mark.asyncio
    async def test_unauthorized_voter_ignored(self):
        from tools import clarify_gateway as cm
        cm.register("cidD", "skD", "Pick", ["a", "b"])
        adapter = _make_adapter()
        await _send_poll_clarify(adapter, ["a", "b"], clarify_id="cidD", session_key="skD")

        class _DenyRunner:
            async def _handle_message(self, event):
                return None

            def _is_user_authorized(self, source):
                return False

        adapter._message_handler = _DenyRunner()._handle_message

        await adapter._handle_clarify_poll_answer(_vote_update([0], user_id=999), MagicMock())

        assert not _entry("cidD").event.is_set()
        assert "poll-1" in adapter._clarify_polls


# ===========================================================================
# Legacy cl: callback — prompts sent as inline buttons before the poll switch
# ===========================================================================

class TestTelegramClarifyCallback:
    """Verify clicking a button resolves the clarify primitive."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_numeric_choice_resolves_with_choice_text(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cidA", "sk-cb", "Pick", ["red", "green", "blue"])
        adapter._clarify_state["cidA"] = "sk-cb"

        query = AsyncMock()
        query.data = "cl:cidA:1"  # green
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        assert "cidA" not in adapter._clarify_state
        entry = _entry("cidA")
        assert entry is not None
        assert entry.response == "green"
        assert entry.event.is_set()
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cidC", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_state["cidC"] = "sk-auth"

        class _DenyRunner:
            async def _handle_message(self, event):
                return None
            def _is_user_authorized(self, source):
                return False

        adapter._message_handler = _DenyRunner()._handle_message

        query = AsyncMock()
        query.data = "cl:cidC:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "999"
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        entry = _entry("cidC")
        assert entry is not None
        assert not entry.event.is_set()
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        assert adapter._clarify_state["cidC"] == "sk-auth"


# ===========================================================================
# Base adapter fallback render — text numbered list
# ===========================================================================

class TestBaseAdapterClarifyFallback:
    """Adapters without button overrides should render numbered text."""

    @pytest.mark.asyncio
    async def test_numbered_text_fallback(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        class _Stub(BasePlatformAdapter):
            name = "stub"

            def __init__(self):
                self.sent: list = []

            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append({"chat_id": chat_id, "content": content})
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()

        result = await adapter.send_clarify(
            chat_id="c",
            question="Pick a fruit",
            choices=["apple", "banana"],
            clarify_id="x",
            session_key="s",
        )
        assert result.success is True
        assert len(adapter.sent) == 1
        text = adapter.sent[0]["content"]
        assert "Pick a fruit" in text
        assert "1." in text and "apple" in text
        assert "2." in text and "banana" in text

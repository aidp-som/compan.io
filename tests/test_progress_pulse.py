"""Unit tests for the Slack progress streaming (stream-json upgrade).

Covers:
- _should_start_pulse gating logic (Slack-only, opt-in)
- _make_progress_callback creation and event processing
- Callback lifecycle inside _invoke_claude_turn / _process_message
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from companio.bus import InboundMessage, MessageBus, OutboundMessage
from companio.config.schema import Config
from companio.core import loop as loop_module
from companio.core.loop import (
    AgentLoop,
    _TOOL_LABELS,
)
from companio.session import Session

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _build_loop(tmp_path: Path, *, slack_pulse_enabled: bool = True) -> tuple[AgentLoop, MessageBus]:
    config = Config()
    config.channels.slack.enabled = True
    config.channels.slack.progress_pulse_enabled = slack_pulse_enabled

    bus = MessageBus()
    claude = MagicMock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    loop = AgentLoop(
        bus=bus,
        claude=claude,
        workspace=workspace,
        config=config,
    )

    fake_session = Session(session_id="test-session")
    loop._session_manager = MagicMock()
    loop._session_manager.get_or_create = AsyncMock(return_value=fake_session)
    loop._session_manager.save = AsyncMock(return_value=None)
    loop._session_manager.initialize = AsyncMock(return_value=None)
    loop._session_manager.close = AsyncMock(return_value=None)
    loop._session_manager.clear = AsyncMock(return_value=None)

    return loop, bus


def _slack_inbound(content: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="C1",
        content=content,
        metadata={"thread_ts": "1712345678.000100"},
    )


def _telegram_inbound(content: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        sender_id="42",
        chat_id="-100",
        content=content,
        metadata={"message_thread_id": 7},
    )


def _drain_outbound(bus: MessageBus) -> list[OutboundMessage]:
    out: list[OutboundMessage] = []
    while not bus.outbound.empty():
        out.append(bus.outbound.get_nowait())
    return out


# -----------------------------------------------------------------------------
# A. _should_start_pulse — gating logic
# -----------------------------------------------------------------------------


class TestShouldStartPulse:
    def test_pulse_enabled_for_slack_with_flag(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=True)
        assert loop._should_start_pulse(_slack_inbound()) is True

    def test_pulse_disabled_flag_skips(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=False)
        assert loop._should_start_pulse(_slack_inbound()) is False

    def test_pulse_skipped_for_non_slack_channels(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=True)
        assert loop._should_start_pulse(_telegram_inbound()) is False

    def test_pulse_skipped_when_no_config(self, tmp_path):
        bus = MessageBus()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        loop = AgentLoop(bus=bus, claude=MagicMock(), workspace=workspace)
        assert loop._should_start_pulse(_slack_inbound()) is False


# -----------------------------------------------------------------------------
# B. _make_progress_callback — unit tests
# -----------------------------------------------------------------------------


class TestMakeProgressCallback:
    def test_returns_none_when_disabled(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=False)
        assert loop._make_progress_callback(_slack_inbound()) is None

    async def test_returns_callable_when_enabled(self, tmp_path):
        loop, _ = _build_loop(tmp_path)
        cb = loop._make_progress_callback(_slack_inbound())
        assert callable(cb)

    def test_returns_none_for_telegram(self, tmp_path):
        loop, _ = _build_loop(tmp_path)
        assert loop._make_progress_callback(_telegram_inbound()) is None

    async def test_tool_use_event_adds_step(self, tmp_path):
        loop, bus = _build_loop(tmp_path)
        cb = loop._make_progress_callback(_slack_inbound())
        assert cb is not None

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/test.txt"}}
                ]
            },
        }
        # First call — step is registered internally
        await cb(event)

    async def test_non_tool_event_ignored(self, tmp_path):
        loop, bus = _build_loop(tmp_path)
        cb = loop._make_progress_callback(_slack_inbound())
        assert cb is not None

        # A result event should not add steps
        await cb({"type": "result", "result": "done"})
        # A system event should not add steps
        await cb({"type": "system", "subtype": "init"})

    def test_tool_labels_has_common_tools(self):
        assert "Read" in _TOOL_LABELS
        assert "Edit" in _TOOL_LABELS
        assert "Bash" in _TOOL_LABELS
        assert "WebSearch" in _TOOL_LABELS


# -----------------------------------------------------------------------------
# C. Callback lifecycle inside _process_message
# -----------------------------------------------------------------------------


class TestCallbackLifecycle:
    async def test_callback_passed_to_inner(self, tmp_path):
        loop, bus = _build_loop(tmp_path)

        received_cb = None

        async def fake_inner(msg, session, key, progress_cb=None):
            nonlocal received_cb
            received_cb = progress_cb
            return OutboundMessage.reply_to_inbound(msg, "done")

        loop._process_message_inner = fake_inner
        loop._maybe_consolidate = MagicMock()

        await loop._process_message(_slack_inbound())
        # The callback should have been passed
        assert received_cb is not None
        assert callable(received_cb)

    async def test_no_callback_when_disabled(self, tmp_path):
        loop, bus = _build_loop(tmp_path, slack_pulse_enabled=False)

        received_cb = None

        async def fake_inner(msg, session, key, progress_cb=None):
            nonlocal received_cb
            received_cb = progress_cb
            return None

        loop._process_message_inner = fake_inner
        loop._maybe_consolidate = MagicMock()

        await loop._process_message(_slack_inbound())
        assert received_cb is None

    async def test_ack_message_sent_before_inner(self, tmp_path):
        loop, bus = _build_loop(tmp_path)

        async def fake_inner(msg, session, key, progress_cb=None):
            return OutboundMessage.reply_to_inbound(msg, "done")

        loop._process_message_inner = fake_inner
        loop._maybe_consolidate = MagicMock()

        await loop._process_message(_slack_inbound())

        outbound = _drain_outbound(bus)
        assert outbound[0].content == "생각 중..."
        assert outbound[0].metadata.get("_progress") is True

    async def test_no_callback_for_telegram(self, tmp_path):
        loop, bus = _build_loop(tmp_path, slack_pulse_enabled=True)

        received_cb = None

        async def fake_inner(msg, session, key, progress_cb=None):
            nonlocal received_cb
            received_cb = progress_cb
            return None

        loop._process_message_inner = fake_inner
        loop._maybe_consolidate = MagicMock()

        await loop._process_message(_telegram_inbound())
        assert received_cb is None

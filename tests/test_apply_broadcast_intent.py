"""Unit tests for `AgentLoop._apply_broadcast_intent`.

Covers 04-plan.md §1.3 — the regex-detected broadcast intent is converted
into a new outbound carrying `reply_broadcast=True`. The 4 guards
(not_channel → no_thread → disabled → blocked) must skip cleanly, and the
new outbound MUST be built via `OutboundMessage.reply_to_inbound(...)` so
the `test_loop_py_uses_only_reply_to_inbound` regression guard stays green.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

import companio.core.loop as loop_module
from companio.bus import InboundMessage, MessageBus, OutboundMessage
from companio.config.schema import ChannelsConfig, Config, SlackConfig
from companio.core.loop import _BROADCAST_MARKER, AgentLoop


@pytest.fixture
def propagate_loguru(caplog):
    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caplog.handler.emit(record)

    handler_id = logger.add(_Handler(), level="DEBUG", format="{message}")
    yield caplog
    logger.remove(handler_id)


def _make_loop(
    tmp_path: Path,
    *,
    broadcast_enabled: bool = True,
    blocked_channels: list[str] | None = None,
) -> AgentLoop:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    bus = MessageBus()
    claude = MagicMock()
    claude.project_dir = workspace

    config = Config(
        channels=ChannelsConfig(
            slack=SlackConfig(
                enabled=True,
                bot_token="xoxb-test",
                app_token="xapp-test",
                broadcast_enabled=broadcast_enabled,
                broadcast_blocked_channels=blocked_channels or [],
            )
        )
    )
    return AgentLoop(bus=bus, claude=claude, workspace=workspace, config=config)


def _channel_inbound(
    *,
    thread_ts: str | None = "1712345678.000100",
    is_channel: bool = True,
    chat_id: str = "C1",
    content: str = "채널에도 공유해줘",
) -> InboundMessage:
    md: dict = {"is_channel": is_channel, "message_ts": "1712345678.000100"}
    if thread_ts is not None:
        md["thread_ts"] = thread_ts
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id=chat_id,
        content=content,
        metadata=md,
    )


def _response(content: str = "여기 결과입니다", media: list[str] | None = None) -> OutboundMessage:
    inbound = _channel_inbound()
    return OutboundMessage.reply_to_inbound(inbound, content, media=media)


# -----------------------------------------------------------------------------
# 1. Happy path
# -----------------------------------------------------------------------------


class TestApplyBroadcastIntentHappyPath:
    def test_all_guards_pass_injects_broadcast(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _channel_inbound()
        response = _response()

        new = loop._apply_broadcast_intent(msg, response)

        assert new is not response  # new object
        assert new.content.endswith(_BROADCAST_MARKER)
        assert new.metadata.get("reply_broadcast") is True
        assert new.metadata.get("thread_ts") == "1712345678.000100"
        assert new.metadata.get("is_channel") is True
        # The marker appends to the original content
        assert new.content == "여기 결과입니다" + _BROADCAST_MARKER

    def test_media_propagated_to_new_outbound(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _channel_inbound()
        response = _response(media=["/tmp/foo.png"])

        new = loop._apply_broadcast_intent(msg, response)

        assert new.media == ["/tmp/foo.png"]


# -----------------------------------------------------------------------------
# 2. Guards — priority order: not_channel → no_thread → disabled → blocked
# -----------------------------------------------------------------------------


class TestApplyBroadcastIntentGuards:
    def test_skip_when_not_channel(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _channel_inbound(is_channel=False)
        response = _response()

        new = loop._apply_broadcast_intent(msg, response)

        assert new is response  # unchanged
        assert "reply_broadcast" not in new.metadata

    def test_skip_when_no_thread(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _channel_inbound(thread_ts=None)
        response = _response()

        new = loop._apply_broadcast_intent(msg, response)

        assert new is response

    def test_skip_when_flag_disabled(self, tmp_path):
        loop = _make_loop(tmp_path, broadcast_enabled=False)
        msg = _channel_inbound()
        response = _response()

        new = loop._apply_broadcast_intent(msg, response)

        assert new is response

    def test_skip_when_channel_blocked(self, tmp_path):
        loop = _make_loop(tmp_path, blocked_channels=["C1"])
        msg = _channel_inbound(chat_id="C1")
        response = _response()

        new = loop._apply_broadcast_intent(msg, response)

        assert new is response

    def test_guard_priority_earliest_reason_wins(self, tmp_path, propagate_loguru):
        """not_channel + disabled + blocked → reason must be 'not_channel'."""
        propagate_loguru.set_level(logging.INFO)
        loop = _make_loop(
            tmp_path, broadcast_enabled=False, blocked_channels=["C1"]
        )
        msg = _channel_inbound(is_channel=False, chat_id="C1")
        response = _response()

        loop._apply_broadcast_intent(msg, response)

        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "reason=not_channel" in joined
        assert "reason=disabled" not in joined
        assert "reason=blocked" not in joined

    def test_skip_returns_response_untouched(self, tmp_path):
        """Skip path must not mutate the original response."""
        loop = _make_loop(tmp_path, broadcast_enabled=False)
        msg = _channel_inbound()
        original_content = "여기 결과입니다"
        original_metadata = {"thread_ts": "1712345678.000100", "is_channel": True}
        response = OutboundMessage(
            channel="slack",
            chat_id="C1",
            content=original_content,
            metadata=dict(original_metadata),
        )

        new = loop._apply_broadcast_intent(msg, response)

        assert new is response
        assert response.content == original_content
        assert response.metadata == original_metadata


# -----------------------------------------------------------------------------
# 3. Marker duplication defense
# -----------------------------------------------------------------------------


class TestApplyBroadcastIntentMarkerDup:
    def test_marker_already_present_returns_unchanged(
        self, tmp_path, propagate_loguru
    ):
        propagate_loguru.set_level(logging.WARNING)
        loop = _make_loop(tmp_path)
        msg = _channel_inbound()
        # Response already has the marker baked in
        inbound = _channel_inbound()
        response = OutboundMessage.reply_to_inbound(
            inbound, "이미 적용됨" + _BROADCAST_MARKER
        )

        new = loop._apply_broadcast_intent(msg, response)

        assert new is response
        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "marker_already_present" in joined


# -----------------------------------------------------------------------------
# 4. reply_to_inbound is the only construction path
# -----------------------------------------------------------------------------


class TestApplyBroadcastIntentConstruction:
    def test_apply_uses_reply_to_inbound_not_constructor(self, tmp_path, monkeypatch):
        """Spy on OutboundMessage.__init__ and .reply_to_inbound to prove the
        success path goes through the factory, not the bare constructor.

        This is the unit-level complement to the source-grep regression guard
        `test_loop_py_uses_only_reply_to_inbound`.
        """
        loop = _make_loop(tmp_path)
        msg = _channel_inbound()
        response = _response()

        factory_calls: list[tuple] = []
        real_factory = OutboundMessage.reply_to_inbound

        def spy_factory(inbound, content, **kwargs):
            factory_calls.append((inbound, content, kwargs))
            return real_factory(inbound, content, **kwargs)

        monkeypatch.setattr(
            loop_module.OutboundMessage, "reply_to_inbound", spy_factory
        )

        new = loop._apply_broadcast_intent(msg, response)

        assert new is not response  # actually built a new outbound
        assert len(factory_calls) == 1
        call_inbound, call_content, call_kwargs = factory_calls[0]
        assert call_inbound is msg
        assert call_content.endswith(_BROADCAST_MARKER)
        assert call_kwargs.get("extra_metadata") == {"reply_broadcast": True}


# -----------------------------------------------------------------------------
# 5. Structured logging
# -----------------------------------------------------------------------------


class TestApplyBroadcastIntentLogging:
    def test_auto_triggered_logs_with_chat_id_thread_ts_content(
        self, tmp_path, propagate_loguru
    ):
        propagate_loguru.set_level(logging.INFO)
        loop = _make_loop(tmp_path)
        msg = _channel_inbound(content="채널에도 공유해줘")
        response = _response()

        loop._apply_broadcast_intent(msg, response)

        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "slack.broadcast.auto_triggered" in joined
        assert "C1" in joined
        assert "1712345678.000100" in joined
        assert "채널에도 공유해줘" in joined

    def test_intent_skipped_logs_with_reason(self, tmp_path, propagate_loguru):
        propagate_loguru.set_level(logging.INFO)
        loop = _make_loop(tmp_path)
        msg = _channel_inbound(is_channel=False)
        response = _response()

        loop._apply_broadcast_intent(msg, response)

        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "slack.broadcast.intent_skipped" in joined
        assert "reason=not_channel" in joined

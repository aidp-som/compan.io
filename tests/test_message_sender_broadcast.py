"""Broadcast (`share_to_channel`) tests for MessageSender.

Covers the 3-layer safety contract from .feature/2026-04-11-slack-ack-reaction-channel-broadcast
04-plan §1.6, §1.8, §1.13:

- `share_to_channel=True` only injects `reply_broadcast=True` into outbound metadata
  when ALL of (is_channel, thread_ts present, broadcast_enabled, not blocked) hold.
- Any guard miss → silent skip + warning log + skip-hint suffix in the return value.
"""

from __future__ import annotations

import pytest

from companio.bus import InboundMessage, OutboundMessage
from companio.tools.message import MessageSender


@pytest.fixture
def captured() -> tuple[list[OutboundMessage], object]:
    sent: list[OutboundMessage] = []

    async def callback(msg: OutboundMessage) -> None:
        sent.append(msg)

    return sent, callback


def _channel_inbound(thread_ts: str | None = "1712345678.000100", chat_id: str = "C1") -> InboundMessage:
    metadata = {"is_channel": True}
    if thread_ts is not None:
        metadata["thread_ts"] = thread_ts
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id=chat_id,
        content="answer in channel",
        metadata=metadata,
    )


def _dm_inbound() -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="D1",
        content="hi",
        metadata={"is_channel": False, "thread_ts": "1.23"},
    )


class TestShareToChannelHappyPath:
    async def test_share_to_channel_sets_reply_broadcast_metadata(self, captured):
        sent, callback = captured
        sender = MessageSender(
            send_callback=callback,
            broadcast_enabled=True,
        )
        sender.set_context(_channel_inbound())
        sender.start_turn()

        result = await sender.send("here you go", share_to_channel=True)

        assert "Message sent" in result
        assert "skipped" not in result
        assert len(sent) == 1
        assert sent[0].metadata.get("reply_broadcast") is True
        assert sent[0].metadata.get("thread_ts") == "1712345678.000100"
        assert sent[0].metadata.get("is_channel") is True
        assert sender._broadcast_called is True

    async def test_share_to_channel_false_default_no_broadcast(self, captured):
        sent, callback = captured
        sender = MessageSender(send_callback=callback, broadcast_enabled=True)
        sender.set_context(_channel_inbound())
        sender.start_turn()

        result = await sender.send("plain reply")

        assert "skipped" not in result
        assert len(sent) == 1
        assert "reply_broadcast" not in sent[0].metadata
        assert sender._broadcast_called is False


class TestShareToChannelGuards:
    async def test_share_to_channel_ignored_in_dm(self, captured):
        sent, callback = captured
        sender = MessageSender(send_callback=callback, broadcast_enabled=True)
        sender.set_context(_dm_inbound())
        sender.start_turn()

        result = await sender.send("hi back", share_to_channel=True)

        assert "skipped" in result
        assert "not a channel" in result
        assert len(sent) == 1
        assert "reply_broadcast" not in sent[0].metadata
        # Tracker still flips True so the shadow detector knows the LLM tried.
        assert sender._broadcast_called is True

    async def test_share_to_channel_ignored_without_thread_ts(self, captured):
        sent, callback = captured
        sender = MessageSender(send_callback=callback, broadcast_enabled=True)
        sender.set_context(_channel_inbound(thread_ts=None))
        sender.start_turn()

        result = await sender.send("answer", share_to_channel=True)

        assert "skipped" in result
        assert "no thread context" in result
        assert "reply_broadcast" not in sent[0].metadata

    async def test_share_to_channel_ignored_when_flag_disabled(self, captured):
        sent, callback = captured
        sender = MessageSender(send_callback=callback, broadcast_enabled=False)
        sender.set_context(_channel_inbound())
        sender.start_turn()

        result = await sender.send("answer", share_to_channel=True)

        assert "skipped" in result
        assert "broadcast disabled" in result
        assert "reply_broadcast" not in sent[0].metadata

    async def test_share_to_channel_blocked_channel_strict_skip(self, captured):
        sent, callback = captured
        sender = MessageSender(
            send_callback=callback,
            broadcast_enabled=True,
            broadcast_blocked_channels=["C1"],
        )
        sender.set_context(_channel_inbound(chat_id="C1"))
        sender.start_turn()

        result = await sender.send("answer", share_to_channel=True)

        assert "skipped" in result
        assert "channel blocked" in result
        assert "reply_broadcast" not in sent[0].metadata


class TestSkipHintInReturnValue:
    async def test_skip_hint_in_return_value_when_ignored(self, captured):
        _, callback = captured
        sender = MessageSender(send_callback=callback, broadcast_enabled=False)
        sender.set_context(_channel_inbound())
        sender.start_turn()

        result = await sender.send("answer", share_to_channel=True)
        assert "(Note: channel broadcast skipped" in result

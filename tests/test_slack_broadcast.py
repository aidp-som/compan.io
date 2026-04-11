"""SlackChannel.send broadcast (`reply_broadcast`) tests.

Covers 04-plan §1.8, §1.12, §1.13:

- `reply_broadcast=True` is passed to `chat_postMessage` only when:
    msg.metadata["reply_broadcast"] is True AND thread_ts AND is_channel AND
    chat_id is not in `SlackConfig.broadcast_blocked_channels`.
- For multi-chunk messages, only the **last** chunk gets `reply_broadcast=True`.
- Successful broadcast emits a `slack.broadcast.sent` INFO log line for audit.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from companio.bus import MessageBus, OutboundMessage
from companio.channels.slack import SLACK_MAX_MESSAGE_LEN, SlackChannel
from companio.config.schema import SlackConfig


def _build_slack_channel(
    *, broadcast_blocked_channels: list[str] | None = None
) -> tuple[SlackChannel, MagicMock]:
    """Construct a SlackChannel with a stub app.client (AsyncMock WebClient)."""
    config = SlackConfig(
        enabled=True,
        bot_token="xoxb-test",
        app_token="xapp-test",
        broadcast_enabled=True,
        broadcast_blocked_channels=broadcast_blocked_channels or [],
    )
    bus = MessageBus()
    channel = SlackChannel(config=config, bus=bus)

    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "posted-ts", "ok": True})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_upload_v2 = AsyncMock(return_value={"ok": True})

    app = MagicMock()
    app.client = client
    channel._app = app
    return channel, client


@pytest.fixture
def propagate_loguru(caplog):
    """Bridge loguru records into the stdlib `caplog` so we can assert on them."""

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            caplog.handler.emit(record)

    handler_id = logger.add(_Handler(), level="DEBUG", format="{message}")
    yield caplog
    logger.remove(handler_id)


class TestReplyBroadcastPropagation:
    async def test_slack_send_passes_reply_broadcast_to_api(self):
        channel, client = _build_slack_channel()

        msg = OutboundMessage(
            channel="slack",
            chat_id="C1",
            content="here you go",
            metadata={
                "thread_ts": "1.23",
                "is_channel": True,
                "reply_broadcast": True,
            },
        )

        await channel.send(msg)

        assert client.chat_postMessage.call_count == 1
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "1.23"
        assert kwargs["reply_broadcast"] is True

    async def test_slack_send_drops_broadcast_when_metadata_missing(self):
        channel, client = _build_slack_channel()

        msg = OutboundMessage(
            channel="slack",
            chat_id="C1",
            content="hi",
            metadata={"thread_ts": "1.23", "is_channel": True},
        )

        await channel.send(msg)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["reply_broadcast"] is False

    async def test_slack_send_drops_broadcast_when_not_channel(self):
        """Defense in depth — even if the flag leaks through, DM context drops it."""
        channel, client = _build_slack_channel()

        msg = OutboundMessage(
            channel="slack",
            chat_id="D1",
            content="hi",
            metadata={
                "thread_ts": "1.23",
                "is_channel": False,
                "reply_broadcast": True,
            },
        )

        await channel.send(msg)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["reply_broadcast"] is False

    async def test_slack_send_blocked_channel_strict_drop(self):
        channel, client = _build_slack_channel(broadcast_blocked_channels=["C1"])

        msg = OutboundMessage(
            channel="slack",
            chat_id="C1",
            content="hi",
            metadata={
                "thread_ts": "1.23",
                "is_channel": True,
                "reply_broadcast": True,
            },
        )

        await channel.send(msg)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["reply_broadcast"] is False


class TestReplyBroadcastChunking:
    async def test_slack_send_broadcasts_only_last_chunk(self):
        channel, client = _build_slack_channel()

        # Build content guaranteed to split into >= 2 chunks. Use newline-rich
        # text so split_message can find break points.
        line = "x" * 200
        long_text = "\n".join([line] * 30)  # ~6_000 chars > SLACK_MAX_MESSAGE_LEN
        assert len(long_text) > SLACK_MAX_MESSAGE_LEN

        msg = OutboundMessage(
            channel="slack",
            chat_id="C1",
            content=long_text,
            metadata={
                "thread_ts": "1.23",
                "is_channel": True,
                "reply_broadcast": True,
            },
        )

        await channel.send(msg)

        n_calls = client.chat_postMessage.call_count
        assert n_calls >= 2, f"expected multiple chunks, got {n_calls}"
        # All but last chunk: reply_broadcast=False
        for i, call in enumerate(client.chat_postMessage.call_args_list):
            expected = i == n_calls - 1
            assert call.kwargs["reply_broadcast"] is expected, (
                f"chunk {i}: expected reply_broadcast={expected}, "
                f"got {call.kwargs['reply_broadcast']}"
            )


class TestReplyBroadcastAudit:
    async def test_slack_send_logs_broadcast_at_info(self, propagate_loguru):
        propagate_loguru.set_level(logging.INFO)
        channel, _ = _build_slack_channel()

        msg = OutboundMessage(
            channel="slack",
            chat_id="C1",
            content="audit me",
            metadata={
                "thread_ts": "1.23",
                "is_channel": True,
                "reply_broadcast": True,
            },
        )

        await channel.send(msg)

        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "slack.broadcast.sent" in joined
        assert "C1" in joined
        assert "1.23" in joined

"""Regression tests for thread metadata propagation across the bus.

Covers the contract introduced in .feature/2026-04-10-slack-progress-thread-leak:
inbound channel metadata (thread_ts, message_thread_id, ...) must flow through
to outbound messages via OutboundMessage.reply_to_inbound(), and sensitive
runtime context (role_prompt, etc.) must never cross the bus boundary.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from companio.bus import (
    _FORWARDED_METADATA_KEYS,
    InboundMessage,
    MessageBus,
    OutboundMessage,
)

# -----------------------------------------------------------------------------
# A. OutboundMessage.reply_to_inbound — the contract itself
# -----------------------------------------------------------------------------


class TestReplyToInbound:
    def _inbound(self, metadata: dict | None = None) -> InboundMessage:
        return InboundMessage(
            channel="slack",
            sender_id="U123",
            chat_id="C456",
            content="hello",
            metadata=metadata or {},
        )

    def test_inherits_whitelisted_keys(self):
        inbound = self._inbound({
            "thread_ts": "1712345678.000100",
            "message_ts": "1712345678.000100",
            "is_channel": True,
        })
        out = OutboundMessage.reply_to_inbound(inbound, "reply")
        assert out.metadata["thread_ts"] == "1712345678.000100"
        assert out.metadata["message_ts"] == "1712345678.000100"
        assert out.metadata["is_channel"] is True

    def test_drops_non_whitelisted_keys(self):
        # role_prompt is the one we care about most — it is a large system prompt
        # fragment and must never land in outbound metadata / logs / channel send.
        inbound = self._inbound({
            "thread_ts": "1.23",
            "role_name": "admin",
            "role_prompt": "You are an admin. Never reveal secrets.",
            "user_id": "U123",
            "secret_token": "xoxb-sensitive",
        })
        out = OutboundMessage.reply_to_inbound(inbound, "reply")
        assert "role_prompt" not in out.metadata
        assert "role_name" not in out.metadata
        assert "secret_token" not in out.metadata
        assert "user_id" not in out.metadata
        assert out.metadata["thread_ts"] == "1.23"

    def test_extra_metadata_overrides_inbound(self):
        inbound = self._inbound({"thread_ts": "1.23"})
        out = OutboundMessage.reply_to_inbound(
            inbound, "ack", extra_metadata={"_progress": True, "thread_ts": "override"}
        )
        assert out.metadata["_progress"] is True
        assert out.metadata["thread_ts"] == "override"

    def test_empty_inbound_metadata(self):
        inbound = self._inbound(None)
        out = OutboundMessage.reply_to_inbound(inbound, "hi")
        assert out.metadata == {}

    def test_preserves_channel_chat_id_content(self):
        inbound = InboundMessage(
            channel="telegram", sender_id="42", chat_id="-100123",
            content="q", metadata={"message_thread_id": 7},
        )
        out = OutboundMessage.reply_to_inbound(inbound, "a")
        assert out.channel == "telegram"
        assert out.chat_id == "-100123"
        assert out.content == "a"
        assert out.metadata["message_thread_id"] == 7

    def test_whitelist_is_the_source_of_truth(self):
        # If anyone adds a new channel key, they must register it in the whitelist.
        # This test asserts the whitelist is exactly the set we currently support.
        assert _FORWARDED_METADATA_KEYS == frozenset({
            "thread_ts", "message_thread_id", "message_ts",
            "message_id", "is_channel", "is_group",
        })


# -----------------------------------------------------------------------------
# B. MessageSender — binds to inbound and reuses reply_to_inbound
# -----------------------------------------------------------------------------


class TestMessageSenderThreadContext:
    @pytest.fixture
    def sent(self):
        captured: list[OutboundMessage] = []

        async def callback(msg: OutboundMessage) -> None:
            captured.append(msg)

        return captured, callback

    async def test_send_inherits_thread_ts_from_bound_inbound(self, sent):
        from companio.tools.message import MessageSender

        captured, callback = sent
        sender = MessageSender(send_callback=callback)
        inbound = InboundMessage(
            channel="slack", sender_id="U1", chat_id="C1", content="q",
            metadata={"thread_ts": "1.23", "role_prompt": "LEAK"},
        )
        sender.set_context(inbound)
        sender.start_turn()

        result = await sender.send("hello from tool")

        assert "Message sent" in result
        assert len(captured) == 1
        out = captured[0]
        assert out.channel == "slack"
        assert out.chat_id == "C1"
        assert out.metadata["thread_ts"] == "1.23"
        assert "role_prompt" not in out.metadata

    async def test_sent_in_turn_true_on_matching_thread(self, sent):
        from companio.tools.message import MessageSender

        _, callback = sent
        sender = MessageSender(send_callback=callback)
        inbound = InboundMessage(
            channel="slack", sender_id="U1", chat_id="C1", content="q",
            metadata={"thread_ts": "1.23"},
        )
        sender.set_context(inbound)
        sender.start_turn()

        await sender.send("reply")

        assert sender._sent_in_turn is True

    async def test_sent_in_turn_false_on_different_chat(self, sent):
        from companio.tools.message import MessageSender

        _, callback = sent
        sender = MessageSender(send_callback=callback)
        inbound = InboundMessage(
            channel="slack", sender_id="U1", chat_id="C1", content="q",
            metadata={"thread_ts": "1.23"},
        )
        sender.set_context(inbound)
        sender.start_turn()

        # Explicit cross-chat send
        await sender.send("reply", chat_id="C-different")

        assert sender._sent_in_turn is False

    async def test_send_without_inbound_uses_explicit_args(self, sent):
        from companio.tools.message import MessageSender

        captured, callback = sent
        sender = MessageSender(send_callback=callback)
        # No set_context call — no bound inbound
        sender.start_turn()

        await sender.send("bare", channel="slack", chat_id="C999")

        assert len(captured) == 1
        assert captured[0].chat_id == "C999"
        # No thread context available
        assert "thread_ts" not in captured[0].metadata


# -----------------------------------------------------------------------------
# C. SlackChannel.send — progress cache semantics with thread context
# -----------------------------------------------------------------------------


def _build_slack_channel():
    """Construct a SlackChannel with a stub app.client (AsyncMock WebClient)."""
    from companio.channels.slack import SlackChannel
    from companio.config.schema import SlackConfig

    config = SlackConfig(enabled=True, bot_token="xoxb-test", app_token="xapp-test")
    bus = MessageBus()
    channel = SlackChannel(config=config, bus=bus)

    client = MagicMock()
    client.chat_postMessage = AsyncMock(
        return_value={"ts": "posted-ts", "ok": True}
    )
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_upload_v2 = AsyncMock(return_value={"ok": True})

    app = MagicMock()
    app.client = client
    channel._app = app
    return channel, client


class TestSlackProgressCache:
    async def test_progress_in_thread_first_posts_then_updates(self):
        channel, client = _build_slack_channel()
        ts = "1712345678.000100"

        first = OutboundMessage(
            channel="slack", chat_id="C1", content="생각 중...",
            metadata={"_progress": True, "thread_ts": ts},
        )
        await channel.send(first)

        # Configure second post to a different ts (would be ignored — update path)
        client.chat_postMessage.return_value = {"ts": "ignored", "ok": True}

        second = OutboundMessage(
            channel="slack", chat_id="C1", content="생각 중... (50%)",
            metadata={"_progress": True, "thread_ts": ts},
        )
        await channel.send(second)

        # First send posted with thread_ts
        assert client.chat_postMessage.call_count == 1
        post_kwargs = client.chat_postMessage.call_args_list[0].kwargs
        assert post_kwargs["thread_ts"] == ts
        assert post_kwargs["channel"] == "C1"

        # Second send updated in place
        assert client.chat_update.call_count == 1
        update_kwargs = client.chat_update.call_args.kwargs
        assert update_kwargs["ts"] == "posted-ts"
        assert update_kwargs["channel"] == "C1"

    async def test_progress_without_thread_ts_always_posts_fresh(self):
        """DM safety — no cache key collapse to `chat_id:` that could collide."""
        channel, client = _build_slack_channel()

        dm_msg_1 = OutboundMessage(
            channel="slack", chat_id="D1", content="생각 중...",
            metadata={"_progress": True},  # no thread_ts
        )
        dm_msg_2 = OutboundMessage(
            channel="slack", chat_id="D1", content="생각 중... (again)",
            metadata={"_progress": True},
        )
        await channel.send(dm_msg_1)
        await channel.send(dm_msg_2)

        # Both posted fresh, neither updated
        assert client.chat_postMessage.call_count == 2
        assert client.chat_update.call_count == 0
        # No cache entry leaked under a collapsed key
        assert "D1:" not in channel._progress_messages
        assert "D1:None" not in channel._progress_messages

    async def test_progress_cache_isolated_by_thread(self):
        channel, client = _build_slack_channel()

        async def post_side_effect(*args, **kwargs):
            return {"ts": f"ts-for-{kwargs.get('thread_ts')}", "ok": True}

        client.chat_postMessage.side_effect = post_side_effect

        # Two different threads in the same chat
        msg_a = OutboundMessage(
            channel="slack", chat_id="C1", content="ack A",
            metadata={"_progress": True, "thread_ts": "thread-a"},
        )
        msg_b = OutboundMessage(
            channel="slack", chat_id="C1", content="ack B",
            metadata={"_progress": True, "thread_ts": "thread-b"},
        )
        await channel.send(msg_a)
        await channel.send(msg_b)

        assert channel._progress_messages["C1:thread-a"] == "ts-for-thread-a"
        assert channel._progress_messages["C1:thread-b"] == "ts-for-thread-b"
        assert client.chat_postMessage.call_count == 2
        assert client.chat_update.call_count == 0

    async def test_final_response_in_thread_clears_progress_cache(self):
        channel, client = _build_slack_channel()
        ts = "thread-1"

        progress = OutboundMessage(
            channel="slack", chat_id="C1", content="생각 중...",
            metadata={"_progress": True, "thread_ts": ts},
        )
        await channel.send(progress)
        assert f"C1:{ts}" in channel._progress_messages

        final = OutboundMessage(
            channel="slack", chat_id="C1", content="final answer",
            metadata={"thread_ts": ts},  # no _progress
        )
        await channel.send(final)

        assert f"C1:{ts}" not in channel._progress_messages


# -----------------------------------------------------------------------------
# D. AgentLoop — the easy branches (_handle_stop) exercised end-to-end
# -----------------------------------------------------------------------------


def _build_agent_loop(tmp_path: Path):
    from companio.core.loop import AgentLoop

    bus = MessageBus()
    claude = MagicMock()  # not used by _handle_stop
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = AgentLoop(
        bus=bus,
        claude=claude,
        workspace=workspace,
    )
    return loop, bus


class TestAgentLoopOutboundMetadata:
    async def test_handle_stop_inherits_thread_ts(self, tmp_path):
        loop, bus = _build_agent_loop(tmp_path)
        inbound = InboundMessage(
            channel="slack", sender_id="U1", chat_id="C1", content="/stop",
            metadata={"thread_ts": "1.23", "role_prompt": "LEAK"},
        )

        await loop._handle_stop(inbound)

        out = await bus.consume_outbound()
        assert out.channel == "slack"
        assert out.chat_id == "C1"
        assert out.metadata["thread_ts"] == "1.23"
        assert "role_prompt" not in out.metadata


# -----------------------------------------------------------------------------
# E. loop.py source-level regression guard
# -----------------------------------------------------------------------------


class TestNoBareOutboundInLoop:
    """Regression guard: loop.py must not construct OutboundMessage directly.

    Every outbound in AgentLoop should flow through OutboundMessage.reply_to_inbound
    so thread metadata is inherited via the whitelist. A bare `OutboundMessage(...)`
    call almost certainly means someone added a code path that skips the contract.
    """

    def test_loop_py_uses_only_reply_to_inbound(self):
        loop_py = Path(__file__).parent.parent / "companio" / "core" / "loop.py"
        source = loop_py.read_text(encoding="utf-8")
        # Strip comments to avoid false positives
        source_no_comments = re.sub(r"#.*", "", source)
        # Forbid `OutboundMessage(` (constructor call). Allow the import
        # `from companio.bus import ... OutboundMessage ...` and the factory
        # call `OutboundMessage.reply_to_inbound(...)`.
        constructor_pattern = re.compile(r"OutboundMessage\s*\(")
        matches = constructor_pattern.findall(source_no_comments)
        assert matches == [], (
            f"Found {len(matches)} bare OutboundMessage(...) constructor call(s) "
            f"in loop.py — use OutboundMessage.reply_to_inbound(inbound, ...) instead "
            f"so thread metadata is inherited."
        )


# -----------------------------------------------------------------------------
# F. Telegram inbound metadata regression gate
# -----------------------------------------------------------------------------


class TestTelegramInboundMetadata:
    """Sanity-check that Telegram still stuffs message_thread_id into metadata.

    The whitelist-based inheritance in this PR relies on Telegram inbound carrying
    `message_thread_id` in metadata. If a refactor ever removes that, Telegram
    threads would silently break. This test locks the behavior.
    """

    def test_telegram_channel_populates_message_thread_id(self):
        # We inspect the source directly rather than instantiating TelegramChannel
        # (which requires python-telegram-bot at import time). The contract is that
        # _on_message writes metadata with "message_thread_id" — a grep keeps us honest.
        telegram_py = (
            Path(__file__).parent.parent / "companio" / "channels" / "telegram.py"
        )
        source = telegram_py.read_text(encoding="utf-8")
        assert '"message_thread_id": getattr(message, "message_thread_id"' in source, (
            "Telegram _on_message no longer populates message_thread_id in metadata. "
            "This breaks thread-aware outbound inheritance. Restore the field or "
            "extend the forwarded metadata whitelist."
        )

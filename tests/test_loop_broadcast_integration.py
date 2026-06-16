"""Integration tests for `AgentLoop._process_message` broadcast intent flow.

Covers 04-plan.md §1.6 — verifies end-to-end:

- Regex-matched inbound → final outbound carries marker + reply_broadcast=True.
- DM (not_channel) → no broadcast even with matching intent.
- Slash commands (/help, /new) → early return, never apply intent.
- None response (e.g. sent via tool) → skip intent application silently.
- Progress pulse messages are NOT broadcast (they are published directly to
  the bus; `_process_message` never routes them through the intent helper).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from companio.bus import InboundMessage, MessageBus
from companio.config.schema import ChannelsConfig, Config, SlackConfig
from companio.core.loop import _BROADCAST_MARKER, AgentLoop


def _build_loop(
    tmp_path: Path, *, broadcast_enabled: bool = True
) -> tuple[AgentLoop, MessageBus, MagicMock]:
    bus = MessageBus()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    claude = MagicMock()
    fake_response = MagicMock()
    fake_response.is_error = False
    fake_response.result = "여기 결과입니다"
    fake_response.session_id = "sid-1"
    fake_response.total_cost_usd = 0.0
    fake_response.duration_ms = 1
    fake_response.num_turns = 1
    fake_response.input_tokens = 1
    fake_response.output_tokens = 1
    fake_response.cache_read_input_tokens = 0
    fake_response.cache_creation_input_tokens = 0
    claude.run = AsyncMock(return_value=fake_response)
    claude.project_dir = workspace

    config = Config(
        channels=ChannelsConfig(
            slack=SlackConfig(
                enabled=True,
                bot_token="xoxb-test",
                app_token="xapp-test",
                broadcast_enabled=broadcast_enabled,
            )
        )
    )

    loop = AgentLoop(bus=bus, claude=claude, workspace=workspace, config=config)
    loop.context.write_claude_md = MagicMock()  # type: ignore[method-assign]
    return loop, bus, claude


def _channel_inbound(content: str) -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="C1",
        content=content,
        metadata={
            "is_channel": True,
            "thread_ts": "1712345678.000100",
            "message_ts": "1712345678.000100",
        },
    )


def _dm_inbound(content: str) -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="D1",
        content=content,
        metadata={"is_channel": False, "thread_ts": "1.23"},
    )


class TestLoopBroadcastIntegration:
    async def test_intent_matched_happy_path_includes_marker_and_metadata(
        self, tmp_path
    ):
        loop, _, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _channel_inbound("채널에도 공유해줘")
            response = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        assert response is not None
        assert response.content.endswith(_BROADCAST_MARKER)
        assert response.metadata.get("reply_broadcast") is True
        assert response.metadata.get("thread_ts") == "1712345678.000100"
        assert response.metadata.get("is_channel") is True

    async def test_intent_matched_dm_skips_silently(self, tmp_path):
        loop, _, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _dm_inbound("채널에도 공유해줘")
            response = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        assert response is not None
        assert _BROADCAST_MARKER not in response.content
        assert "reply_broadcast" not in response.metadata

    async def test_slash_help_with_intent_does_not_apply_broadcast(self, tmp_path):
        loop, _, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            # /help is an early-return path — even if content would match
            # regex, we never apply broadcast intent.
            msg = _channel_inbound("/help")
            response = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        assert response is not None
        assert "companio commands" in response.content
        assert _BROADCAST_MARKER not in response.content
        assert "reply_broadcast" not in response.metadata

    async def test_slash_new_with_intent_does_not_apply_broadcast(self, tmp_path):
        loop, _, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _channel_inbound("/new")
            response = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        assert response is not None
        assert _BROADCAST_MARKER not in response.content
        assert "reply_broadcast" not in response.metadata

    async def test_response_none_skips_intent_application(self, tmp_path):
        """If the tool path already sent the final response, `_process_message`
        returns None and the intent helper is never called."""
        loop, _, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _channel_inbound("채널에도 공유해줘")
            # Simulate the LLM having invoked message_sender.send() already.
            # _process_message_inner returns None when _sent_in_turn is True.
            original_inner = loop._process_message_inner

            async def fake_inner(inbound, session, key, progress_cb=None):
                await original_inner(inbound, session, key)
                loop.message_sender._sent_in_turn = True
                return None

            loop._process_message_inner = fake_inner  # type: ignore[method-assign]
            response = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        # No final outbound means no marker, no broadcast — silent skip.
        assert response is None

    async def test_non_matching_content_does_not_apply_broadcast(self, tmp_path):
        """Sanity: a non-broadcast request must not get the marker."""
        loop, _, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _channel_inbound("hello how are you")
            response = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        assert response is not None
        assert _BROADCAST_MARKER not in response.content
        assert "reply_broadcast" not in response.metadata

    async def test_progress_pulse_messages_not_broadcast(self, tmp_path):
        """Regression guard: progress/ACK outbound messages are published
        directly to the bus from `_invoke_claude_turn`, not routed through
        `_apply_broadcast_intent`. So even when the inbound text matches the
        broadcast intent, the ACK on the bus must NOT carry reply_broadcast.
        """
        loop, bus, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _channel_inbound("채널에도 공유해줘")
            final = await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        # Drain whatever was published during the turn (ACK/pulse).
        ack_msgs = []
        while bus.outbound_size > 0:
            ack_msgs.append(await bus.consume_outbound())

        # At least one ACK/progress outbound was published.
        progress_msgs = [m for m in ack_msgs if m.metadata.get("_progress")]
        assert progress_msgs, "expected at least one progress/ACK outbound"
        for pm in progress_msgs:
            assert "reply_broadcast" not in pm.metadata
            assert _BROADCAST_MARKER not in pm.content

        # The FINAL response carries the marker + broadcast flag.
        assert final is not None
        assert final.content.endswith(_BROADCAST_MARKER)
        assert final.metadata.get("reply_broadcast") is True

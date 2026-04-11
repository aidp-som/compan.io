"""Shadow detection tests for `share_to_channel` LLM trigger.

Covers 04-plan §1.6: when an inbound message contains a broadcast trigger
phrase but the LLM never invokes `share_to_channel=True`, AgentLoop must log
`slack.broadcast.shadow_miss` at WARN. Conversely, no warning fires if the
LLM honored the intent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from companio.bus import InboundMessage, MessageBus
from companio.core.loop import (
    _BROADCAST_TRIGGER_PATTERN,
    AgentLoop,
    _detect_broadcast_intent,
)


@pytest.fixture
def propagate_loguru(caplog):
    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            caplog.handler.emit(record)

    handler_id = logger.add(_Handler(), level="DEBUG", format="{message}")
    yield caplog
    logger.remove(handler_id)


def _build_loop(tmp_path: Path) -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    claude = MagicMock()
    # claude.run -> ClaudeResponse-like object with the fields _process_message reads.
    fake_response = MagicMock()
    fake_response.is_error = False
    fake_response.result = "ok"
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

    loop = AgentLoop(bus=bus, claude=claude, workspace=workspace)
    # Stub context.write_claude_md to avoid filesystem template ops.
    loop.context.write_claude_md = MagicMock()  # type: ignore[method-assign]
    return loop, bus


def _channel_inbound(content: str) -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="C1",
        content=content,
        metadata={"is_channel": True, "thread_ts": "1.23", "message_ts": "1.23"},
    )


class TestShadowDetectorLogger:
    async def test_shadow_logger_warns_on_unmatched_trigger(
        self, tmp_path, propagate_loguru
    ):
        propagate_loguru.set_level(logging.WARNING)
        loop, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()
        try:
            msg = _channel_inbound("이 결과 채널에도 공유해줘")
            await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "slack.broadcast.shadow_miss" in joined
        assert "C1" in joined

    async def test_shadow_logger_silent_when_tool_used(
        self, tmp_path, propagate_loguru
    ):
        propagate_loguru.set_level(logging.WARNING)
        loop, _ = _build_loop(tmp_path)
        await loop._session_manager.initialize()

        msg = _channel_inbound("이 결과 채널에도 공유해줘")

        # Patch claude.run to simulate the LLM honoring the intent: it would
        # call message_sender.send(share_to_channel=True). We emulate that by
        # flipping the tracker on the bound MessageSender.
        original_run = loop.claude.run

        async def fake_run(*args, **kwargs):
            loop.message_sender._broadcast_called = True
            return await original_run(*args, **kwargs)

        loop.claude.run = fake_run  # type: ignore[method-assign]

        try:
            await loop._process_message(msg)
        finally:
            await loop._session_manager.close()

        joined = "\n".join(r.getMessage() for r in propagate_loguru.records)
        assert "slack.broadcast.shadow_miss" not in joined


class TestShadowPatternMatching:
    @pytest.mark.parametrize(
        "phrase",
        [
            "채널에도 공유해줘",
            "이거 채널에 공유해주세요",
            "팀에 공지해주세요",
            "다른 사람도 알 수 있게 해줘",
            "방송해주세요",
            "모두에게 알려주세요",
            "팀에 알려",
        ],
    )
    def test_shadow_pattern_matches_korean_phrases(self, phrase):
        assert _detect_broadcast_intent(phrase), (
            f"phrase {phrase!r} should match _BROADCAST_TRIGGER_PATTERN"
        )

    @pytest.mark.parametrize(
        "phrase",
        [
            "please broadcast this",
            "Broadcast to the team",
            "let everyone know",
            "EVERYONE should see this",
        ],
    )
    def test_shadow_pattern_matches_english_phrases(self, phrase):
        assert _detect_broadcast_intent(phrase)

    def test_pattern_does_not_match_irrelevant_text(self):
        assert not _detect_broadcast_intent("hello world")
        assert not _detect_broadcast_intent("can you fix the bug?")
        # Sanity: pattern object is the same one wired into AgentLoop.
        assert _BROADCAST_TRIGGER_PATTERN.search("broadcast") is not None

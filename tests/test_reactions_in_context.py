"""Tests for reactions rendering in _format_thread_messages."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from companio.bus import MessageBus
from companio.channels.slack import SlackChannel
from companio.config.schema import SlackConfig


def _build_channel() -> SlackChannel:
    config = SlackConfig(
        enabled=True, bot_token="xoxb-test", app_token="xapp-test",
    )
    bus = MessageBus()
    ch = SlackChannel(config=config, bus=bus)
    app_mock = MagicMock()
    app_mock.client = AsyncMock()
    app_mock.client.users_info = AsyncMock(return_value={
        "user": {"profile": {"display_name": "홍주"}, "real_name": "이홍주", "id": "U001"},
    })
    ch._app = app_mock
    return ch


@pytest.mark.asyncio
async def test_message_with_reactions():
    ch = _build_channel()
    messages = [
        {
            "text": "발주 올려드립니다",
            "user": "U001",
            "ts": "1715400000.000100",
            "reactions": [
                {"name": "white_check_mark", "users": ["U002"], "count": 1},
                {"name": "+1", "users": ["U003"], "count": 1},
            ],
        },
    ]
    result = await ch._format_thread_messages(messages, current_message_ts=None)
    assert "reactions:" in result
    assert "white_check_mark" in result


@pytest.mark.asyncio
async def test_message_without_reactions():
    ch = _build_channel()
    messages = [
        {"text": "주문 접수합니다", "user": "U001", "ts": "1715400000.000200"},
    ]
    result = await ch._format_thread_messages(messages, current_message_ts=None)
    assert "[reactions:" not in result
    assert "주문 접수합니다" in result


@pytest.mark.asyncio
async def test_message_with_empty_reactions_list():
    ch = _build_channel()
    messages = [
        {"text": "배송 건", "user": "U001", "ts": "1715400000.000300", "reactions": []},
    ]
    result = await ch._format_thread_messages(messages, current_message_ts=None)
    assert "[reactions:" not in result
    assert "배송 건" in result


@pytest.mark.asyncio
async def test_bot_message_included_other_subtypes_skipped():
    ch = _build_channel()
    messages = [
        {"text": "봇 알림", "user": "U001", "ts": "1715400000.000400", "subtype": "bot_message"},
        {"text": "시스템", "user": "U001", "ts": "1715400000.000450", "subtype": "channel_join"},
        {"text": "사람 메시지", "user": "U001", "ts": "1715400000.000500"},
    ]
    result = await ch._format_thread_messages(messages, current_message_ts=None)
    assert "봇 알림" in result
    assert "시스템" not in result
    assert "사람 메시지" in result

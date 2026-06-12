import asyncio
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from companio.bus import InboundMessage, MessageBus, OutboundMessage


class TestMessageBus:
    @pytest.mark.asyncio
    async def test_publish_and_consume_inbound(self):
        bus = MessageBus()
        msg = InboundMessage(channel="telegram", sender_id="user1", chat_id="chat1", content="hello")
        await bus.publish_inbound(msg)
        received = await bus.consume_inbound()
        assert received.content == "hello"
        assert received.channel == "telegram"

    @pytest.mark.asyncio
    async def test_publish_and_consume_outbound(self):
        bus = MessageBus()
        msg = OutboundMessage(channel="telegram", chat_id="chat1", content="response")
        await bus.publish_outbound(msg)
        received = await bus.consume_outbound()
        assert received.content == "response"

    def test_inbound_message_session_key(self):
        msg = InboundMessage(channel="telegram", sender_id="user1", chat_id="chat123", content="hi")
        assert msg.session_key == "telegram:chat123"

    def test_inbound_message_session_key_override(self):
        msg = InboundMessage(
            channel="telegram", sender_id="user1", chat_id="chat123",
            content="hi", session_key_override="custom:key"
        )
        assert msg.session_key == "custom:key"

    @pytest.mark.asyncio
    async def test_queue_sizes(self):
        bus = MessageBus()
        assert bus.inbound_size == 0
        assert bus.outbound_size == 0
        await bus.publish_inbound(
            InboundMessage(channel="t", sender_id="u", chat_id="c", content="x")
        )
        assert bus.inbound_size == 1


class TestSessionLocking:
    def test_different_sessions_different_locks(self):
        locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        lock_a = locks["telegram:user_a"]
        lock_b = locks["telegram:user_b"]
        assert lock_a is not lock_b

    def test_same_session_same_lock(self):
        locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        lock1 = locks["telegram:user_a"]
        lock2 = locks["telegram:user_a"]
        assert lock1 is lock2

    @pytest.mark.asyncio
    async def test_concurrent_different_sessions(self):
        """Different sessions should not block each other."""
        locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        results = []

        async def process(session_key, delay, value):
            async with locks[session_key]:
                await asyncio.sleep(delay)
                results.append(value)

        # Start two tasks for different sessions concurrently
        await asyncio.gather(
            process("session_a", 0.1, "a"),
            process("session_b", 0.05, "b"),
        )
        # b should finish first since it has shorter delay
        assert results[0] == "b"
        assert results[1] == "a"


@pytest.fixture
def agent(tmp_path):
    from companio.bus import MessageBus
    from companio.core.claude_cli import ClaudeCLI
    from companio.core.loop import AgentLoop

    bus = MessageBus()
    claude = MagicMock(spec=ClaudeCLI)
    return AgentLoop(bus=bus, claude=claude, workspace=tmp_path)


class TestActiveTasksCount:
    def test_active_tasks_count_empty(self, agent):
        assert agent.active_tasks_count == 0

    def test_active_tasks_count_with_tasks(self, agent):
        import asyncio
        agent._active_tasks["session1"] = [asyncio.Future(), asyncio.Future()]
        agent._active_tasks["session2"] = [asyncio.Future()]
        assert agent.active_tasks_count == 3

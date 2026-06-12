"""Regression guards for AgentLoop._dispatch try/finally guarantees.

WO-P1-01 §3-6: every terminal state of `_dispatch` must publish a
`_reaction_lifecycle` outbound (done_success or done_error) so SlackChannel can
finalize the 👀 ack. Skip the publish only when no `message_ts` is present.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from companio.bus import InboundMessage, MessageBus
from companio.core.loop import AgentLoop


def _build_loop(tmp_path: Path) -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    claude = MagicMock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = AgentLoop(bus=bus, claude=claude, workspace=workspace)
    return loop, bus


def _inbound_with_ts(content: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="C1",
        content=content,
        metadata={"thread_ts": "1.23", "message_ts": "1.23", "is_channel": True},
    )


def _drain_outbound(bus: MessageBus) -> list:
    out = []
    while not bus.outbound.empty():
        out.append(bus.outbound.get_nowait())
    return out


class TestDispatchFinally:
    async def test_dispatch_normal_path_publishes_done_success(self, tmp_path):
        loop, bus = _build_loop(tmp_path)
        loop._process_message = AsyncMock(return_value=None)  # nothing to publish
        msg = _inbound_with_ts()

        await loop._dispatch(msg)

        outs = _drain_outbound(bus)
        # Should be exactly one outbound — the done_success lifecycle marker
        assert len(outs) == 1
        out = outs[0]
        assert out.metadata.get("_reaction_lifecycle") == "done_success"
        assert out.content == ""
        assert out.metadata.get("message_ts") == "1.23"

    async def test_dispatch_exception_path_publishes_done_error(self, tmp_path):
        loop, bus = _build_loop(tmp_path)
        loop._process_message = AsyncMock(side_effect=RuntimeError("boom"))
        msg = _inbound_with_ts()

        await loop._dispatch(msg)

        outs = _drain_outbound(bus)
        # First outbound is the user-facing error reply, last is the done_error
        # marker — we don't care about strict order beyond "lifecycle present".
        lifecycles = [o.metadata.get("_reaction_lifecycle") for o in outs]
        assert "done_error" in lifecycles
        # Verify the lifecycle marker has empty content (reaction-only).
        marker = next(o for o in outs if o.metadata.get("_reaction_lifecycle"))
        assert marker.content == ""
        assert marker.metadata.get("message_ts") == "1.23"

    async def test_dispatch_cancellation_path_publishes_done_error_before_reraise(
        self, tmp_path,
    ):
        loop, bus = _build_loop(tmp_path)
        loop._process_message = AsyncMock(side_effect=asyncio.CancelledError())
        msg = _inbound_with_ts()

        with pytest.raises(asyncio.CancelledError):
            await loop._dispatch(msg)

        outs = _drain_outbound(bus)
        lifecycles = [o.metadata.get("_reaction_lifecycle") for o in outs]
        assert "done_error" in lifecycles

    async def test_dispatch_skips_done_when_no_message_ts(self, tmp_path):
        loop, bus = _build_loop(tmp_path)
        loop._process_message = AsyncMock(return_value=None)
        # Inbound without message_ts (e.g. CLI/cron path)
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="c",
            content="hi",
            metadata={},
        )

        await loop._dispatch(msg)

        outs = _drain_outbound(bus)
        # No message_ts → no lifecycle marker should be published
        assert all(
            o.metadata.get("_reaction_lifecycle") is None for o in outs
        )

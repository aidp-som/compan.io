"""Unit tests for the Slack progress pulse loop (WO-P2-01).

Covers 04-plan.md §1.14a / PR 2 — Feature 3:
- 5-second cadence, max 4 pulses, "생각 중... (약 N초)" text format
- Slack-only, opt-in via SlackConfig.progress_pulse_enabled
- Pulse task lifecycle inside _process_message: cancelled on success,
  exception, or task cancellation
- Each pulse publishes an OutboundMessage with `_progress=True` so the
  existing SlackChannel `_progress_messages` cache routes it to chat.update
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
    PULSE_INTERVAL_SECONDS,
    PULSE_MAX_COUNT,
    PULSE_TEXT_FORMAT,
    AgentLoop,
)
from companio.session import Session

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _build_loop(tmp_path: Path, *, slack_pulse_enabled: bool = True) -> tuple[AgentLoop, MessageBus]:
    """Build an AgentLoop with a real bus and a stubbed claude/session manager."""
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

    # Replace SQLite-backed session manager with an async stub so _process_message
    # can run without touching disk. Tests that exercise _process_message patch
    # _process_message_inner directly so the rest of the body is irrelevant.
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


# Save the real asyncio.sleep before any monkeypatching so fast_sleep can still
# yield to the event loop without hitting the (patched) symbol.
_REAL_SLEEP = asyncio.sleep


def _make_fast_sleep(record: list[float]):
    """Build a fake sleep that records its arg and yields immediately."""
    async def fast_sleep(seconds: float) -> None:
        record.append(seconds)
        await _REAL_SLEEP(0)

    return fast_sleep


# -----------------------------------------------------------------------------
# A. _should_start_pulse — gating logic
# -----------------------------------------------------------------------------


class TestShouldStartPulse:
    def test_pulse_enabled_for_slack_with_flag(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=True)
        assert loop._should_start_pulse(_slack_inbound()) is True

    def test_pulse_disabled_flag_skips_loop(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=False)
        assert loop._should_start_pulse(_slack_inbound()) is False

    def test_pulse_skipped_for_non_slack_channels(self, tmp_path):
        loop, _ = _build_loop(tmp_path, slack_pulse_enabled=True)
        assert loop._should_start_pulse(_telegram_inbound()) is False

    def test_pulse_skipped_when_no_config(self, tmp_path):
        # Defensive default: AgentLoop built without config never pulses.
        bus = MessageBus()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        loop = AgentLoop(bus=bus, claude=MagicMock(), workspace=workspace)
        assert loop._should_start_pulse(_slack_inbound()) is False


# -----------------------------------------------------------------------------
# B. _progress_pulse_loop — direct unit tests
# -----------------------------------------------------------------------------


class TestProgressPulseLoop:
    async def test_pulse_sends_4_messages_at_5s_intervals(self, tmp_path, monkeypatch):
        loop, bus = _build_loop(tmp_path)
        sleeps: list[float] = []
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep(sleeps))

        await loop._progress_pulse_loop(_slack_inbound())

        # Exactly 4 publishes
        outbound = _drain_outbound(bus)
        assert len(outbound) == PULSE_MAX_COUNT == 4

        # Each iteration slept exactly PULSE_INTERVAL_SECONDS (5)
        assert sleeps == [PULSE_INTERVAL_SECONDS] * PULSE_MAX_COUNT

    async def test_pulse_text_format_includes_elapsed_seconds(self, tmp_path, monkeypatch):
        loop, bus = _build_loop(tmp_path)
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep([]))

        await loop._progress_pulse_loop(_slack_inbound())

        outbound = _drain_outbound(bus)
        assert [m.content for m in outbound] == [
            PULSE_TEXT_FORMAT.format(n=5),
            PULSE_TEXT_FORMAT.format(n=10),
            PULSE_TEXT_FORMAT.format(n=15),
            PULSE_TEXT_FORMAT.format(n=20),
        ]
        # Sanity: format produces the Korean string spec'd in the WO
        assert outbound[0].content == "\uc0dd\uac01 \uc911... (\uc57d 5\ucd08)"
        assert outbound[-1].content == "\uc0dd\uac01 \uc911... (\uc57d 20\ucd08)"

    async def test_pulse_max_count_4(self, tmp_path, monkeypatch):
        """5th sleep must never be invoked even if the loop is exhausted."""
        loop, bus = _build_loop(tmp_path)
        sleeps: list[float] = []
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep(sleeps))

        await loop._progress_pulse_loop(_slack_inbound())

        assert len(sleeps) == 4
        assert len(_drain_outbound(bus)) == 4

    async def test_pulse_publishes_with_progress_metadata(self, tmp_path, monkeypatch):
        loop, bus = _build_loop(tmp_path)
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep([]))

        inbound = _slack_inbound()
        await loop._progress_pulse_loop(inbound)

        outbound = _drain_outbound(bus)
        for m in outbound:
            assert m.metadata.get("_progress") is True
            # thread_ts must be inherited from inbound so SlackChannel routes
            # the pulse to the existing chat.update path (not a fresh post).
            assert m.metadata.get("thread_ts") == inbound.metadata["thread_ts"]
            assert m.channel == "slack"
            assert m.chat_id == "C1"

    async def test_pulse_cancelled_on_fast_response(self, tmp_path):
        """If pulse is cancelled before the first sleep returns, 0 publishes."""
        loop, bus = _build_loop(tmp_path)

        task = asyncio.create_task(loop._progress_pulse_loop(_slack_inbound()))
        # Yield once so the task can enter `asyncio.sleep(5)` and park.
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert _drain_outbound(bus) == []

    async def test_pulse_swallows_cancellederror_inside_loop(self, tmp_path, monkeypatch):
        """The loop's own try/except CancelledError must not propagate.

        04-plan.md §1.14a says cancellation "stops" the pulse — it should not
        bubble out as an error from _progress_pulse_loop itself.
        """
        loop, _ = _build_loop(tmp_path)

        async def raise_cancelled(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(loop_module.asyncio, "sleep", raise_cancelled)
        # Should NOT raise — swallowed by the loop's except CancelledError.
        await loop._progress_pulse_loop(_slack_inbound())


# -----------------------------------------------------------------------------
# C. Pulse lifecycle inside _process_message
# -----------------------------------------------------------------------------


class TestPulseLifecycleInProcessMessage:
    async def test_pulse_cancelled_on_success(self, tmp_path, monkeypatch):
        """Fast inner response → finally cancels pulse → 0 pulse messages."""
        loop, bus = _build_loop(tmp_path)
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep([]))

        async def fast_inner(msg, session, key):
            return OutboundMessage.reply_to_inbound(msg, "done")

        loop._process_message_inner = fast_inner
        # Skip side-effects we don't care about for this test
        loop._maybe_consolidate = MagicMock()

        result = await loop._process_message(_slack_inbound())

        assert result is not None
        assert result.content == "done"
        # ACK was published, but no pulse messages (pulse_task cancelled before
        # it could complete its first sleep yield).
        outbound = _drain_outbound(bus)
        # First message is the ACK ("생각 중...")
        assert outbound[0].content == "\uc0dd\uac01 \uc911..."
        # Anything after the ACK that came from the pulse loop would carry
        # "(약" — assert none did.
        assert all("\uc57d" not in m.content for m in outbound[1:])

    async def test_pulse_cancelled_on_exception(self, tmp_path, monkeypatch):
        """Inner raises → finally still cancels pulse → exception propagates."""
        loop, bus = _build_loop(tmp_path)
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep([]))

        async def boom_inner(msg, session, key):
            raise RuntimeError("claude exploded")

        loop._process_message_inner = boom_inner
        loop._maybe_consolidate = MagicMock()

        with pytest.raises(RuntimeError, match="claude exploded"):
            await loop._process_message(_slack_inbound())

        # ACK is published before the inner runs; no pulse messages should leak.
        outbound = _drain_outbound(bus)
        assert outbound[0].content == "\uc0dd\uac01 \uc911..."
        assert all("\uc57d" not in m.content for m in outbound[1:])

    async def test_pulse_cancelled_on_cancellation(self, tmp_path, monkeypatch):
        """If _process_message is cancelled mid-flight, pulse cleanup still runs."""
        loop, bus = _build_loop(tmp_path)

        # Use a "park forever" sleep so the pulse task stays inside its first
        # `await asyncio.sleep(...)` and never completes its first iteration.
        # This is what happens in production when the response arrives in <5s.
        async def blocking_sleep(_seconds):
            await _REAL_SLEEP(60)

        monkeypatch.setattr(loop_module.asyncio, "sleep", blocking_sleep)

        inner_started = asyncio.Event()

        async def slow_inner(msg, session, key):
            inner_started.set()
            await _REAL_SLEEP(10)  # blocks until cancelled
            return None

        loop._process_message_inner = slow_inner
        loop._maybe_consolidate = MagicMock()

        task = asyncio.create_task(loop._process_message(_slack_inbound()))
        await inner_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # No pulse text leaked even though we cancelled.
        outbound = _drain_outbound(bus)
        assert outbound[0].content == "\uc0dd\uac01 \uc911..."
        assert all("\uc57d" not in m.content for m in outbound[1:])

    async def test_no_pulse_task_when_disabled(self, tmp_path, monkeypatch):
        """With the flag off, _process_message must not even create the task."""
        loop, bus = _build_loop(tmp_path, slack_pulse_enabled=False)

        # Spy on create_task — when the flag is off it must not be called for
        # the pulse loop. Other create_task calls inside the inner are bypassed
        # because we replace _process_message_inner with a no-op.
        sleeps: list[float] = []
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep(sleeps))

        async def fast_inner(msg, session, key):
            return None

        loop._process_message_inner = fast_inner
        loop._maybe_consolidate = MagicMock()

        await loop._process_message(_slack_inbound())

        # Pulse loop never invoked → no sleeps recorded.
        assert sleeps == []
        # Only the ACK was published, nothing pulse-shaped.
        outbound = _drain_outbound(bus)
        assert len(outbound) == 1
        assert outbound[0].content == "\uc0dd\uac01 \uc911..."

    async def test_no_pulse_task_for_telegram(self, tmp_path, monkeypatch):
        """Telegram inbound never gets a pulse task even if flag is on."""
        loop, bus = _build_loop(tmp_path, slack_pulse_enabled=True)
        sleeps: list[float] = []
        monkeypatch.setattr(loop_module.asyncio, "sleep", _make_fast_sleep(sleeps))

        async def fast_inner(msg, session, key):
            return None

        loop._process_message_inner = fast_inner
        loop._maybe_consolidate = MagicMock()

        await loop._process_message(_telegram_inbound())

        assert sleeps == []
        # ACK still goes out (channel-agnostic), but no pulse messages.
        outbound = _drain_outbound(bus)
        assert len(outbound) == 1
        assert outbound[0].content == "\uc0dd\uac01 \uc911..."

"""Unit tests for SlackChannel ACK reaction lifecycle.

Covers WO-P1-01 §3 — `_send_ack_reaction`, `_finalize_reaction`, error matrix,
TTL/maxsize bounds, and the metadata-driven `done_*` lifecycle dispatch in
`SlackChannel.send`. No real Slack API calls — `app.client` is an AsyncMock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from slack_sdk.errors import SlackApiError

from companio.bus import MessageBus, OutboundMessage
from companio.channels.slack import (
    _ACK_EYES,
    _ACK_REACTIONS_MAX_SIZE,
    SlackChannel,
)
from companio.config.schema import SlackConfig


def _build_slack_channel(*, ack_enabled: bool = True) -> tuple[SlackChannel, MagicMock]:
    """Construct a SlackChannel with a stub app.client (AsyncMock WebClient)."""
    config = SlackConfig(
        enabled=True,
        bot_token="xoxb-test",
        app_token="xapp-test",
        ack_reactions_enabled=ack_enabled,
    )
    bus = MessageBus()
    channel = SlackChannel(config=config, bus=bus)

    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "posted-ts", "ok": True})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_upload_v2 = AsyncMock(return_value={"ok": True})
    client.reactions_add = AsyncMock(return_value={"ok": True})
    client.reactions_remove = AsyncMock(return_value={"ok": True})

    app = MagicMock()
    app.client = client
    channel._app = app
    return channel, client


def _slack_error(code: str, *, retry_after: str | None = None) -> SlackApiError:
    """Build a SlackApiError whose `.response` mimics the WebClient response."""
    response = MagicMock()
    response.get = MagicMock(side_effect=lambda k, default=None: {"error": code, "ok": False}.get(k, default))
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    response.headers = headers
    return SlackApiError(message=f"slack error: {code}", response=response)


# -----------------------------------------------------------------------------
# A. _send_ack_reaction — happy path + dedup
# -----------------------------------------------------------------------------


class TestSendAckReaction:
    async def test_send_ack_reaction_calls_reactions_add_with_message_ts(self):
        channel, client = _build_slack_channel()
        await channel._send_ack_reaction("C1", "1712345678.000100")
        # Wait for fire-and-store task
        entry = channel._active_ack_reactions[("C1", "1712345678.000100")]
        await entry["task"]
        client.reactions_add.assert_awaited_once_with(
            channel="C1", timestamp="1712345678.000100", name=_ACK_EYES,
        )

    async def test_send_ack_reaction_idempotent_on_duplicate_key(self):
        channel, client = _build_slack_channel()
        await channel._send_ack_reaction("C1", "ts-1")
        await channel._send_ack_reaction("C1", "ts-1")  # duplicate event
        await channel._active_ack_reactions[("C1", "ts-1")]["task"]
        assert client.reactions_add.await_count == 1

    async def test_send_ack_reaction_disabled_flag_skips(self):
        channel, client = _build_slack_channel(ack_enabled=False)
        # Gate function should refuse the send
        assert channel._should_send_ack("ts-1") is False
        # And calling _send_ack_reaction directly should still work — gating is
        # the responsibility of the caller (handlers). Verify the gate.
        assert client.reactions_add.await_count == 0

    async def test_send_ack_reaction_disabled_runtime_skips(self):
        channel, client = _build_slack_channel(ack_enabled=True)
        channel._ack_reactions_runtime_disabled = True
        assert channel._should_send_ack("ts-1") is False
        assert client.reactions_add.await_count == 0


# -----------------------------------------------------------------------------
# B. _finalize_reaction — order of ops
# -----------------------------------------------------------------------------


class TestFinalizeReaction:
    async def test_finalize_reaction_removes_eyes_after_task_await(self):
        channel, client = _build_slack_channel()
        await channel._send_ack_reaction("C1", "ts-1")
        await channel._finalize_reaction("C1", "ts-1")
        client.reactions_add.assert_awaited_once_with(
            channel="C1", timestamp="ts-1", name=_ACK_EYES
        )
        client.reactions_remove.assert_awaited_once_with(
            channel="C1", timestamp="ts-1", name=_ACK_EYES
        )

    async def test_finalize_reaction_pops_dict_entry(self):
        channel, _ = _build_slack_channel()
        await channel._send_ack_reaction("C1", "ts-1")
        assert ("C1", "ts-1") in channel._active_ack_reactions
        await channel._finalize_reaction("C1", "ts-1")
        assert ("C1", "ts-1") not in channel._active_ack_reactions

    async def test_finalize_reaction_silent_when_no_prior_ack(self):
        channel, client = _build_slack_channel()
        await channel._finalize_reaction("C1", "never-ack")
        # No add task to await, no remove issued
        client.reactions_add.assert_not_awaited()
        client.reactions_remove.assert_not_awaited()


# -----------------------------------------------------------------------------
# C. Error matrix (04-plan §1.11)
# -----------------------------------------------------------------------------


class TestReactionErrorHandling:
    async def test_reactions_add_already_reacted_is_silent(self, caplog):
        channel, client = _build_slack_channel()
        client.reactions_add.side_effect = _slack_error("already_reacted")
        ok = await channel._add_reaction("C1", "ts-1", _ACK_EYES)
        assert ok is False
        assert channel._ack_reactions_runtime_disabled is False
        # No WARN/ERROR — silent
        for record in caplog.records:
            assert record.levelname not in ("WARNING", "ERROR")

    async def test_reactions_add_message_not_found_is_silent(self, caplog):
        channel, client = _build_slack_channel()
        client.reactions_add.side_effect = _slack_error("message_not_found")
        ok = await channel._add_reaction("C1", "ts-1", _ACK_EYES)
        assert ok is False
        for record in caplog.records:
            assert record.levelname not in ("WARNING", "ERROR")

    async def test_reactions_add_invalid_auth_disables_runtime(self):
        channel, client = _build_slack_channel()
        client.reactions_add.side_effect = _slack_error("invalid_auth")
        ok = await channel._add_reaction("C1", "ts-1", _ACK_EYES)
        assert ok is False
        assert channel._ack_reactions_runtime_disabled is True

    async def test_reactions_add_missing_scope_disables_runtime(self):
        channel, client = _build_slack_channel()
        client.reactions_add.side_effect = _slack_error("missing_scope")
        ok = await channel._add_reaction("C1", "ts-1", _ACK_EYES)
        assert ok is False
        assert channel._ack_reactions_runtime_disabled is True

    async def test_reactions_add_rate_limited_retries_once_then_silent(self, monkeypatch):
        channel, client = _build_slack_channel()
        # First call → ratelimited; retry inside _handle_reaction_error fails again
        client.reactions_add.side_effect = [
            _slack_error("ratelimited", retry_after="0"),
            _slack_error("ratelimited", retry_after="0"),
        ]
        # Skip the actual sleep
        async def _no_sleep(_):
            return None
        monkeypatch.setattr("companio.channels.slack.asyncio.sleep", _no_sleep)
        ok = await channel._add_reaction("C1", "ts-1", _ACK_EYES)
        assert ok is False
        assert client.reactions_add.await_count == 2
        # Runtime not disabled — ratelimited is recoverable
        assert channel._ack_reactions_runtime_disabled is False

    async def test_reactions_add_rate_limited_retry_succeeds(self, monkeypatch):
        channel, client = _build_slack_channel()
        client.reactions_add.side_effect = [
            _slack_error("ratelimited", retry_after="0"),
            {"ok": True},
        ]
        async def _no_sleep(_):
            return None
        monkeypatch.setattr("companio.channels.slack.asyncio.sleep", _no_sleep)
        ok = await channel._add_reaction("C1", "ts-1", _ACK_EYES)
        assert ok is True
        assert client.reactions_add.await_count == 2


# -----------------------------------------------------------------------------
# D. TTL / maxsize bounds
# -----------------------------------------------------------------------------


class TestBoundedAckCache:
    async def test_ttl_evicts_stale_entries(self):
        channel, _ = _build_slack_channel()
        # Inject a stale entry by hand
        old = datetime.now() - timedelta(hours=2)
        stale_task: asyncio.Task = asyncio.create_task(asyncio.sleep(0))
        await stale_task
        channel._active_ack_reactions[("C1", "old-ts")] = {
            "added_at": old, "task": stale_task,
        }
        # Now schedule a fresh ack — TTL cleanup runs first
        await channel._send_ack_reaction("C1", "new-ts")
        assert ("C1", "old-ts") not in channel._active_ack_reactions
        assert ("C1", "new-ts") in channel._active_ack_reactions
        # Cleanup the new task we just spawned
        await channel._active_ack_reactions[("C1", "new-ts")]["task"]

    async def test_maxsize_evicts_oldest(self):
        channel, _ = _build_slack_channel()
        # Pre-fill to one short of the limit so we can observe one eviction
        now = datetime.now()
        for i in range(_ACK_REACTIONS_MAX_SIZE):
            t: asyncio.Task = asyncio.create_task(asyncio.sleep(0))
            await t
            channel._active_ack_reactions[("C1", f"ts-{i}")] = {
                "added_at": now, "task": t,
            }
        # Adding one more should evict ts-0 (oldest insertion)
        await channel._send_ack_reaction("C1", "ts-fresh")
        assert ("C1", "ts-0") not in channel._active_ack_reactions
        assert ("C1", "ts-fresh") in channel._active_ack_reactions
        await channel._active_ack_reactions[("C1", "ts-fresh")]["task"]


# -----------------------------------------------------------------------------
# E. Independent lifecycles
# -----------------------------------------------------------------------------


class TestIndependentLifecycles:
    async def test_two_quick_mentions_have_independent_lifecycles(self):
        channel, client = _build_slack_channel()
        await channel._send_ack_reaction("C1", "ts-A")
        await channel._send_ack_reaction("C1", "ts-B")
        # Finalize B first, then A — order should not matter
        await channel._finalize_reaction("C1", "ts-B")
        await channel._finalize_reaction("C1", "ts-A")
        assert client.reactions_add.await_count == 2
        assert client.reactions_remove.await_count == 2
        added_ts = {call.kwargs["timestamp"] for call in client.reactions_add.call_args_list}
        removed_ts = {call.kwargs["timestamp"] for call in client.reactions_remove.call_args_list}
        assert added_ts == {"ts-A", "ts-B"}
        assert removed_ts == {"ts-A", "ts-B"}


# -----------------------------------------------------------------------------
# F. SlackChannel.send — done lifecycle metadata
# -----------------------------------------------------------------------------


class TestSendDoneLifecycle:
    async def test_send_with_done_success_metadata_finalizes_and_skips_text(self):
        channel, client = _build_slack_channel()
        await channel._send_ack_reaction("C1", "ts-1")
        await channel._active_ack_reactions[("C1", "ts-1")]["task"]

        out = OutboundMessage(
            channel="slack", chat_id="C1", content="",
            metadata={"_reaction_lifecycle": "done_success", "message_ts": "ts-1"},
        )
        await channel.send(out)

        client.reactions_remove.assert_awaited_once_with(
            channel="C1", timestamp="ts-1", name=_ACK_EYES
        )
        # Empty content → no text post
        client.chat_postMessage.assert_not_awaited()

    async def test_send_with_done_error_metadata_finalizes_and_skips_text(self):
        channel, client = _build_slack_channel()
        await channel._send_ack_reaction("C1", "ts-2")
        await channel._active_ack_reactions[("C1", "ts-2")]["task"]

        out = OutboundMessage(
            channel="slack", chat_id="C1", content="",
            metadata={"_reaction_lifecycle": "done_error", "message_ts": "ts-2"},
        )
        await channel.send(out)

        client.reactions_remove.assert_awaited_once()
        client.chat_postMessage.assert_not_awaited()

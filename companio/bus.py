"""Message bus: event types and async queue for channel-agent communication."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Keys that are safe to forward from InboundMessage.metadata to OutboundMessage.metadata.
# Add new keys here when a new channel needs a new thread/reply hint. Never include
# sensitive runtime context (role_prompt, role_name, credentials, etc.) — those must
# stay in separate dicts and never cross the bus boundary.
_FORWARDED_METADATA_KEYS = frozenset({
    "thread_ts",          # Slack: parent message ts for threaded reply
    "message_thread_id",  # Telegram: forum topic id
    "message_ts",         # Slack: original message ts (for reactions/updates)
    "message_id",         # Telegram: original message id (for reply-to)
    "is_channel",         # Slack: channel vs DM flag
    "is_group",           # Telegram: group chat flag
})


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # e.g. "telegram"
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def reply_to_inbound(
        cls,
        inbound: "InboundMessage",
        content: str,
        *,
        extra_metadata: dict[str, Any] | None = None,
        media: list[str] | None = None,
    ) -> "OutboundMessage":
        """Build an outbound reply that inherits thread/reply context from `inbound`.

        Only keys in `_FORWARDED_METADATA_KEYS` are copied; anything else (role_prompt,
        credentials, sender identity) stays inside the inbound and never leaks to
        outbound logs or downstream channels. `extra_metadata` overrides inherited keys.
        """
        md: dict[str, Any] = {
            k: v for k, v in (inbound.metadata or {}).items()
            if k in _FORWARDED_METADATA_KEYS
        }
        if extra_metadata:
            md.update(extra_metadata)
        return cls(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            content=content,
            media=media or [],
            metadata=md,
        )


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

"""Message sender for sending messages to users."""

from typing import Any, Awaitable, Callable

from companio.bus import InboundMessage, OutboundMessage


class MessageSender:
    """Sends messages to users on chat channels.

    The LLM-facing `send()` tool accepts only the legacy parameters (channel,
    chat_id, message_id, media) — do not add `metadata` to its signature, because
    it is exposed to the model and an untrusted model could poison internal state.

    Thread/reply context is stored internally via `set_context(inbound)` and
    propagated through `OutboundMessage.reply_to_inbound()`.
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._send_callback = send_callback
        self._default_inbound: InboundMessage | None = None
        self._sent_in_turn: bool = False

    def set_context(self, inbound: InboundMessage) -> None:
        """Bind the sender to the inbound message currently being processed.

        All subsequent `send()` calls in this turn default to replying to this
        inbound — preserving its channel, chat_id, and thread metadata.
        """
        self._default_inbound = inbound

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    async def send(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        inbound = self._default_inbound
        target_channel = channel or (inbound.channel if inbound else "")
        target_chat_id = chat_id or (inbound.chat_id if inbound else "")

        if not target_channel or not target_chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        # Build extra metadata. Only include message_id if explicitly provided —
        # do not leak the inbound's own message_id unless the caller asks for it.
        extra: dict[str, Any] = {}
        if message_id is not None:
            extra["message_id"] = message_id

        # If replying to the bound inbound, inherit its thread context via the
        # whitelist factory. Otherwise, build a bare message (caller is explicitly
        # targeting a different chat, so thread context does not apply).
        if inbound and target_channel == inbound.channel and target_chat_id == inbound.chat_id:
            msg = OutboundMessage.reply_to_inbound(
                inbound, content, extra_metadata=extra, media=media
            )
        else:
            msg = OutboundMessage(
                channel=target_channel,
                chat_id=target_chat_id,
                content=content,
                media=media or [],
                metadata=extra,
            )

        try:
            await self._send_callback(msg)
            # Mark turn as sent only if targeting the same chat AND same thread as
            # the bound inbound. Comparing thread_ts/message_thread_id prevents a
            # cross-thread send from suppressing the final reply to the original.
            if inbound and target_channel == inbound.channel and target_chat_id == inbound.chat_id:
                inbound_thread = (inbound.metadata or {}).get("thread_ts") or (inbound.metadata or {}).get("message_thread_id")
                msg_thread = msg.metadata.get("thread_ts") or msg.metadata.get("message_thread_id")
                if inbound_thread == msg_thread:
                    self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {target_channel}:{target_chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"

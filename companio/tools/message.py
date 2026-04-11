"""Message sender for sending messages to users."""

from typing import Any, Awaitable, Callable

from loguru import logger

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
        broadcast_enabled: bool = False,
        broadcast_blocked_channels: list[str] | None = None,
    ):
        self._send_callback = send_callback
        self._default_inbound: InboundMessage | None = None
        self._sent_in_turn: bool = False
        self._broadcast_enabled: bool = broadcast_enabled
        self._broadcast_blocked_channels: list[str] = list(broadcast_blocked_channels or [])
        # Tracks whether the LLM invoked share_to_channel=True at any point in the
        # current turn. Used by the shadow detector in AgentLoop to spot trigger
        # phrases the LLM failed to honor.
        self._broadcast_called: bool = False

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
        self._broadcast_called = False

    async def send(
        self,
        content: str,
        share_to_channel: bool = False,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        """Send a message to the current chat context.

        Args:
            content: 메시지 내용.
            share_to_channel: True면 채널 본문에도 함께 공지 (스레드 컨텍스트에서만 유효).
                반드시 사용자가 명시적으로 "채널에 공유/공지/알려"를 요청한 경우에만 사용.
                애매하면 False로 두세요. 비가역 액션이라 한 번 broadcast된 메시지는
                취소할 수 없습니다.
            channel: 명시적 채널 override (보통 사용 X).
            chat_id: 명시적 chat_id override (보통 사용 X).
            message_id: 명시적 message_id override (보통 사용 X).
            media: 첨부 파일 경로 목록.
        """
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

        # 3중 안전장치: share_to_channel=True 가 실효하려면
        #   1. inbound.metadata["is_channel"] == True
        #   2. inbound.metadata["thread_ts"] 존재
        #   3. SlackConfig.broadcast_enabled == True
        # + blocked_channels에 등록되지 않아야 함.
        broadcast_skip_reason: str | None = None
        if share_to_channel:
            self._broadcast_called = True
            inbound_metadata = (inbound.metadata or {}) if inbound else {}
            is_channel = bool(inbound_metadata.get("is_channel"))
            has_thread = bool(inbound_metadata.get("thread_ts"))
            blocked = bool(inbound) and inbound.chat_id in self._broadcast_blocked_channels

            if not is_channel:
                broadcast_skip_reason = "not a channel"
            elif not has_thread:
                broadcast_skip_reason = "no thread context"
            elif not self._broadcast_enabled:
                broadcast_skip_reason = "broadcast disabled"
            elif blocked:
                broadcast_skip_reason = "channel blocked"
            else:
                extra["reply_broadcast"] = True

            if broadcast_skip_reason:
                logger.warning("share_to_channel ignored: {}", broadcast_skip_reason)

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
            base = f"Message sent to {target_channel}:{target_chat_id}{media_info}"
            if broadcast_skip_reason:
                return f"{base} (Note: channel broadcast skipped — {broadcast_skip_reason})"
            return base
        except Exception as e:
            return f"Error sending message: {str(e)}"

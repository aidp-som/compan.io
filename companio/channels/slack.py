"""Slack channel implementation using slack-bolt (Socket Mode)."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from loguru import logger

from companio.bus import MessageBus, OutboundMessage
from companio.channels.base import BaseChannel
from companio.config.schema import SlackConfig
from companio.helpers import split_message

SLACK_MAX_MESSAGE_LEN = 3000  # Slack 메시지 안전 길이
MAX_RECONNECT_FAILURES = 5


def _markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format.

    Protects code blocks from transformation, then applies conversions.
    """
    if not text:
        return ""

    # 1. Extract and protect fenced code blocks
    code_blocks: list[str] = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))  # keep entire ```...``` block as-is
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?[\s\S]*?```", save_code_block, text)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`]+`", save_inline_code, text)

    # 3. Links [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # 4. Headings # Title → *Title* (bold)  — use placeholder to protect from italic pass
    bold_phs: list[str] = []

    def _save_heading_bold(m: re.Match) -> str:
        bold_phs.append(m.group(1))
        return f"\x00BD{len(bold_phs) - 1}\x00"

    text = re.sub(r"^#{1,6}\s+(.+)$", _save_heading_bold, text, flags=re.MULTILINE)

    # 5. Bold **text** or __text__ → placeholder (protect from italic pass)
    def _save_bold(m: re.Match) -> str:
        bold_phs.append(m.group(1))
        return f"\x00BD{len(bold_phs) - 1}\x00"

    text = re.sub(r"\*\*(.+?)\*\*", _save_bold, text)
    text = re.sub(r"__(.+?)__", _save_bold, text)

    # 6. Italic *text* → _text_ (single asterisks only, now safe after bold extraction)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)

    # 6b. Restore bold placeholders → *text*
    for i, content in enumerate(bold_phs):
        text = text.replace(f"\x00BD{i}\x00", f"*{content}*")

    # 7. Strikethrough ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # 8. Bullet lists: - item → • item
    text = re.sub(r"^- ", "• ", text, flags=re.MULTILINE)

    # 9. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    # 10. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", code)

    return text


class SlackChannel(BaseChannel):
    """Slack channel using Socket Mode for DM conversations."""

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus, workspace: Path | None = None):
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._workspace = workspace
        self._app = None  # AsyncApp
        self._handler = None  # AsyncSocketModeHandler
        self._bot_user_id: str | None = None  # 봇 자기 user ID
        self._active_threads: dict[str, set[str]] = {}  # channel_id -> set of thread_ts
        self._progress_messages: dict[str, str] = {}  # chat_id:thread_ts → message ts
        self._consecutive_failures = 0
        self._log = logger.bind(channel="slack")

    async def start(self) -> None:
        """Start the Slack bot with Socket Mode."""
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        if not self.config.bot_token or not self.config.app_token:
            self._log.error("Slack bot_token or app_token not configured")
            return

        self._running = True

        # Build AsyncApp
        self._app = AsyncApp(token=self.config.bot_token)

        # Register event handlers
        @self._app.event("app_mention")
        async def handle_mention(event, say):  # noqa: ARG001
            await self._on_app_mention(event)

        @self._app.event("message")
        async def handle_message(event, say):  # noqa: ARG001
            await self._on_message(event)

        # Connect via Socket Mode
        self._handler = AsyncSocketModeHandler(self._app, self.config.app_token)
        await self._handler.connect_async()

        # Get bot's own user ID via auth.test
        try:
            auth_result = await self._app.client.auth_test()
            self._bot_user_id = auth_result.get("user_id")
            self._log.info("Slack bot connected (user_id={})", self._bot_user_id)
        except Exception as e:
            self._log.error("Failed to get Slack bot identity: {}", e)

        # Keep running with health check
        while self._running:
            await asyncio.sleep(5)
            try:
                connected = await self._handler.client.is_connected()
            except Exception:
                connected = False

            if not connected:
                self._consecutive_failures += 1
                self._log.error(
                    "Slack WebSocket disconnected (failure {}/{})",
                    self._consecutive_failures,
                    MAX_RECONNECT_FAILURES,
                )
                if self._consecutive_failures >= MAX_RECONNECT_FAILURES:
                    self._log.critical(
                        "Slack reconnection failed {} times, exiting for systemd restart",
                        MAX_RECONNECT_FAILURES,
                    )
                    sys.exit(1)
            else:
                if self._consecutive_failures > 0:
                    self._log.info(
                        "Slack WebSocket reconnected after {} failures",
                        self._consecutive_failures,
                    )
                self._consecutive_failures = 0

    async def stop(self) -> None:
        """Stop the Slack bot."""
        self._running = False
        if self._handler:
            await self._handler.close()
            self._handler = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Slack."""
        if not self._app:
            self._log.warning("Slack bot not running")
            return

        is_progress = msg.metadata.get("_progress", False)
        thread_ts = msg.metadata.get("thread_ts")
        progress_key = f"{msg.chat_id}:{thread_ts or ''}"

        mrkdwn_text = _markdown_to_mrkdwn(msg.content) if msg.content else ""

        # Upload media files
        for media_path in msg.media or []:
            try:
                await self._app.client.files_upload_v2(
                    channel=msg.chat_id,
                    file=media_path,
                    thread_ts=thread_ts,
                )
            except Exception as e:
                self._log.error("Failed to upload file {}: {}", media_path, e)

        if not mrkdwn_text:
            return

        if is_progress and progress_key in self._progress_messages:
            # Edit existing progress message
            try:
                await self._app.client.chat_update(
                    channel=msg.chat_id,
                    ts=self._progress_messages[progress_key],
                    text=mrkdwn_text,
                )
                return
            except Exception as e:
                self._log.debug("Failed to update progress message: {}", e)
                # fallthrough to post new message

        # Post new message
        try:
            for chunk in split_message(mrkdwn_text, SLACK_MAX_MESSAGE_LEN):
                result = await self._app.client.chat_postMessage(
                    channel=msg.chat_id,
                    text=chunk,
                    thread_ts=thread_ts,
                )
            if is_progress:
                self._progress_messages[progress_key] = result["ts"]
            else:
                # 최종 응답이면 progress 메시지 추적 제거
                self._progress_messages.pop(progress_key, None)
        except Exception as e:
            self._log.error("Error sending Slack message to {}: {}", msg.chat_id, e)

    async def _download_files(self, event: dict) -> list[str]:
        """Download files attached to a Slack message to workspace."""
        files = event.get("files", [])
        if not files or not self._workspace:
            return []

        media_dir = self._workspace / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for file_info in files:
            url = file_info.get("url_private")
            if not url:
                continue

            # 보안: 도메인 화이트리스트
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.hostname not in ("files.slack.com", "files-pri.slack.com"):
                self._log.warning("Blocked file download from non-Slack domain: {}", parsed.hostname)
                continue

            # 파일명 안전화
            filename = file_info.get("name", f"slack_file_{file_info.get('id', 'unknown')}")
            # 경로 traversal 방지
            safe_name = Path(filename).name  # .. 제거
            target = (media_dir / safe_name).resolve()
            if not str(target).startswith(str(media_dir.resolve())):
                self._log.warning("Path traversal detected, skipping: {}", filename)
                continue

            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {self.config.bot_token}"}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            target.write_bytes(await resp.read())
                            downloaded.append(str(target))
                            self._log.debug("Downloaded Slack file: {}", safe_name)
                        else:
                            self._log.warning("Failed to download {}: HTTP {}", safe_name, resp.status)
            except Exception as e:
                self._log.error("Error downloading Slack file {}: {}", safe_name, e)

        return downloaded

    async def _on_app_mention(self, event: dict) -> None:
        """Handle app_mention events in channels."""
        if event.get("bot_id"):
            return

        sender_id = event["user"]

        if not self.is_allowed(sender_id):
            self._log.debug("Slack mention from unauthorized user {}", sender_id)
            return

        chat_id = event["channel"]
        content = event.get("text", "")
        content = re.sub(rf"<@{self._bot_user_id}>", "", content).strip()

        thread_ts = event.get("thread_ts") or event["ts"]

        # Track active thread
        self._active_threads.setdefault(chat_id, set()).add(thread_ts)

        # Download attached files
        media = await self._download_files(event)

        session_key = f"slack:{chat_id}:{thread_ts}"
        metadata = {
            "user_id": event["user"],
            "thread_ts": thread_ts,
            "message_ts": event.get("ts"),
            "is_channel": True,
        }

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata=metadata,
            session_key=session_key,
            media=media,
        )

    def _is_active_thread(self, channel_id: str, thread_ts: str) -> bool:
        """Check if a thread is actively tracked."""
        return thread_ts in self._active_threads.get(channel_id, set())

    async def _on_message(self, event: dict) -> None:
        """Handle incoming Slack message events."""
        # Filter out bot's own messages
        if event.get("bot_id") or event.get("user") == self._bot_user_id:
            return

        # Filter out subtypes (edits, deletes, etc.)
        if event.get("subtype"):
            return

        channel_type = event.get("channel_type", "")
        thread_ts = event.get("thread_ts")

        # DM: existing logic
        if channel_type == "im":
            sender_id = event["user"]
            chat_id = event["channel"]
            content = event.get("text", "")

            # Download attached files
            media = await self._download_files(event)

            metadata = {
                "user_id": event["user"],
                "thread_ts": event.get("thread_ts") or event.get("ts"),
                "message_ts": event.get("ts"),
                "is_channel": False,
            }

            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata=metadata,
                media=media,
            )
            return

        # Channel message: only handle if in an active thread (no mention needed)
        # Skip if message contains bot mention — app_mention handler will process it
        if self._bot_user_id and f"<@{self._bot_user_id}>" in event.get("text", ""):
            return
        if thread_ts and self._is_active_thread(event["channel"], thread_ts):
            sender_id = event["user"]
            if not self.is_allowed(sender_id):
                self._log.debug("Slack thread message from unauthorized user {}", sender_id)
                return
            chat_id = event["channel"]
            content = event.get("text", "")
            content = re.sub(rf"<@{self._bot_user_id}>", "", content).strip()

            # Download attached files
            media = await self._download_files(event)

            session_key = f"slack:{chat_id}:{thread_ts}"
            metadata = {
                "user_id": event["user"],
                "thread_ts": thread_ts,
                "message_ts": event.get("ts"),
                "is_channel": True,
            }
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata=metadata,
                session_key=session_key,
                media=media,
            )

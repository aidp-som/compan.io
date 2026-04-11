"""Slack channel implementation using slack-bolt (Socket Mode)."""

from __future__ import annotations

import asyncio
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from companio.bus import MessageBus, OutboundMessage
from companio.channels.base import BaseChannel
from companio.config.schema import SlackConfig
from companio.helpers import split_message

SLACK_MAX_MESSAGE_LEN = 3000  # Slack 메시지 안전 길이
MAX_RECONNECT_FAILURES = 5

# ACK reaction lifecycle constants (04-plan §1.9, §1.14)
_ACK_REACTIONS_TTL = timedelta(hours=1)
_ACK_REACTIONS_MAX_SIZE = 500
_ACK_EYES = "eyes"

WINDOWS_SLACK_INSTALL_GUIDE = """\
안녕하세요! SOM ERP Slack에 오신 것을 환영합니다 :wave:

*Windows용 Slack 설치 방법*

1. 아래 링크에서 Slack 설치 파일을 다운로드하세요.
   https://slack.com/downloads/windows

2. 다운로드된 `SlackSetup.exe` 파일을 실행합니다.

3. 설치가 완료되면 Slack을 실행하고 *Sign in to Slack* 을 클릭합니다.

4. 워크스페이스 URL 또는 초대받은 이메일 주소로 로그인합니다.

5. 로그인하면 자동으로 워크스페이스에 연결됩니다.

문의사항이 있으시면 언제든지 말씀해주세요!
"""


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
        # Per-progress-key lock so the "check cache → post/update → record cache"
        # sequence is atomic. Without this, two rapid _progress messages for the
        # same thread could both miss the cache and post duplicate messages.
        self._progress_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # ACK reaction lifecycle state (04-plan §1.9). Bounded OrderedDict so a
        # missed `done` event cannot leak memory; insertion order doubles as LRU.
        # value: {"added_at": datetime, "task": asyncio.Task[bool]}
        self._active_ack_reactions: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._ack_reactions_lock = asyncio.Lock()
        # Flips to True if the workspace is missing the reactions:write scope or the
        # token is invalid — prevents repeated 4xx storms (04-plan §1.11).
        self._ack_reactions_runtime_disabled = False
        # Suppress repeated WARN spam for the same chat after the bot is removed.
        self._ack_not_in_channel_warned: set[str] = set()
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

        @self._app.event("member_joined_channel")
        async def handle_member_joined(event, say):  # noqa: ARG001
            await self._on_member_joined(event)

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

        # Done lifecycle (04-plan §1.3): a reaction-only outbound carries
        # _reaction_lifecycle in metadata and (usually) empty content. Finalize the
        # 👀 reaction here, then either skip text dispatch (empty content) or fall
        # through to send the text payload alongside.
        lifecycle = msg.metadata.get("_reaction_lifecycle")
        if lifecycle in ("done_success", "done_error"):
            message_ts = msg.metadata.get("message_ts")
            if message_ts:
                await self._finalize_reaction(msg.chat_id, message_ts)
            if not msg.content:
                return  # reaction-only outbound — no text to send

        is_progress = msg.metadata.get("_progress", False)
        thread_ts = msg.metadata.get("thread_ts")
        # Only cache/edit progress messages when a real thread context exists.
        # DMs and bare channel posts (thread_ts falsy) always get fresh messages
        # so the cache key cannot collapse and cross-contaminate unrelated chats.
        use_progress_cache = is_progress and bool(thread_ts)
        thread_key = f"{msg.chat_id}:{thread_ts}" if thread_ts else ""

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

        # Serialize the "check cache → post/update → record" sequence per thread.
        # Without this, concurrent progress sends for the same thread race and
        # post duplicates. Sends without a thread context take a per-chat fallback
        # lock — effectively a no-op unless two bare posts collide.
        lock_key = thread_key or f"nothread:{msg.chat_id}"
        async with self._progress_locks[lock_key]:
            if use_progress_cache and thread_key in self._progress_messages:
                # Edit existing progress message in place
                try:
                    await self._app.client.chat_update(
                        channel=msg.chat_id,
                        ts=self._progress_messages[thread_key],
                        text=mrkdwn_text,
                    )
                    return
                except Exception as e:
                    self._log.debug("Failed to update progress message: {}", e)
                    # fallthrough to post new message

            # Defense in depth: even if MessageSender's 3-layer guard misfires,
            # SlackChannel re-validates every condition before honoring
            # reply_broadcast=True. The flag is dropped if any check fails.
            reply_broadcast_flag = (
                bool(msg.metadata.get("reply_broadcast", False))
                and bool(thread_ts)
                and bool(msg.metadata.get("is_channel", False))
                and msg.chat_id not in self.config.broadcast_blocked_channels
            )

            # Post new message(s)
            try:
                chunks = list(split_message(mrkdwn_text, SLACK_MAX_MESSAGE_LEN))
                for i, chunk in enumerate(chunks):
                    is_last = i == len(chunks) - 1
                    chunk_broadcast = reply_broadcast_flag and is_last
                    result = await self._app.client.chat_postMessage(
                        channel=msg.chat_id,
                        text=chunk,
                        thread_ts=thread_ts,
                        reply_broadcast=chunk_broadcast,
                    )
                    if chunk_broadcast:
                        self._log.info(
                            "slack.broadcast.sent chat_id={} thread_ts={} content_len={}",
                            msg.chat_id,
                            thread_ts,
                            len(chunk),
                        )
                if use_progress_cache:
                    self._progress_messages[thread_key] = result["ts"]
                elif thread_key:
                    # Final response in a thread — drop any tracked progress entry
                    self._progress_messages.pop(thread_key, None)
            except Exception as e:
                self._log.error("Error sending Slack message to {}: {}", msg.chat_id, e)

    # ------------------------------------------------------------------
    # Reaction helpers (04-plan §1.2, §1.11)
    # ------------------------------------------------------------------

    async def _add_reaction(self, chat_id: str, message_ts: str, name: str) -> bool:
        """Add a reaction to a message.

        Graceful per the 9-error matrix in 04-plan.md §1.11. Never raises;
        returns True only on a confirmed `ok` from the Slack API.
        """
        if not self._app:
            return False
        try:
            await self._app.client.reactions_add(
                channel=chat_id, timestamp=message_ts, name=name
            )
            self._log.debug(
                "slack.reaction.add status=ok chat_id={} message_ts={}",
                chat_id, message_ts,
            )
            return True
        except Exception as e:
            return await self._handle_reaction_error(
                e, chat_id, message_ts, name, op="add"
            )

    async def _remove_reaction(self, chat_id: str, message_ts: str, name: str) -> bool:
        """Remove a reaction from a message. Same graceful contract as add."""
        if not self._app:
            return False
        try:
            await self._app.client.reactions_remove(
                channel=chat_id, timestamp=message_ts, name=name
            )
            self._log.debug(
                "slack.reaction.remove status=ok chat_id={} message_ts={}",
                chat_id, message_ts,
            )
            return True
        except Exception as e:
            return await self._handle_reaction_error(
                e, chat_id, message_ts, name, op="remove"
            )

    async def _handle_reaction_error(
        self, exc: Exception, chat_id: str, message_ts: str, name: str, *, op: str,
    ) -> bool:
        """Map a SlackApiError (or other) onto the 9-error matrix in 04-plan §1.11.

        Returns True only if a retry succeeded; otherwise False. Never raises.
        """
        # Lazy import keeps the slack_sdk dependency optional at module import time.
        slack_api_error_cls: type | None
        try:
            from slack_sdk.errors import SlackApiError as slack_api_error_cls
        except Exception:  # pragma: no cover - slack_sdk should always be present
            slack_api_error_cls = None

        if slack_api_error_cls is None or not isinstance(exc, slack_api_error_cls):
            self._log.warning(
                "slack.reaction.{} status=error chat_id={} message_ts={} err={}",
                op, chat_id, message_ts, exc,
            )
            return False

        response = getattr(exc, "response", None)
        err: str | None = None
        if response is not None and hasattr(response, "get"):
            try:
                err = response.get("error")
            except Exception:
                err = None

        # Silent / idempotent cases — debug only.
        if err in ("already_reacted", "message_not_found", "no_reaction", "not_reacted"):
            self._log.debug(
                "slack.reaction.{} status={} chat_id={} message_ts={}",
                op, err, chat_id, message_ts,
            )
            return False

        # Bot kicked from channel — warn once per chat to avoid spam.
        if err == "not_in_channel":
            if chat_id not in self._ack_not_in_channel_warned:
                self._ack_not_in_channel_warned.add(chat_id)
                self._log.warning(
                    "slack.reaction.{} status=not_in_channel chat_id={} (warned once)",
                    op, chat_id,
                )
            return False

        # Hard auth/scope failures — disable runtime so we stop hammering the API.
        if err in ("invalid_auth", "missing_scope"):
            self._ack_reactions_runtime_disabled = True
            self._log.error(
                "slack.reaction.{} status={} chat_id={} message_ts={} — "
                "runtime-disabling ack reactions",
                op, err, chat_id, message_ts,
            )
            return False

        # Rate limited — single retry honoring Retry-After.
        if err == "ratelimited":
            retry_after = 1
            try:
                headers = getattr(response, "headers", None) or {}
                ra = headers.get("Retry-After") if hasattr(headers, "get") else None
                if ra is not None:
                    retry_after = int(ra)
            except Exception:
                pass
            self._log.warning(
                "slack.reaction.{} status=ratelimited chat_id={} message_ts={} "
                "retry_after={}s",
                op, chat_id, message_ts, retry_after,
            )
            try:
                await asyncio.sleep(retry_after)
                if op == "add":
                    await self._app.client.reactions_add(
                        channel=chat_id, timestamp=message_ts, name=name
                    )
                else:
                    await self._app.client.reactions_remove(
                        channel=chat_id, timestamp=message_ts, name=name
                    )
                self._log.debug(
                    "slack.reaction.{} status=ok_after_retry chat_id={} message_ts={}",
                    op, chat_id, message_ts,
                )
                return True
            except Exception as retry_exc:
                self._log.warning(
                    "slack.reaction.{} status=retry_failed chat_id={} message_ts={} "
                    "err={}",
                    op, chat_id, message_ts, retry_exc,
                )
                return False

        # Anything else — defensive WARN, no raise.
        self._log.warning(
            "slack.reaction.{} status=error chat_id={} message_ts={} err={}",
            op, chat_id, message_ts, err or exc,
        )
        return False

    async def _send_ack_reaction(self, chat_id: str, message_ts: str) -> None:
        """Schedule a 👀 reaction add for (chat_id, message_ts), idempotent.

        Runs lock-protected only for the bookkeeping; the actual API call happens
        inside the asyncio.Task so it never blocks the inbound dispatch path
        (04-plan §1.2 — lock-free latency is the core value).
        """
        key = (chat_id, message_ts)
        async with self._ack_reactions_lock:
            now = datetime.now()
            # Lazy TTL cleanup so a quiet bot does not accumulate stale entries.
            expired = [
                k for k, v in self._active_ack_reactions.items()
                if now - v["added_at"] > _ACK_REACTIONS_TTL
            ]
            for k in expired:
                self._active_ack_reactions.pop(k, None)
            # Bound size — evict oldest insertion.
            while len(self._active_ack_reactions) >= _ACK_REACTIONS_MAX_SIZE:
                self._active_ack_reactions.popitem(last=False)
            # Dedup: a Slack retry of the same event must not double-add.
            if key in self._active_ack_reactions:
                return
            task = asyncio.create_task(
                self._add_reaction(chat_id, message_ts, _ACK_EYES)
            )
            self._active_ack_reactions[key] = {"added_at": now, "task": task}

    async def _finalize_reaction(self, chat_id: str, message_ts: str) -> None:
        """Finalize the lifecycle: await the pending add task then remove the eyes.

        Awaiting the add task first prevents the add/remove race when a Claude
        turn is so fast it overlaps the in-flight `reactions.add` (04-plan §1.9).
        Silent if no prior ack was registered for this key.
        """
        key = (chat_id, message_ts)
        async with self._ack_reactions_lock:
            entry = self._active_ack_reactions.pop(key, None)
        if entry is None:
            return
        task = entry.get("task")
        if task is not None:
            try:
                await task
            except Exception:
                # Add already swallowed its own errors; defensive only.
                pass
        await self._remove_reaction(chat_id, message_ts, _ACK_EYES)

    def _should_send_ack(self, message_ts: str | None) -> bool:
        """Gate for ack reactions: feature flag, runtime disable, presence of ts."""
        return bool(
            self.config.ack_reactions_enabled
            and not self._ack_reactions_runtime_disabled
            and message_ts
        )

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

        # Fire-and-store the 👀 ack outside any per-session lock so direct-prior
        # work in flight cannot delay the user-visible reaction (04-plan §1.2).
        if self._should_send_ack(metadata.get("message_ts")):
            asyncio.create_task(
                self._send_ack_reaction(chat_id, metadata["message_ts"])
            )

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

            # DM ack — same lock-free fire-and-store as channel mentions.
            if self._should_send_ack(metadata.get("message_ts")):
                asyncio.create_task(
                    self._send_ack_reaction(chat_id, metadata["message_ts"])
                )

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

            # Channel-thread ack — same lock-free fire-and-store.
            if self._should_send_ack(metadata.get("message_ts")):
                asyncio.create_task(
                    self._send_ack_reaction(chat_id, metadata["message_ts"])
                )

            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata=metadata,
                session_key=session_key,
                media=media,
            )

    async def _on_member_joined(self, event: dict) -> None:
        """Handle member_joined_channel — send Windows Slack install guide via DM."""
        user_id = event.get("user")
        if not user_id or not self._app:
            return

        # 봇 자신이 채널에 입장한 경우 무시
        if user_id == self._bot_user_id:
            return

        try:
            # DM 채널 열기
            dm_result = await self._app.client.conversations_open(users=user_id)
            dm_channel = dm_result["channel"]["id"]

            await self._app.client.chat_postMessage(
                channel=dm_channel,
                text=WINDOWS_SLACK_INSTALL_GUIDE,
            )
            self._log.info("Sent Windows Slack install guide to new member {}", user_id)
        except Exception as e:
            self._log.error("Failed to send welcome message to {}: {}", user_id, e)

"""Agent loop: Claude CLI subprocess delegation."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from companio.bus import InboundMessage, MessageBus, OutboundMessage
from companio.core.claude_cli import ClaudeCLI
from companio.core.context import ContextBuilder
from companio.core.memory import MemoryStore
from companio.helpers import filter_secrets
from companio.session import Session, SessionManager
from companio.tools.message import MessageSender

if TYPE_CHECKING:
    from companio.cron import CronService

from companio.config.schema import Config, RoleConfig


class AgentLoop:
    """Delegates processing to Claude CLI subprocess."""

    def __init__(
        self,
        bus: MessageBus,
        claude: ClaudeCLI,
        workspace: Path,
        memory_window: int = 200,
        bot_name: str = "companio",
        cron_service: CronService | None = None,
        session_manager: SessionManager | None = None,
        config: Config | None = None,
    ):
        self.bus = bus
        self.claude = claude
        self.workspace = workspace
        self.memory_window = memory_window
        self.cron_service = cron_service
        self.context = ContextBuilder(workspace, bot_name=bot_name)
        self._session_manager = session_manager or SessionManager(workspace)
        self._config = config
        self.message_sender = MessageSender(send_callback=bus.publish_outbound)
        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._consolidating: set[str] = set()
        self._consolidation_tasks: set[asyncio.Task] = set()
        # Claude CLI session IDs per session key (for --resume)
        self._claude_session_ids: dict[str, str] = {}
        # Track cumulative cost per session key for per-turn diff
        self._claude_session_costs: dict[str, float] = {}
        # Idle consolidation timers
        self._idle_timers: dict[str, asyncio.TimerHandle] = {}
        self._idle_timeout = config.agents.defaults.idle_consolidation_timeout if config else 1800
        self._idle_min_turns = config.agents.defaults.idle_consolidation_min_turns if config else 4

    def _resolve_role(self, sender_id: str) -> tuple[str | None, RoleConfig | None]:
        """Resolve the sender's role name and config. Returns (None, None) if no role system configured."""
        if not self._config or not self._config.roles:
            return None, None  # No roles configured — full access (backwards compatible)

        # Try exact match first, then pipe-split parts (telegram "id|username" format)
        role_name = self._config.user_roles.get(sender_id)
        if not role_name and "|" in sender_id:
            sid, username = sender_id.split("|", 1)
            role_name = self._config.user_roles.get(sid) or self._config.user_roles.get(username)
        if not role_name:
            role_name = self._config.default_role

        if not role_name:
            return None, None  # No default role — deny access handled by ACL

        return role_name, self._config.roles.get(role_name)

    async def run(self) -> None:
        """Main loop - consume messages from bus."""
        self._running = True
        await self._session_manager.initialize()
        logger.info("Agent loop started")

        try:
            while self._running:
                try:
                    msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if msg.content.strip().lower() in ("/stop", "!stop"):
                    await self._handle_stop(msg)
                else:
                    task = asyncio.create_task(self._dispatch(msg))
                    self._active_tasks.setdefault(msg.session_key, []).append(task)
                    task.add_done_callback(
                        lambda t, k=msg.session_key: (
                            self._active_tasks.get(k, [])
                            and t in self._active_tasks.get(k, [])
                            and self._active_tasks[k].remove(t)
                        )
                    )
        finally:
            await self._session_manager.close()

    def stop(self) -> None:
        self._running = False
        for timer in self._idle_timers.values():
            timer.cancel()
        self._idle_timers.clear()
        logger.info("Agent loop stopping")

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel active tasks for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        content = f"Stopped {cancelled} task(s)." if cancelled else "No active task to stop."
        await self.bus.publish_outbound(
            OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)
        )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process under per-session lock."""
        async with self._session_locks[msg.session_key]:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    )
                )

    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """Process a single message via Claude CLI."""
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = msg.session_key
        session = await self._session_manager.get_or_create(key)

        # Slash commands (support both /cmd and !cmd for Slack compatibility)
        cmd = msg.content.strip().lower()
        if cmd in ("/new", "!new"):
            return await self._handle_new(msg, session)
        if cmd in ("/help", "!help"):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="companio commands:\n/new or !new \u2014 Start a new conversation\n/stop or !stop \u2014 Stop the current task\n/help or !help \u2014 Show available commands",
            )

        # Background consolidation if needed
        self._maybe_consolidate(session)

        # Set message sender context
        self.message_sender.set_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        self.message_sender.start_turn()

        # Send ACK to user (so they know we're processing)
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="\uc0dd\uac01 \uc911...",
                metadata={"_progress": True},
            )
        )

        # Resolve role-based tool restrictions
        role_name, role = self._resolve_role(msg.sender_id)
        role_tools: dict = {}
        if role:
            if role.allowed_tools is not None:
                role_tools["allowed_tools"] = role.allowed_tools
            if role.disallowed_tools is not None:
                role_tools["disallowed_tools"] = role.disallowed_tools
        # Inject role info into metadata for runtime context
        if role_name and msg.metadata is not None:
            msg.metadata["role_name"] = role_name
            if role and role.role_prompt:
                msg.metadata["role_prompt"] = role.role_prompt

        # Check if we have an existing Claude CLI session for this chat
        claude_sid = self._claude_session_ids.get(key)

        # Fallback to DB-persisted session ID
        if not claude_sid and session.claude_session_id:
            # TTL check: ignore sessions older than 4 hours
            if session.claude_session_updated_at:
                from datetime import timedelta
                try:
                    updated = datetime.fromisoformat(session.claude_session_updated_at)
                    if datetime.now() - updated < timedelta(hours=4):
                        claude_sid = session.claude_session_id
                        logger.info("Restored Claude session {} from DB for {}", claude_sid, key)
                except (ValueError, TypeError):
                    pass

        if claude_sid:
            # Resume existing session — no system prompt or history needed
            runtime_ctx = ContextBuilder._build_runtime_context(msg.channel, msg.chat_id, msg.metadata)
            full_message = f"{runtime_ctx}\n\n{msg.content}"
            response = await self.claude.run(
                message=full_message, resume_session_id=claude_sid,
                **role_tools,
            )
            # Resume failed — fallback to new session
            if response.is_error:
                logger.warning("Resume failed for {}, creating new session", key)
                self._claude_session_ids.pop(key, None)
                self._claude_session_costs.pop(key, None)
                session.claude_session_id = None
                claude_sid = None  # Will fall through to if not claude_sid block below

        if not claude_sid:
            # First call — write CLAUDE.md and inject history if available
            self.context.write_claude_md(self.claude.project_dir)
            runtime_ctx = ContextBuilder._build_runtime_context(msg.channel, msg.chat_id, msg.metadata)
            history_text = ContextBuilder.format_history(session.messages[-self.memory_window:])

            full_message = f"{runtime_ctx}\n\n"
            if history_text:
                full_message += f"## Recent Conversation\n{history_text}\n\n"
            full_message += f"## Current Message\n{msg.content}"

            new_session_id = str(uuid.uuid4())
            response = await self.claude.run(
                message=full_message,
                session_id=new_session_id,
                **role_tools,
            )

        # Log Claude CLI response stats
        is_resume = claude_sid is not None
        prev_cost = self._claude_session_costs.get(key, 0.0)
        if response.total_cost_usd >= prev_cost:
            # Normal: cumulative total increased
            turn_cost = response.total_cost_usd - prev_cost
        else:
            # Session was compacted/reset by CLI — treat total as this turn's cost
            turn_cost = response.total_cost_usd
        self._claude_session_costs[key] = response.total_cost_usd
        logger.info(
            "Claude CLI response: session={} resume={} turn_cost=${:.4f} total_cost=${:.4f} "
            "duration={}ms turns={} tokens(in={} out={} cache_read={} cache_create={})",
            response.session_id or "n/a",
            is_resume,
            turn_cost,
            response.total_cost_usd,
            response.duration_ms,
            response.num_turns,
            response.input_tokens,
            response.output_tokens,
            response.cache_read_input_tokens,
            response.cache_creation_input_tokens,
        )

        # Store Claude CLI session ID from response for future --resume
        if response.session_id and not response.is_error:
            self._claude_session_ids[key] = response.session_id
            session.claude_session_id = response.session_id

        # Apply secret filtering
        result_text = filter_secrets(response.result) if response.result else ""

        if response.is_error:
            logger.error("Claude CLI error: {}", result_text[:200])
            result_text = result_text or "Sorry, I encountered an error."

        # Save turn with cost and token data
        self._save_turn(
            session, msg.content, result_text,
            turn_cost_usd=turn_cost,
            total_cost_usd=response.total_cost_usd,
            duration_ms=response.duration_ms,
            num_turns=response.num_turns,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_input_tokens=response.cache_read_input_tokens,
            cache_creation_input_tokens=response.cache_creation_input_tokens,
        )
        await self._session_manager.save(session)

        # Idle consolidation timer
        self._reset_idle_timer(key, session)

        # If message_sender already sent in this turn, don't duplicate
        if self.message_sender._sent_in_turn:
            return None

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=result_text,
            metadata=msg.metadata or {},
        )

    async def _handle_new(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle /new command - archive and clear session."""
        self._consolidating.add(session.session_id)
        try:
            if session.messages:
                await self._consolidate_memory(session, archive_all=True)
        except Exception:
            logger.exception("/new archival failed for {}", session.session_id)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Memory archival failed, session not cleared.",
            )
        finally:
            self._consolidating.discard(session.session_id)

        await self._session_manager.clear(session.session_id)
        # Clear Claude CLI session so next call creates a fresh one
        self._claude_session_ids.pop(msg.session_key, None)
        self._claude_session_costs.pop(msg.session_key, None)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="New session started.")

    def _reset_idle_timer(self, session_key: str, session: Session) -> None:
        """Reset idle consolidation timer for a session."""
        if self._idle_timeout <= 0:
            return  # Disabled
        if session_key.startswith("cron:"):
            return  # Skip cron sessions

        # Cancel existing timer
        if session_key in self._idle_timers:
            self._idle_timers[session_key].cancel()

        loop = asyncio.get_running_loop()
        self._idle_timers[session_key] = loop.call_later(
            self._idle_timeout,
            lambda sk=session_key: asyncio.ensure_future(self._idle_consolidate(sk)),
        )

    async def _idle_consolidate(self, session_key: str) -> None:
        """Consolidate an idle session and clean up."""
        self._idle_timers.pop(session_key, None)

        session = await self._session_manager.get_or_create(session_key)

        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated < self._idle_min_turns:
            return  # Too few messages, not worth consolidating

        if session_key in self._consolidating:
            return  # Already consolidating

        logger.info("Idle consolidation for session {} ({} messages)", session_key, unconsolidated)
        self._consolidating.add(session_key)
        try:
            await self._consolidate_memory(session, archive_all=True)
            self._claude_session_ids.pop(session_key, None)
            self._claude_session_costs.pop(session_key, None)
        except Exception:
            logger.exception("Idle consolidation failed for {}", session_key)
        finally:
            self._consolidating.discard(session_key)

    def _maybe_consolidate(self, session: Session) -> None:
        """Trigger background consolidation if needed."""
        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated >= self.memory_window and session.session_id not in self._consolidating:
            self._consolidating.add(session.session_id)

            async def _do():
                try:
                    await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.session_id)
                    t = asyncio.current_task()
                    if t:
                        self._consolidation_tasks.discard(t)

            task = asyncio.create_task(_do())
            self._consolidation_tasks.add(task)

    def _save_turn(
        self, session: Session, user_content: str, assistant_content: str,
        *, turn_cost_usd: float = 0.0, total_cost_usd: float = 0.0,
        duration_ms: int = 0, num_turns: int = 0,
        input_tokens: int = 0, output_tokens: int = 0,
        cache_read_input_tokens: int = 0, cache_creation_input_tokens: int = 0,
    ) -> None:
        """Save user + assistant messages to session with cost and token metadata."""
        now = datetime.now().isoformat()
        session.messages.append({"role": "user", "content": user_content, "timestamp": now})
        if assistant_content:
            session.messages.append({
                "role": "assistant", "content": assistant_content, "timestamp": now,
                "turn_cost_usd": turn_cost_usd,
                "total_cost_usd": total_cost_usd,
                "duration_ms": duration_ms,
                "num_turns": num_turns,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
            })

    async def _consolidate_memory(self, session: Session, archive_all: bool = False) -> bool:
        return await MemoryStore(self.workspace).consolidate(
            session, self.claude, archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self, content: str, session_key: str = "cli:direct",
        channel: str = "cli", chat_id: str = "direct",
        ephemeral: bool = False,
    ) -> str:
        """Process a message directly (for CLI or cron usage).

        Args:
            ephemeral: If True, use a fresh session with no history or resume.
        """
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, session_key_override=session_key,
        )
        async with self._session_locks[msg.session_key]:
            if ephemeral:
                # Force a fresh Claude CLI session with no history
                self._claude_session_ids.pop(msg.session_key, None)
                self._claude_session_costs.pop(msg.session_key, None)
                await self._session_manager.clear(msg.session_key)
            response = await self._process_message(msg)
            return response.content if response else ""

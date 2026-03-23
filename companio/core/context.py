"""Context builder for assembling agent prompts."""

import platform
import time
from datetime import datetime
from pathlib import Path

from companio.core.memory import MemoryStore


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path, bot_name: str = "companio"):
        self.workspace = workspace
        self.bot_name = bot_name
        self.memory = MemoryStore(workspace)

    def build_system_prompt(self) -> str:
        """Build the system prompt from identity, bootstrap files, and memory."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        return "\n\n---\n\n".join(parts)

    def write_claude_md(self, project_dir: Path) -> None:
        """Write CLAUDE.md to the Claude CLI project directory.

        This file is read automatically by Claude CLI at session start,
        replacing the need for --append-system-prompt.
        """
        content = self.build_system_prompt()
        claude_md = project_dir / "CLAUDE.md"
        claude_md.write_text(content, encoding="utf-8")

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        name = self.bot_name

        return f"""# {name}

You are {name}, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md (write important facts here)
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

{platform_policy}

## Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        metadata: dict | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if metadata:
            if metadata.get("is_group"):
                lines.append("Chat Type: group")
            sender_parts = []
            if metadata.get("first_name"):
                sender_parts.append(metadata["first_name"])
            if metadata.get("username"):
                sender_parts.append(f"@{metadata['username']}")
            if sender_parts:
                lines.append(f"Sender: {' '.join(sender_parts)}")
            if metadata.get("role_name"):
                lines.append(f"Role: {metadata['role_name']}")
            if metadata.get("role_prompt"):
                lines.append(f"\n## Role Instructions\n{metadata['role_prompt']}")
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def format_history(messages: list[dict]) -> str:
        """Format session messages as a text string for injection into Claude CLI prompt.

        Args:
            messages: List of dicts with "role" and "content" keys.

        Returns:
            Formatted string with each message on its own line prefixed by role.
            Empty-content messages are skipped. Messages longer than 2000 chars
            are truncated with "...(truncated)".
        """
        lines = []
        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")
            if not content:
                continue
            if isinstance(content, str) and len(content) > 2000:
                content = content[:2000] + "...(truncated)"
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

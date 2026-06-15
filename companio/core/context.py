"""Context builder for assembling agent prompts."""

import os
import platform
import time
from datetime import datetime
from pathlib import Path

from companio.core.memory import MemoryStore


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
    _MEDIA_CONTEXT_OPEN = '<external-context trust="medium" source="channel-upload">'
    _MEDIA_CONTEXT_CLOSE = "</external-context>"

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

    def write_claude_md(
        self,
        project_dir: Path,
        disallowed_tools: list[str] | None = None,
    ) -> None:
        """Write CLAUDE.md to the Claude CLI project directory.

        This file is read automatically by Claude CLI at session start,
        replacing the need for --append-system-prompt.

        Args:
            project_dir: Directory where CLAUDE.md will be written.
            disallowed_tools: Tools blocked by role policy. When non-empty,
                a [TOOL POLICY] section is appended so the model can fail
                fast instead of searching for workarounds.
        """
        import re

        content = self.build_system_prompt()

        if disallowed_tools:
            sanitized = [
                t for t in disallowed_tools
                if t and isinstance(t, str) and re.match(r"^[\w\-]+$", t)
            ]
            if sanitized:
                tool_list = ", ".join(sanitized)
                policy_parts = [
                    "\n\n---\n\n"
                    "## [TOOL POLICY]\n"
                    f"The following tools are blocked by policy in this environment: {tool_list}.\n\n"
                    "If a task requires any of these tools, immediately inform the user:\n"
                    f'"이 작업은 현재 환경에서 사용할 수 없는 도구({tool_list})가 필요합니다. '
                    '관리자에게 문의하거나 상위 권한으로 실행해 주세요."\n\n'
                    "Do NOT attempt ANY workaround including but not limited to:\n"
                    "- ToolSearch, Agent spawn, TaskCreate\n"
                    "- Read/Glob/Grep to discover alternative paths\n"
                    "- Write/Edit to create scripts for indirect execution\n"
                    "- WebSearch or WebFetch for external tools\n"
                    "- MCP tools (computer-use, etc.) for indirect shell access\n\n"
                    "Stop immediately and report the limitation to the user."
                ]

                # Skill → required-tool mapping (hard deps only).
                # SSOT: each skill's SKILL.md `requires.tools` frontmatter.
                # When adding/removing skills, update BOTH this dict AND the
                # corresponding SKILL.md. Future: parse frontmatter dynamically.
                _SKILL_TOOL_DEPS: dict[str, list[str]] = {
                    "claude-sync": ["Bash"],
                    "git-workflow": ["Bash"],
                    "md-to-pdf": ["Bash"],
                    "setup-env": ["Bash"],
                }
                blocked_set = set(sanitized)
                blocked_skills = [
                    (skill, deps)
                    for skill, deps in _SKILL_TOOL_DEPS.items()
                    if any(d in blocked_set for d in deps)
                ]
                if blocked_skills:
                    lines = [
                        "\n\n### Blocked Skills\n"
                        "The following skills require blocked tools and will NOT work "
                        "in this environment. Do not invoke them via the Skill tool:\n"
                    ]
                    for skill, deps in blocked_skills:
                        lines.append(f"- {skill} (requires: {', '.join(deps)})")
                    lines.append(
                        "\nIf the user requests one of these skills, explain that "
                        "the skill requires elevated permissions (blocked tool) "
                        "and cannot run in the current environment."
                    )
                    policy_parts.append("\n".join(lines))

                content += "".join(policy_parts)

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
- 기본은 단일 에이전트로 직접 처리한다. 사용자가 "딥다이브", "교차검증", "다중 에이전트"를 명시한 경우에만 서브에이전트를 사용하되 최대 5개까지 생성한다. 교차검증은 최대 2라운드, 수렴 기준은 평가 항목별 평균 7점 이상(만장일치 불요). 2라운드 미수렴 시 최고 결과물에 개선 제안을 첨부하여 즉시 반환한다. 사용자가 명시적으로 더 높은 기준을 요청하면 최대 3라운드까지 허용하되 예상 단계수를 먼저 안내한다. 위키 조회는 전체 작업에서 최대 10회로 제한한다. 분석 대상이 5개 이상이거나 서브에이전트 3개 이상 필요한 경우 단계별로 분할하여 각 단계 완료 시 중간 결과를 보고한 뒤 진행한다. PDF 분석·요약·일반 보고서는 서브에이전트 없이 직접 처리한다.
- If a tool call fails 3 times consecutively with the same error pattern, stop retrying immediately. Report the failure to the user with the tool name and error summary. Do not attempt alternative approaches for the same goal.

Reply directly with text for all responses. For scheduled tasks (cron), your response text is automatically delivered to the target channel — no additional tool call needed."""

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
            if metadata.get("display_name"):
                sender_parts.append(metadata["display_name"])
            if sender_parts:
                line = f"Sender: {' '.join(sender_parts)}"
                if metadata.get("user_id"):
                    line += f" ({metadata['user_id']})"
                lines.append(line)
            elif metadata.get("user_id"):
                lines.append(f"Sender: {metadata['user_id']}")
            if metadata.get("role_name"):
                lines.append(f"Role: {metadata['role_name']}")
            if metadata.get("role_prompt"):
                lines.append(f"\n## Role Instructions\n{metadata['role_prompt']}")
        # Git repo info from environment
        git_repo = os.environ.get("GIT_REPO_URL")
        if git_repo:
            lines.append(f"\n## Connected Git Repository")
            lines.append(f"Repo: {git_repo}")
            lines.append("SSH 접근 가능 (Deploy Key 등록됨). 작업 시 반드시 feature 브랜치를 생성하고 main에 직접 push하지 마세요.")
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
    def format_media_tags(media: list[str]) -> str:
        """Format media paths as an attachment block for Claude prompt injection.

        Wraps one-per-line ``[image: /path]`` / ``[file: /path]`` tags in an
        ``<external-context trust="medium" source="channel-upload">`` block so
        the LLM can distinguish bot-injected attachments from text the user
        may have typed (which might contain look-alike tags as prompt
        injection). Filters out falsy entries (download failures leaving
        empty paths) and de-duplicates while preserving order.

        Returns an empty string when ``media`` is empty or contains only
        falsy entries — callers can safely concatenate the result.
        """
        paths = [p for p in dict.fromkeys(media) if p]
        if not paths:
            return ""
        lines = []
        for path in paths:
            ext = Path(path).suffix.lower()
            tag = "image" if ext in ContextBuilder._IMAGE_EXTS else "file"
            lines.append(f"[{tag}: {path}]")
        body = "\n".join(lines)
        return (
            f"\n\n{ContextBuilder._MEDIA_CONTEXT_OPEN}\n"
            f"{body}\n"
            f"{ContextBuilder._MEDIA_CONTEXT_CLOSE}"
        )

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

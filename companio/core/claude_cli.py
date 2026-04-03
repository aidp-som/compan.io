"""ClaudeCLI subprocess wrapper for calling Claude Code CLI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

_SECRET_PATTERNS = frozenset(
    {
        "API_KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "CREDENTIAL",
        "ANTHROPIC_",
        "OPENAI_",
        "GOOGLE_",
        "GEMINI_",
        "TELEGRAM_BOT_TOKEN",
        "AWS_",
        "AZURE_",
        "STRIPE_",
        "SLACK_",
        "BRAVE_",
    }
)

_SECRET_EXACT = frozenset(
    {
        "DATABASE_URL",
        "DB_PASSWORD",
        "PGPASSWORD",
        "JWT_SECRET",
        "SESSION_SECRET",
        "SENTRY_DSN",
        "COMPANIO_TOKEN",
        "COMPANIO_SECRET",
    }
)

_CLAUDE_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE")

# Env vars that must pass through despite matching secret/Claude patterns.
# CLAUDE_CODE_OAUTH_TOKEN is required for headless Claude CLI auth (Max plan).
_PASSTHROUGH_KEYS = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})


def _filtered_env() -> dict[str, str]:
    """Return a copy of os.environ with secrets and Claude internals removed."""
    result: dict[str, str] = {}
    for k, v in os.environ.items():
        upper = k.upper()
        # Always allow explicitly whitelisted keys
        if upper in _PASSTHROUGH_KEYS:
            result[k] = v
            continue
        # Remove CLAUDECODE*, CLAUDE_CODE_ENTRYPOINT, CLAUDE* env vars
        if any(upper.startswith(prefix) for prefix in _CLAUDE_PREFIXES):
            continue
        # Remove exact-match secret keys
        if upper in _SECRET_EXACT:
            continue
        # Remove keys matching secret patterns
        if any(pat in upper for pat in _SECRET_PATTERNS):
            continue
        result[k] = v
    return result


def verify_claude_cli() -> str:
    """Check that the claude CLI exists and return its version string.

    Raises RuntimeError if not found or version check fails.
    """
    path = shutil.which("claude")
    if path is None:
        raise RuntimeError("Claude CLI not found on PATH. Install it first.")
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip() or proc.stderr.strip() or "unknown"
    except Exception as exc:
        raise RuntimeError(f"Failed to check Claude CLI version: {exc}") from exc


@dataclass
class ClaudeResponse:
    """Parsed response from Claude CLI JSON output."""

    result: str
    session_id: str | None = None
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    is_error: bool = False
    subtype: str = "success"
    # Token usage breakdown
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_json(cls, raw: str) -> ClaudeResponse:
        """Parse Claude CLI JSON output into a ClaudeResponse."""
        if not raw or not raw.strip():
            return cls(
                result="Empty response from Claude CLI",
                is_error=True,
                subtype="error_empty",
            )
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return cls(
                result=f"Failed to parse JSON from Claude CLI: {exc}",
                is_error=True,
                subtype="error_parse",
            )
        # Extract token usage from nested usage object
        usage = data.get("usage", {})
        return cls(
            result=data.get("result", ""),
            session_id=data.get("session_id"),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            duration_ms=data.get("duration_ms", 0),
            num_turns=data.get("num_turns", 0),
            is_error=data.get("is_error", False),
            subtype=data.get("subtype", "success"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
        )


class ClaudeCLI:
    """Async wrapper around the Claude Code CLI."""

    def __init__(
        self,
        *,
        project_dir: Path,
        workspace_dir: Path | None = None,
        max_turns: int = 50,
        timeout: int = 300,
        max_concurrent: int = 5,
        model: str | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.workspace_dir = workspace_dir
        self.max_turns = max_turns
        self.timeout = timeout
        self.model = model
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _build_cmd(
        self,
        *,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> list[str]:
        """Build the claude CLI command list.

        System prompt is read from CLAUDE.md in the project directory (cwd),
        not passed via CLI args.

        Args:
            session_id: Create a new session with this UUID (first call).
            resume_session_id: Resume an existing session by ID (subsequent calls).
            allowed_tools: Whitelist of tools (role-based access control).
            disallowed_tools: Blacklist of tools (role-based access control).
        """
        cmd = ["claude", "-p", "--output-format", "json"]
        cmd.extend(["--max-turns", str(self.max_turns)])
        if self.workspace_dir:
            cmd.extend(["--add-dir", str(self.workspace_dir)])

        if self.model:
            cmd.extend(["--model", self.model])

        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        elif session_id:
            cmd.extend(["--session-id", session_id])

        # Role-based tool restrictions
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        if disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])

        # Always skip permissions — companio runs as an autonomous agent
        cmd.append("--dangerously-skip-permissions")

        return cmd

    async def _spawn(
        self, cmd: list[str], message: str
    ) -> tuple[int, str, str]:
        """Spawn the claude CLI process and return (returncode, stdout, stderr).

        Messages are passed via stdin for ARG_MAX safety and security.
        Uses start_new_session=True for process group management.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_filtered_env(),
            cwd=str(self.project_dir),
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(message.encode("utf-8")),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Claude CLI process timed out after {}s", self.timeout)
            await self._kill_proc(proc)
            raise
        except asyncio.CancelledError:
            logger.warning("Claude CLI process was cancelled")
            await self._kill_proc(proc)
            raise

        returncode = proc.returncode if proc.returncode is not None else -1
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        return (returncode, stdout, stderr)

    @staticmethod
    async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
        """Kill the process group: SIGTERM, wait 5s, then SIGKILL."""
        if proc.pid is None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("Process {} did not exit after SIGTERM, escalating to SIGKILL", proc.pid)
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

    async def run(
        self,
        message: str,
        *,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> ClaudeResponse:
        """Run a message through the Claude CLI and return parsed response.

        System prompt is read from CLAUDE.md in the project directory.

        Args:
            message: User message (passed via stdin).
            session_id: Create session with this UUID (first call).
            resume_session_id: Resume existing session (subsequent calls).
            allowed_tools: Whitelist of tools (role-based access control).
            disallowed_tools: Blacklist of tools (role-based access control).
        """
        cmd = self._build_cmd(
            session_id=session_id,
            resume_session_id=resume_session_id,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )
        logger.debug("Running Claude CLI: {}", " ".join(cmd))

        async with self._semaphore:
            try:
                returncode, stdout, stderr = await self._spawn(cmd, message)
            except asyncio.TimeoutError:
                return ClaudeResponse(
                    result=f"Claude CLI timeout after {self.timeout}s",
                    is_error=True,
                    subtype="error_timeout",
                )
            except asyncio.CancelledError:
                return ClaudeResponse(
                    result="Claude CLI call was cancelled",
                    is_error=True,
                    subtype="error_cancelled",
                )

        if returncode != 0:
            logger.error("Claude CLI exited with code {}: {}", returncode, stderr)
            return ClaudeResponse(
                result=stderr or f"Claude CLI exited with code {returncode}",
                is_error=True,
                subtype="error_process",
            )

        if not stdout.strip():
            return ClaudeResponse(
                result="Empty stdout from Claude CLI with exit code 0",
                is_error=True,
                subtype="error_empty",
            )

        return ClaudeResponse.from_json(stdout)

"""Utility functions for companio."""

import re
from datetime import datetime
from pathlib import Path


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

# ---------------------------------------------------------------------------
# Secret filtering (Tier 2: output-level value scanning)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = re.compile(
    r"|".join(
        [
            # Private keys (mask from BEGIN marker to END marker, including the body)
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            # JWT tokens (header.payload, ignoring signature to keep pattern tight)
            r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+)?",
            # Anthropic keys (must come before generic sk- to take priority)
            r"sk-ant-[a-zA-Z0-9]{20,}",
            # OpenAI keys
            r"sk-[a-zA-Z0-9]{20,}",
            # GitHub tokens
            r"ghp_[a-zA-Z0-9]{36,}",
            r"gho_[a-zA-Z0-9]{36,}",
            r"ghs_[a-zA-Z0-9]{36,}",
            r"ghu_[a-zA-Z0-9]{36,}",
            # Slack tokens
            r"xoxb-[0-9]+-[0-9]+-[a-zA-Z0-9]+",
            r"xoxp-[0-9]+-[0-9]+-[a-zA-Z0-9]+",
            r"xoxs-[0-9]+-[0-9]+-[a-zA-Z0-9]+",
            r"xapp-[0-9]+-[a-zA-Z0-9]+-[a-zA-Z0-9]+",
            # AWS access key IDs
            r"AKIA[0-9A-Z]{16}",
            # Webhook secrets
            r"whsec_[a-zA-Z0-9]+",
        ]
    )
)


def filter_secrets(text: str, mask: str = "***") -> str:
    """Scan *text* for known secret patterns and replace matches with *mask*.

    This is Tier 2 of the secret-safety system: it operates on output values
    (stdout/stderr) rather than environment variable names.

    Args:
        text: The string to scan.
        mask: Replacement string (default ``"***"``).

    Returns:
        The sanitised string with all detected secrets replaced by *mask*.
    """
    if not text:
        return text
    return _SECRET_PATTERNS.sub(mask, text)


def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files

    try:
        tpl = pkg_files("companio") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md"):
            _write(item, workspace / item.name)
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    _write(tpl / "memory" / "REQUESTS.md", workspace / "memory" / "REQUESTS.md")
    _write(tpl / "memory" / "PROJECT.md", workspace / "memory" / "PROJECT.md")
    (workspace / "skills").mkdir(exist_ok=True)
    skills_tpl = tpl / "skills"
    if skills_tpl.is_dir():
        for skill_dir in skills_tpl.iterdir():
            if skill_dir.is_dir():
                for skill_file in skill_dir.iterdir():
                    if skill_file.name.endswith(".md"):
                        _write(skill_file, workspace / "skills" / skill_dir.name / skill_file.name)

    if added and not silent:
        from rich.console import Console

        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added

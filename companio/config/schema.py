"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    reply_to_message: bool = False  # If true, bot replies quote the original message


class RoleConfig(Base):
    """Tool access control role definition."""

    description: str = ""
    allowed_tools: list[str] | None = None  # None = all tools allowed
    disallowed_tools: list[str] | None = None  # None = no tools blocked
    role_prompt: str | None = None  # Injected into system prompt for behavioral rules


class SlackConfig(Base):
    """Slack channel configuration (Socket Mode)."""

    enabled: bool = False
    block_kit: str = "false"  # "auto" | "true" | "false"
    bot_token: str = ""  # xoxb-... Bot User OAuth Token
    app_token: str = ""  # xapp-... App-Level Token for Socket Mode
    allow_from: list[str] = Field(default_factory=list)  # Slack user IDs (e.g. ["U12345678"])
    respond_in_thread: bool = True  # Reply in thread when mentioned in channels
    ack_reactions_enabled: bool = True  # Send 👀 reaction on mention; remove on done
    progress_pulse_enabled: bool = True  # Edit progress message every 5s up to 4 times
    broadcast_enabled: bool = True  # Master flag for share_to_channel reply_broadcast
    broadcast_blocked_channels: list[str] = Field(
        default_factory=list
    )  # chat_id list — broadcast strictly skipped even when enabled
    # Auto-fetch the parent thread's messages on app_mention so the LLM sees prior
    # context written by other users. Required scopes: channels:history, groups:history,
    # im:history, mpim:history. Falls back to runtime-disabled on missing_scope.
    thread_context_enabled: bool = True
    thread_context_default_limit: int = 20  # messages fetched per mention by default
    thread_context_max_limit: int = 200  # cap when user intent triggers a deeper fetch
    thread_context_blocked_channels: list[str] = Field(
        default_factory=list
    )  # chat_id list — fetch strictly skipped even when enabled (e.g. #hr, #finance)


class ChannelsConfig(Base):
    """Configuration for chat channels."""

    send_progress: bool = True  # stream agent's text progress to the channel
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.companio/workspace"
    memory_window: int = 50
    bot_name: str = "companio"  # Customizable bot identity name
    idle_consolidation_timeout: int = 1800  # 30분, 0이면 비활성화
    idle_consolidation_min_turns: int = 4  # 최소 턴 수


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


class ClaudeCLIConfig(Base):
    """Claude CLI subprocess configuration."""

    max_turns: int = 50
    timeout: int = 300
    max_concurrent: int = 5
    model: str | None = None  # Claude CLI model override


class CrosspostRoute(Base):
    """A single crosspost target channel."""

    channel_id: str
    description: str = ""


class CrosspostConfig(Base):
    """Cross-channel message posting configuration."""

    enabled: bool = False
    routes: dict[str, CrosspostRoute] = Field(default_factory=dict)


class Config(BaseSettings):
    """Root configuration for companio."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    claude: ClaudeCLIConfig = Field(default_factory=ClaudeCLIConfig)
    crosspost: CrosspostConfig = Field(default_factory=CrosspostConfig)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    user_roles: dict[str, str] = Field(default_factory=dict)  # sender_id/username -> role name
    default_role: str | None = None  # role for unlisted users; None = deny access

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    model_config = ConfigDict(env_prefix="COMPANIO_", env_nested_delimiter="__", extra="ignore")

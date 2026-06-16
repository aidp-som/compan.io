"""Configuration loading utilities."""

import json
from pathlib import Path

from loguru import logger

from companio.config.schema import Config

# Slack fields that should be present in the raw config for operational clarity.
# Missing fields still work (Pydantic fills schema defaults), but a warning
# nudges operators toward explicit configuration.
_SLACK_RECOMMENDED: set[str] = {
    "blockKit",
    "respondInThread",
    "ackReactionsEnabled",
    "broadcastEnabled",
    "threadContextEnabled",
}

# Schema defaults to show in the warning message (camelCase key -> default value)
_SLACK_DEFAULTS: dict[str, str] = {
    "blockKit": "auto",
    "respondInThread": "true",
    "ackReactionsEnabled": "true",
    "broadcastEnabled": "true",
    "threadContextEnabled": "true",
}

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".companio" / "config.json"


def load_dotenv_if_exists(config_dir: Path) -> None:
    """Load .env file from config directory if it exists."""
    env_file = config_dir / ".env"
    if env_file.exists():
        from dotenv import load_dotenv

        load_dotenv(env_file)


def warn_missing_recommended(data: dict) -> list[str]:
    """Check raw config dict for missing recommended Slack fields.

    Inspects the JSON data *before* Pydantic fills defaults so operators
    are aware when they rely on implicit values.

    Returns:
        List of human-readable warning strings (empty when all present).
    """
    slack = data.get("channels", {}).get("slack", {})
    if not slack:
        return []

    warnings: list[str] = []
    for field in sorted(_SLACK_RECOMMENDED):
        if field not in slack:
            default = _SLACK_DEFAULTS.get(field, "N/A")
            warnings.append(
                f"Slack config missing recommended field '{field}' "
                f'(will use schema default: "{default}")'
            )
    return warnings


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    # Load .env file from the config directory before loading config
    load_dotenv_if_exists(path.parent)

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)

            # Warn about recommended fields missing from the raw config
            for msg in warn_missing_recommended(data):
                logger.warning(msg)

            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Remove stale keys from pre-Claude-CLI architecture
    for stale in ("tools", "providers"):
        data.pop(stale, None)
    # Remove stale keys from subsections
    if "channels" in data:
        data["channels"].pop("sendToolHints", None)
    if "claude" in data:
        data["claude"].pop("allowedTools", None)
    if "gateway" in data:
        data["gateway"].pop("heartbeat", None)
    # Backfill block_kit for configs predating Block Kit feature
    slack = data.get("channels", {}).get("slack", {})
    if slack and "blockKit" not in slack and "block_kit" not in slack:
        slack["blockKit"] = "auto"
    return data

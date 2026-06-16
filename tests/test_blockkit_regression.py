"""Regression tests for Slack Block Kit table feature.

Ensures the block_kit config field defaults to "auto", is preserved when
explicitly set, is backfilled by _migrate_config(), and that _should_use_blocks
correctly detects pipe tables vs. respects explicit "false" mode.
"""

from companio.channels.slack import _should_use_blocks
from companio.config.loader import _migrate_config, warn_missing_recommended
from companio.config.schema import Config


class TestBlockKitConfigDefaults:
    """Config without blockKit field should default to 'auto'."""

    def test_missing_blockkit_defaults_to_auto(self):
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "botToken": "xoxb-test",
                    "appToken": "xapp-test",
                }
            }
        }
        config = Config.model_validate(raw)
        assert config.channels.slack.block_kit == "auto"

    def test_explicit_blockkit_false_preserved(self):
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "botToken": "xoxb-test",
                    "appToken": "xapp-test",
                    "blockKit": "false",
                }
            }
        }
        migrated = _migrate_config(raw)
        config = Config.model_validate(migrated)
        assert config.channels.slack.block_kit == "false"


class TestMigrateBackfillsBlockKit:
    """_migrate_config must add blockKit='auto' when absent."""

    def test_migrate_backfills_blockkit(self):
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "botToken": "xoxb-test",
                    "appToken": "xapp-test",
                }
            }
        }
        migrated = _migrate_config(raw)
        assert migrated["channels"]["slack"]["blockKit"] == "auto"

    def test_migrate_does_not_overwrite_existing_blockkit(self):
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "blockKit": "false",
                }
            }
        }
        migrated = _migrate_config(raw)
        assert migrated["channels"]["slack"]["blockKit"] == "false"

    def test_migrate_handles_missing_slack_section(self):
        raw = {"channels": {"telegram": {"enabled": False}}}
        migrated = _migrate_config(raw)
        # No crash; slack section not present so nothing to backfill
        assert "slack" not in migrated.get("channels", {})

    def test_migrate_handles_empty_config(self):
        raw = {}
        migrated = _migrate_config(raw)
        assert migrated == {}


class TestShouldUseBlocksDetection:
    """_should_use_blocks must detect pipe tables in auto mode."""

    PIPE_TABLE = (
        "| Name | Status |\n"
        "|------|--------|\n"
        "| Alice | done |\n"
    )

    def test_should_use_blocks_auto_detects_tables(self):
        assert _should_use_blocks(self.PIPE_TABLE, "auto") is True

    def test_should_use_blocks_false_blocks_tables(self):
        assert _should_use_blocks(self.PIPE_TABLE, "false") is False

    def test_should_use_blocks_true_always(self):
        assert _should_use_blocks("hello", "true") is True

    def test_should_use_blocks_auto_short_text(self):
        # Short text without headings or tables should not trigger blocks
        assert _should_use_blocks("hi", "auto") is False


class TestWarnMissingRecommended:
    """warn_missing_recommended detects absent recommended Slack fields."""

    def test_warn_missing_recommended_detects_absent_fields(self):
        """Config with Slack section but missing recommended fields produces warnings."""
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "botToken": "xoxb-test",
                    "appToken": "xapp-test",
                    # blockKit added by _migrate_config, but we test raw data here
                }
            }
        }
        warnings = warn_missing_recommended(raw)
        # All 5 recommended fields are absent
        assert len(warnings) == 5
        # Each warning mentions the field name and "schema default"
        for w in warnings:
            assert "Slack config missing recommended field" in w
            assert "schema default" in w

    def test_warn_missing_recommended_silent_when_present(self):
        """Config with all recommended fields present produces no warnings."""
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "botToken": "xoxb-test",
                    "appToken": "xapp-test",
                    "blockKit": "auto",
                    "respondInThread": True,
                    "ackReactionsEnabled": True,
                    "broadcastEnabled": True,
                    "threadContextEnabled": True,
                }
            }
        }
        warnings = warn_missing_recommended(raw)
        assert warnings == []

    def test_warn_missing_recommended_no_slack_section(self):
        """Config without a Slack section returns no warnings."""
        raw = {"channels": {"telegram": {"enabled": False}}}
        assert warn_missing_recommended(raw) == []

    def test_warn_missing_recommended_partial(self):
        """Only absent fields produce warnings; present ones are silent."""
        raw = {
            "channels": {
                "slack": {
                    "enabled": True,
                    "blockKit": "auto",
                    "respondInThread": True,
                    # ackReactionsEnabled, broadcastEnabled, threadContextEnabled absent
                }
            }
        }
        warnings = warn_missing_recommended(raw)
        assert len(warnings) == 3
        field_names = {w.split("'")[1] for w in warnings}
        assert field_names == {"ackReactionsEnabled", "broadcastEnabled", "threadContextEnabled"}

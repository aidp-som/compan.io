"""Tests for config validate subcommand."""

import json
import subprocess
import sys

import pytest

PYTHON = "/Users/holmes-mini/Desktop/my-projects/internal/compan.io/.venv/bin/python"


class TestConfigValidate:
    def test_valid_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "channels": {"telegram": {"enabled": False}},
            "claude": {"maxTurns": 50},
        }))
        result = subprocess.run(
            [PYTHON, "-m", "companio", "config-validate", "--config", str(config_file)],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["valid"] is True

    def test_invalid_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("{not valid json")
        result = subprocess.run(
            [PYTHON, "-m", "companio", "config-validate", "--config", str(config_file)],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["valid"] is False
        assert len(output["errors"]) > 0

    def test_missing_file(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        result = subprocess.run(
            [PYTHON, "-m", "companio", "config-validate", "--config", str(config_file)],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["valid"] is True  # missing file = use defaults, which are valid

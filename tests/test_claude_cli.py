"""Tests for ClaudeCLI subprocess wrapper."""

import asyncio
import json
import shutil
import subprocess

import pytest

from companio.core.claude_cli import ClaudeCLI, ClaudeResponse, _filtered_env, verify_claude_cli

# ── ClaudeResponse.from_json ──────────────────────────────────────────


class TestClaudeResponseFromJson:
    def test_success(self):
        raw = json.dumps(
            {
                "result": "Hello, world!",
                "session_id": "sess-123",
                "total_cost_usd": 0.05,
                "duration_ms": 1234,
                "num_turns": 3,
                "is_error": False,
                "subtype": "success",
            }
        )
        resp = ClaudeResponse.from_json(raw)
        assert resp.result == "Hello, world!"
        assert resp.session_id == "sess-123"
        assert resp.total_cost_usd == 0.05
        assert resp.duration_ms == 1234
        assert resp.num_turns == 3
        assert resp.is_error is False
        assert resp.subtype == "success"

    def test_error_response(self):
        raw = json.dumps(
            {
                "result": "Max turns exceeded",
                "session_id": "sess-456",
                "total_cost_usd": 0.10,
                "duration_ms": 5000,
                "num_turns": 50,
                "is_error": True,
                "subtype": "error_max_turns",
            }
        )
        resp = ClaudeResponse.from_json(raw)
        assert resp.is_error is True
        assert resp.subtype == "error_max_turns"
        assert resp.result == "Max turns exceeded"

    def test_invalid_json(self):
        resp = ClaudeResponse.from_json("not valid json {{{")
        assert resp.is_error is True
        assert "parse" in resp.result.lower() or "json" in resp.result.lower()

    def test_missing_fields_uses_defaults(self):
        raw = json.dumps({"result": "ok"})
        resp = ClaudeResponse.from_json(raw)
        assert resp.result == "ok"
        assert resp.session_id is None
        assert resp.total_cost_usd == 0.0
        assert resp.duration_ms == 0
        assert resp.num_turns == 0
        assert resp.is_error is False

    def test_empty_string(self):
        resp = ClaudeResponse.from_json("")
        assert resp.is_error is True


# ── _filtered_env ─────────────────────────────────────────────────────


class TestFilteredEnv:
    def test_removes_claude_vars(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "test")
        monkeypatch.setenv("CLAUDECODE_FOO", "bar")
        monkeypatch.setenv("CLAUDE_WHATEVER", "baz")
        monkeypatch.setenv("HOME", "/home/user")
        env = _filtered_env()
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert "CLAUDECODE_FOO" not in env
        assert "CLAUDE_WHATEVER" not in env
        assert env.get("HOME") == "/home/user"

    def test_removes_secret_patterns(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "secret123")
        monkeypatch.setenv("DB_PASSWORD", "pass")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("TELEGRAM_TOKEN", "tok")
        monkeypatch.setenv("DATABASE_URL", "postgres://...")
        monkeypatch.setenv("PGPASSWORD", "pg")
        monkeypatch.setenv("JWT_SECRET", "jwt")
        monkeypatch.setenv("SENTRY_DSN", "https://...")
        monkeypatch.setenv("SAFE_VAR", "keep")
        env = _filtered_env()
        for key in [
            "MY_API_KEY", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_TOKEN",
            "DATABASE_URL", "PGPASSWORD", "JWT_SECRET", "SENTRY_DSN",
        ]:
            assert key not in env, f"{key} should be filtered"
        assert env.get("SAFE_VAR") == "keep"

    def test_companio_exact_names_filtered(self, monkeypatch):
        monkeypatch.setenv("COMPANIO_TOKEN", "secret")
        monkeypatch.setenv("COMPANIO_SECRET", "secret")
        monkeypatch.setenv("COMPANIO_LOG_LEVEL", "DEBUG")
        env = _filtered_env()
        assert "COMPANIO_TOKEN" not in env
        assert "COMPANIO_SECRET" not in env
        assert env.get("COMPANIO_LOG_LEVEL") == "DEBUG"


# ── ClaudeCLI._build_cmd ─────────────────────────────────────────────


class TestBuildCmd:
    def _make_cli(self, tmp_path=None, **kwargs):
        from pathlib import Path
        project_dir = tmp_path or Path("/tmp/test-project")
        return ClaudeCLI(project_dir=project_dir, **kwargs)

    def test_basic(self, tmp_path):
        cli = self._make_cli(tmp_path)
        cmd = cli._build_cmd()
        assert cmd[0].endswith("claude")
        assert cmd[1:5] == ["-p", "--output-format", "stream-json", "--verbose"]
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "50"

    def test_with_model(self, tmp_path):
        cli = self._make_cli(tmp_path, model="opus-4")
        cmd = cli._build_cmd()
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus-4"

    def test_always_skips_permissions(self, tmp_path):
        cli = self._make_cli(tmp_path)
        cmd = cli._build_cmd()
        assert "--dangerously-skip-permissions" in cmd

    def test_session_id(self, tmp_path):
        cli = self._make_cli(tmp_path)
        cmd = cli._build_cmd(session_id="abc-123")
        assert "--session-id" in cmd
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "abc-123"
        assert "--resume" not in cmd

    def test_resume_session(self, tmp_path):
        cli = self._make_cli(tmp_path)
        cmd = cli._build_cmd(resume_session_id="sess-456")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-456"
        assert "--session-id" not in cmd

    def test_resume_takes_precedence_over_session_id(self, tmp_path):
        cli = self._make_cli(tmp_path)
        cmd = cli._build_cmd(session_id="new", resume_session_id="old")
        assert "--resume" in cmd
        assert "--session-id" not in cmd

    def test_add_dir_home(self, tmp_path):
        cli = self._make_cli(tmp_path)
        cmd = cli._build_cmd()
        assert "--add-dir" in cmd
        idx = cmd.index("--add-dir")
        from pathlib import Path
        assert cmd[idx + 1] == str(Path.home())


# ── ClaudeCLI.run ─────────────────────────────────────────────────────


class TestClaudeCLIRun:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch, tmp_path):
        response_json = json.dumps(
            {
                "result": "Done!",
                "session_id": "sess-1",
                "total_cost_usd": 0.01,
                "duration_ms": 500,
                "num_turns": 1,
                "is_error": False,
                "subtype": "success",
            }
        )

        async def fake_spawn(cmd, message, **kwargs):
            return (0, response_json, "")

        cli = ClaudeCLI(project_dir=tmp_path)
        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        resp = await cli.run("Hello")
        assert resp.result == "Done!"
        assert resp.is_error is False

    @pytest.mark.asyncio
    async def test_process_error_nonzero_exit(self, monkeypatch, tmp_path):
        async def fake_spawn(cmd, message, **kwargs):
            return (1, "", "something went wrong")

        cli = ClaudeCLI(project_dir=tmp_path)
        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        resp = await cli.run("Hello")
        assert resp.is_error is True
        assert "something went wrong" in resp.result

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch, tmp_path):
        async def fake_spawn(cmd, message, **kwargs):
            raise asyncio.TimeoutError()

        cli = ClaudeCLI(project_dir=tmp_path)
        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        resp = await cli.run("Hello")
        assert resp.is_error is True
        assert "timeout" in resp.result.lower()

    @pytest.mark.asyncio
    async def test_cancelled_error(self, monkeypatch, tmp_path):
        async def fake_spawn(cmd, message, **kwargs):
            raise asyncio.CancelledError()

        cli = ClaudeCLI(project_dir=tmp_path)
        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        resp = await cli.run("Hello")
        assert resp.is_error is True
        assert "cancel" in resp.result.lower()

    @pytest.mark.asyncio
    async def test_empty_stdout_exit_zero(self, monkeypatch, tmp_path):
        async def fake_spawn(cmd, message, **kwargs):
            return (0, "", "")

        cli = ClaudeCLI(project_dir=tmp_path)
        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        resp = await cli.run("Hello")
        assert resp.is_error is True
        assert "empty" in resp.result.lower()

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, monkeypatch, tmp_path):
        call_count = 0
        max_concurrent = 0

        async def fake_spawn(cmd, message, **kwargs):
            nonlocal call_count, max_concurrent
            call_count += 1
            max_concurrent = max(max_concurrent, call_count)
            await asyncio.sleep(0.05)
            call_count -= 1
            return (
                0,
                json.dumps({"result": "ok", "is_error": False}),
                "",
            )

        cli = ClaudeCLI(project_dir=tmp_path, max_concurrent=2)
        monkeypatch.setattr(cli, "_spawn", fake_spawn)
        tasks = [cli.run("msg") for _ in range(5)]
        await asyncio.gather(*tasks)
        assert max_concurrent <= 2


# ── verify_claude_cli ────────────────────────────────────────────────


class TestVerifyClaudeCli:
    def test_cli_found(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args, returncode=0, stdout="claude 1.0.0\n", stderr=""
            ),
        )
        version = verify_claude_cli()
        assert "1.0.0" in version

    def test_cli_not_found(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="not found"):
            verify_claude_cli()

    def test_version_check_failure(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
        )
        with pytest.raises(RuntimeError, match="Failed to check"):
            verify_claude_cli()

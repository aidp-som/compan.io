# compan.io Tauri Desktop App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap the existing `companio gateway` CLI into a Tauri-based cross-platform desktop app (Windows + macOS) with a minimal system-tray UI for process management, config editing, and log viewing — without changing any existing CLI behavior.

**Architecture:** Tauri (Rust backend + WebView frontend) spawns `companio gateway --tauri` as a child process. Python emits JSON Lines on stdout for IPC events and structured loguru JSON on stderr for logs. The frontend is static HTML/CSS/JS with no build step. A `--tauri` flag gates all new behavior so existing CLI usage is unaffected.

**Tech Stack:** Tauri v2 (Rust), vanilla HTML/CSS/JS (no framework), Python 3.11+ (existing), loguru JSON serialization, JSON Lines protocol.

---

## File Structure

### Python changes (existing codebase)

| File | Action | Responsibility |
|------|--------|---------------|
| `companio/ipc.py` | Create | `emit_event()` — write JSON Lines to stdout with thread-safe lock |
| `companio/cli.py` | Modify | `--tauri` flag, signal handlers, heartbeat task, `config validate` subcommand |
| `companio/channels/base.py` | Modify | Add `get_health()` method returning structured channel status |
| `companio/channels/slack.py` | Modify | Override `get_health()` with reconnection state; add `emit_event` before `sys.exit(1)` calls |
| `companio/channels/telegram.py` | Modify | Override `get_health()` |
| `companio/channels/manager.py` | Modify | Add `get_health_all()`, change exit code for config errors (1 → 2) |
| `companio/core/loop.py` | Modify | Add `active_tasks_count` property |
| `tests/test_ipc.py` | Create | Tests for IPC event emission |
| `tests/test_gateway_health.py` | Create | Tests for channel health reporting |
| `tests/test_config_validate.py` | Create | Tests for config validate subcommand |

### Tauri app (new)

| File | Responsibility |
|------|---------------|
| `desktop/src-tauri/Cargo.toml` | Rust dependencies (tauri, serde, serde_json, tokio) |
| `desktop/src-tauri/tauri.conf.json` | Window size, tray icon, single-instance, bundle ID |
| `desktop/src-tauri/src/main.rs` | App entry, tray setup, window management |
| `desktop/src-tauri/src/gateway.rs` | Process spawn/stop/restart, stdout/stderr capture |
| `desktop/src-tauri/src/logs.rs` | Log ring buffer (500 lines), event emission to frontend |
| `desktop/src-tauri/src/platform.rs` | OS-specific process kill, Python/Claude CLI detection |
| `desktop/src-tauri/icons/` | App icons (.png, .ico, .icns) |
| `desktop/ui/index.html` | Single-page app shell with 4 tabs |
| `desktop/ui/style.css` | System fonts, dark/light auto-switch, all styling |
| `desktop/ui/app.js` | Tauri invoke/listen bindings, tab routing, DOM updates |

---

## Task 1: IPC Module (`companio/ipc.py`)

**Files:**
- Create: `companio/ipc.py`
- Create: `tests/test_ipc.py`

- [ ] **Step 1: Write the failing test for `emit_event`**

```python
# tests/test_ipc.py
"""Tests for IPC event emission."""

import io
import json

from companio.ipc import emit_event


class TestEmitEvent:
    def test_emits_json_line_to_stream(self):
        buf = io.StringIO()
        emit_event("ready", {"version": "0.1.2", "pid": 123}, stream=buf)
        line = buf.getvalue()
        assert line.endswith("\n")
        parsed = json.loads(line)
        assert parsed["type"] == "ready"
        assert parsed["data"]["version"] == "0.1.2"
        assert parsed["data"]["pid"] == 123
        assert "ts" in parsed

    def test_emits_multiple_events(self):
        buf = io.StringIO()
        emit_event("health", {"ok": True}, stream=buf)
        emit_event("error", {"code": "FAIL"}, stream=buf)
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "health"
        assert json.loads(lines[1])["type"] == "error"

    def test_timestamp_is_iso_format(self):
        buf = io.StringIO()
        emit_event("test", {}, stream=buf)
        parsed = json.loads(buf.getvalue())
        # ISO format: YYYY-MM-DDTHH:MM:SS
        assert "T" in parsed["ts"]

    def test_handles_non_serializable_gracefully(self):
        buf = io.StringIO()
        emit_event("test", {"path": "/tmp/foo"}, stream=buf)
        parsed = json.loads(buf.getvalue())
        assert parsed["data"]["path"] == "/tmp/foo"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ipc.py -v`
Expected: `ModuleNotFoundError: No module named 'companio.ipc'`

- [ ] **Step 3: Implement `companio/ipc.py`**

```python
"""IPC event emission for Tauri desktop wrapper."""

import json
import sys
import threading
from datetime import datetime, timezone
from typing import IO, Any

_lock = threading.Lock()


def emit_event(event_type: str, data: dict[str, Any], stream: IO[str] | None = None) -> None:
    """Write a JSON Lines event to the given stream (default: stdout)."""
    target = stream or sys.stdout
    event = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
    with _lock:
        target.write(line)
        target.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ipc.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add companio/ipc.py tests/test_ipc.py
git commit -m "feat(ipc): add JSON Lines event emission module for Tauri IPC"
```

---

## Task 2: Channel Health Reporting

**Files:**
- Modify: `companio/channels/base.py:126-130` (add `get_health()` after `is_running`)
- Modify: `companio/channels/slack.py:261,344` (add `get_health()` override)
- Modify: `companio/channels/telegram.py:153` (add `get_health()` override)
- Modify: `companio/channels/manager.py:148-153` (add `get_health_all()`)
- Create: `tests/test_gateway_health.py`

- [ ] **Step 1: Write the failing test for `BaseChannel.get_health()`**

```python
# tests/test_gateway_health.py
"""Tests for channel health reporting."""

from unittest.mock import MagicMock

from companio.channels.base import BaseChannel
from companio.bus import MessageBus


class StubChannel(BaseChannel):
    name = "stub"

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    async def send(self, msg):
        pass


class TestBaseChannelHealth:
    def test_health_when_stopped(self):
        bus = MagicMock(spec=MessageBus)
        ch = StubChannel(config=MagicMock(), bus=bus)
        health = ch.get_health()
        assert health == {"running": False, "status": "stopped"}

    def test_health_when_running(self):
        bus = MagicMock(spec=MessageBus)
        ch = StubChannel(config=MagicMock(), bus=bus)
        ch._running = True
        health = ch.get_health()
        assert health == {"running": True, "status": "connected"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gateway_health.py::TestBaseChannelHealth -v`
Expected: `AttributeError: 'StubChannel' object has no attribute 'get_health'`

- [ ] **Step 3: Implement `get_health()` on `BaseChannel`**

Add after line 129 of `companio/channels/base.py`:

```python
    def get_health(self) -> dict[str, str | bool]:
        """Return structured health status for IPC reporting."""
        return {
            "running": self._running,
            "status": "connected" if self._running else "stopped",
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gateway_health.py::TestBaseChannelHealth -v`
Expected: 2 tests PASS

- [ ] **Step 5: Write the failing test for `SlackChannel.get_health()`**

Add to `tests/test_gateway_health.py`:

```python
class TestSlackChannelHealth:
    def test_health_connected(self):
        from companio.channels.slack import SlackChannel

        config = MagicMock()
        config.bot_token = "xoxb-test"
        config.app_token = "xapp-test"
        config.allow_from = ["U123"]
        bus = MagicMock(spec=MessageBus)
        ch = SlackChannel(config, bus, workspace="/tmp")
        ch._running = True
        ch._consecutive_failures = 0
        health = ch.get_health()
        assert health["status"] == "connected"
        assert health["running"] is True

    def test_health_reconnecting(self):
        from companio.channels.slack import SlackChannel

        config = MagicMock()
        config.bot_token = "xoxb-test"
        config.app_token = "xapp-test"
        config.allow_from = ["U123"]
        bus = MagicMock(spec=MessageBus)
        ch = SlackChannel(config, bus, workspace="/tmp")
        ch._running = True
        ch._consecutive_failures = 3
        health = ch.get_health()
        assert health["status"] == "reconnecting"
        assert health["consecutive_failures"] == 3
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest tests/test_gateway_health.py::TestSlackChannelHealth -v`
Expected: FAIL — `get_health()` returns base class result without reconnection state

- [ ] **Step 7: Implement `SlackChannel.get_health()` override**

Add to `companio/channels/slack.py` (inside the `SlackChannel` class, after the `stop()` method around line 354):

```python
    def get_health(self) -> dict[str, str | bool | int]:
        """Return health status including reconnection state."""
        health = super().get_health()
        if self._running and self._consecutive_failures > 0:
            health["status"] = "reconnecting"
            health["consecutive_failures"] = self._consecutive_failures
        return health
```

- [ ] **Step 8: Implement `TelegramChannel.get_health()` override**

Add to `companio/channels/telegram.py` (inside the `TelegramChannel` class, after the `stop()` method):

```python
    def get_health(self) -> dict[str, str | bool]:
        """Return health status for Telegram channel."""
        return super().get_health()
```

This is a pass-through for now, but having the explicit override lets us add Telegram-specific health data later without changing the interface.

- [ ] **Step 9: Write the failing test for `ChannelManager.get_health_all()`**

Add to `tests/test_gateway_health.py`:

```python
from companio.channels.manager import ChannelManager
from companio.config.schema import Config


class TestChannelManagerHealthAll:
    def test_get_health_all_with_channels(self):
        config = Config()
        bus = MagicMock(spec=MessageBus)
        manager = ChannelManager.__new__(ChannelManager)
        manager.config = config
        manager.bus = bus
        manager.channels = {}
        stub = StubChannel(config=MagicMock(), bus=bus)
        stub._running = True
        manager.channels["stub"] = stub
        health = manager.get_health_all()
        assert "stub" in health
        assert health["stub"]["status"] == "connected"

    def test_get_health_all_empty(self):
        manager = ChannelManager.__new__(ChannelManager)
        manager.channels = {}
        health = manager.get_health_all()
        assert health == {}
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `python -m pytest tests/test_gateway_health.py::TestChannelManagerHealthAll -v`
Expected: `AttributeError: 'ChannelManager' object has no attribute 'get_health_all'`

- [ ] **Step 11: Implement `get_health_all()` on `ChannelManager`**

Add after line 153 of `companio/channels/manager.py`:

```python
    def get_health_all(self) -> dict[str, dict]:
        """Get health status of all channels (for IPC reporting)."""
        return {name: channel.get_health() for name, channel in self.channels.items()}
```

- [ ] **Step 12: Run all health tests**

Run: `python -m pytest tests/test_gateway_health.py -v`
Expected: All tests PASS

- [ ] **Step 13: Run existing channel tests for regression**

Run: `python -m pytest tests/test_channels.py -v`
Expected: All existing tests PASS

- [ ] **Step 14: Commit**

```bash
git add companio/channels/base.py companio/channels/slack.py companio/channels/telegram.py companio/channels/manager.py tests/test_gateway_health.py
git commit -m "feat(channels): add get_health() for structured channel status reporting"
```

---

## Task 3: AgentLoop `active_tasks_count` Property

**Files:**
- Modify: `companio/core/loop.py:159-160`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_core.py`:

```python
class TestActiveTasksCount:
    def test_active_tasks_count_empty(self, agent):
        assert agent.active_tasks_count == 0

    def test_active_tasks_count_with_tasks(self, agent):
        import asyncio
        agent._active_tasks["session1"] = [asyncio.Future(), asyncio.Future()]
        agent._active_tasks["session2"] = [asyncio.Future()]
        assert agent.active_tasks_count == 3
```

Note: this test uses the existing `agent` fixture in `test_agent_core.py`. Check if the fixture exists; if not, create a minimal one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_core.py::TestActiveTasksCount -v`
Expected: `AttributeError: 'AgentLoop' object has no attribute 'active_tasks_count'`

- [ ] **Step 3: Implement the property**

Add after line 160 of `companio/core/loop.py`:

```python
    @property
    def active_tasks_count(self) -> int:
        """Total number of active tasks across all sessions."""
        return sum(len(tasks) for tasks in self._active_tasks.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_core.py::TestActiveTasksCount -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add companio/core/loop.py tests/test_agent_core.py
git commit -m "feat(loop): add active_tasks_count property for IPC health reporting"
```

---

## Task 4: `--tauri` Flag and Signal Handlers in Gateway

**Files:**
- Modify: `companio/cli.py:329-486`

This is the core integration point. The `--tauri` flag enables:
1. JSON Lines IPC events on stdout
2. Structured loguru output on stderr (via `serialize=True`)
3. Signal handlers for graceful shutdown
4. A periodic heartbeat task

- [ ] **Step 1: Add `--tauri` flag to gateway function signature**

Modify `companio/cli.py` line 329-334 to add the `tauri` parameter:

```python
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    tauri: bool = typer.Option(False, "--tauri", help="Enable structured IPC output for Tauri desktop wrapper", hidden=True),
):
```

- [ ] **Step 2: Redirect Rich console to stderr when `--tauri` is active**

Add right after line 334 (inside the `gateway` function body, before line 336):

```python
    if tauri:
        # Reserve stdout for JSON Lines IPC; redirect Rich output to stderr
        from rich.console import Console as _Console
        nonlocal console  # not needed — console is module-level, just reassign locally
        console = _Console(stderr=True)
```

Wait — `console` is a module-level variable at line 42. We cannot easily reassign it with `nonlocal`. Instead, create a local `out` console:

```python
    if tauri:
        out = Console(stderr=True)
        # Redirect loguru to stderr with JSON serialization
        logger.remove()
        logger.add(sys.stderr, serialize=True, level="INFO")
    else:
        out = console
```

Then replace all `console.print(...)` calls inside the `gateway` function (lines 353-470) with `out.print(...)`. There are 7 occurrences inside `gateway()`:
- Line 353: `console.print("[bold yellow]Warning...")` → `out.print(...)`
- Line 365: `console.print(f"{__logo__} Starting...")` → `out.print(...)`
- Line 373: `console.print("[green]✓...")` → `out.print(...)`
- Line 464: `console.print(f"[green]✓...")` → `out.print(...)`
- Line 466: `console.print("[yellow]Warning...")` → `out.print(...)`
- Line 470: `console.print(f"[green]✓...")` → `out.print(...)`
- Line 480: `console.print("\nShutting down...")` → `out.print(...)`

- [ ] **Step 3: Add signal handlers and IPC events to the `run()` coroutine**

Replace the `run()` coroutine (lines 472-486) with:

```python
    async def run():
        shutdown_event = asyncio.Event()

        if tauri:
            import os
            from companio.ipc import emit_event

            def _on_signal(signum, _frame):
                emit_event("shutdown", {"reason": signal.Signals(signum).name, "exit_code": 0})
                shutdown_event.set()

            signal.signal(signal.SIGINT, _on_signal)
            signal.signal(signal.SIGTERM, _on_signal)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, _on_signal)

        try:
            await cron.start()

            if tauri:
                import os
                from companio import __version__
                from companio.ipc import emit_event

                emit_event("ready", {
                    "version": __version__,
                    "pid": os.getpid(),
                    "workspace": str(config.workspace_path),
                })

            if tauri:
                async def _heartbeat():
                    from companio.ipc import emit_event
                    while not shutdown_event.is_set():
                        emit_event("health", {
                            "channels": channels.get_health_all(),
                            "cron": cron.status(),
                            "agent": {
                                "running": agent._running,
                                "active_tasks": agent.active_tasks_count,
                            },
                        })
                        try:
                            await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                            break
                        except asyncio.TimeoutError:
                            pass

                heartbeat_task = asyncio.create_task(_heartbeat())
                tasks = [
                    asyncio.create_task(agent.run()),
                    asyncio.create_task(channels.start_all()),
                    asyncio.create_task(shutdown_event.wait()),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            else:
                await asyncio.gather(
                    agent.run(),
                    channels.start_all(),
                )
        except KeyboardInterrupt:
            out.print("\nShutting down...")
        finally:
            cron.stop()
            agent.stop()
            await channels.stop_all()
            if tauri:
                from companio.ipc import emit_event
                emit_event("shutdown", {"reason": "clean", "exit_code": 0})
```

- [ ] **Step 4: Run existing tests for regression**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All existing tests PASS (the `--tauri` flag is off by default, so no behavior changes)

- [ ] **Step 5: Commit**

```bash
git add companio/cli.py
git commit -m "feat(gateway): add --tauri flag with IPC events, signal handlers, and heartbeat"
```

---

## Task 5: Error Event Emission Before `sys.exit(1)` in Slack

**Files:**
- Modify: `companio/channels/slack.py:261,344`
- Modify: `companio/channels/manager.py:66-72`

- [ ] **Step 1: Add `emit_event` before `sys.exit(1)` at line 261 of `slack.py`**

Before the `sys.exit(1)` at line 261, add:

```python
                    try:
                        from companio.ipc import emit_event
                        emit_event("error", {
                            "code": "SLACK_INITIAL_CONNECT_FAILED",
                            "message": f"Slack initial connection failed after {MAX_RECONNECT_FAILURES} attempts",
                            "fatal": True,
                            "channel": "slack",
                        })
                    except Exception:
                        pass
                    sys.exit(1)
```

- [ ] **Step 2: Add `emit_event` before `sys.exit(1)` at line 344 of `slack.py`**

Before the `sys.exit(1)` at line 344, add:

```python
                    try:
                        from companio.ipc import emit_event
                        emit_event("error", {
                            "code": "SLACK_RECONNECT_EXHAUSTED",
                            "message": f"Slack reconnection failed {MAX_RECONNECT_FAILURES} times",
                            "fatal": True,
                            "channel": "slack",
                        })
                    except Exception:
                        pass
                    sys.exit(1)
```

- [ ] **Step 3: Change exit code for config errors in `manager.py`**

Modify `companio/channels/manager.py` lines 66-72:

```python
    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                try:
                    from companio.ipc import emit_event
                    emit_event("error", {
                        "code": "CONFIG_ALLOW_FROM_EMPTY",
                        "message": f'"{name}" has empty allowFrom (denies all)',
                        "fatal": True,
                        "channel": name,
                    })
                except Exception:
                    pass
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )
```

Note: `SystemExit` does not accept a return code when given a message string — it always results in exit code 1. To get exit code 2, we need to change this to:

```python
                import sys
                print(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.',
                    file=sys.stderr,
                )
                sys.exit(2)
```

- [ ] **Step 4: Run existing tests for regression**

Run: `python -m pytest tests/test_channels.py tests/test_slack_broadcast.py tests/test_slack_reactions.py -v`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add companio/channels/slack.py companio/channels/manager.py
git commit -m "feat(ipc): emit error events before fatal exits, distinguish config vs runtime exit codes"
```

---

## Task 6: `config validate` Subcommand

**Files:**
- Modify: `companio/cli.py` (add new subcommand)
- Create: `tests/test_config_validate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_validate.py
"""Tests for config validate subcommand."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


class TestConfigValidate:
    def test_valid_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "channels": {"telegram": {"enabled": False}},
            "claude": {"maxTurns": 50},
        }))
        result = subprocess.run(
            [sys.executable, "-m", "companio", "config-validate", "--config", str(config_file)],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["valid"] is True

    def test_invalid_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("{not valid json")
        result = subprocess.run(
            [sys.executable, "-m", "companio", "config-validate", "--config", str(config_file)],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["valid"] is False
        assert len(output["errors"]) > 0

    def test_missing_file(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        result = subprocess.run(
            [sys.executable, "-m", "companio", "config-validate", "--config", str(config_file)],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["valid"] is True  # missing file = use defaults, which are valid
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config_validate.py -v`
Expected: FAIL — `No such command 'config-validate'`

- [ ] **Step 3: Implement the `config-validate` subcommand**

Add to `companio/cli.py`, after the `gateway` function:

```python
@app.command("config-validate")
def config_validate(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Validate a configuration file and output JSON result."""
    import json as _json
    from companio.config.loader import load_config

    config_path = Path(config) if config else None
    result: dict = {"valid": True, "errors": [], "warnings": []}

    try:
        cfg = load_config(config_path)
    except (ValueError, Exception) as e:
        result["valid"] = False
        result["errors"].append(str(e))
        print(_json.dumps(result))
        raise typer.Exit(1)

    # Check for common misconfigurations
    if cfg.channels.telegram.enabled and not cfg.channels.telegram.token:
        result["warnings"].append("Telegram enabled but token is empty")
    if cfg.channels.slack.enabled and not cfg.channels.slack.bot_token:
        result["warnings"].append("Slack enabled but bot_token is empty")
    if cfg.channels.slack.enabled and not cfg.channels.slack.app_token:
        result["warnings"].append("Slack enabled but app_token is empty")

    # Check Claude CLI availability
    try:
        from companio.core.claude_cli import verify_claude_cli
        verify_claude_cli()
    except (RuntimeError, SystemExit):
        result["warnings"].append("Claude CLI not found on PATH")

    print(_json.dumps(result))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config_validate.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add companio/cli.py tests/test_config_validate.py
git commit -m "feat(cli): add config-validate subcommand for Tauri pre-flight checks"
```

---

## Task 7: Tauri Project Scaffold

**Files:**
- Create: `desktop/src-tauri/Cargo.toml`
- Create: `desktop/src-tauri/tauri.conf.json`
- Create: `desktop/src-tauri/build.rs`
- Create: `desktop/src-tauri/src/main.rs`
- Create: `desktop/ui/index.html`
- Create: `desktop/ui/style.css`
- Create: `desktop/ui/app.js`

Prerequisites: Install Rust and Tauri CLI.
- macOS: `xcode-select --install && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- `cargo install tauri-cli`

- [ ] **Step 1: Create `desktop/src-tauri/Cargo.toml`**

```toml
[package]
name = "companio-desktop"
version = "0.1.0"
edition = "2021"

[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-single-instance = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["process", "io-util", "sync", "time"] }

[build-dependencies]
tauri-build = "2"
```

- [ ] **Step 2: Create `desktop/src-tauri/build.rs`**

```rust
fn main() {
    tauri_build::build()
}
```

- [ ] **Step 3: Create `desktop/src-tauri/tauri.conf.json`**

```json
{
  "$schema": "https://raw.githubusercontent.com/nicedoc/tauri/v2/tooling/cli/schema.json",
  "productName": "compan.io",
  "version": "0.1.0",
  "identifier": "io.compan.desktop",
  "build": {
    "frontendDist": "../ui"
  },
  "app": {
    "windows": [
      {
        "title": "compan.io",
        "width": 480,
        "height": 600,
        "minWidth": 400,
        "minHeight": 500,
        "visible": false,
        "center": true
      }
    ],
    "trayIcon": {
      "iconPath": "icons/icon.png",
      "iconAsTemplate": true
    }
  },
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.icns",
      "icons/icon.ico"
    ]
  }
}
```

- [ ] **Step 4: Create a minimal `desktop/ui/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>compan.io</title>
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <nav id="tabs">
    <button class="tab active" data-view="dashboard">Dashboard</button>
    <button class="tab" data-view="logs">Logs</button>
    <button class="tab" data-view="settings">Settings</button>
    <button class="tab" data-view="files">Files</button>
  </nav>

  <main>
    <section id="view-dashboard" class="view active">
      <div id="status-badge" class="status stopped">
        <span id="status-dot"></span>
        <span id="status-text">Gateway Stopped</span>
        <button id="btn-toggle">Start</button>
      </div>
      <div id="channels-list">
        <h3>Channels</h3>
        <div id="channels-container"><p class="muted">No channels configured</p></div>
      </div>
      <div id="stats">
        <div class="stat"><span class="label">Cron</span><span id="cron-info">-</span></div>
        <div class="stat"><span class="label">Uptime</span><span id="uptime">-</span></div>
      </div>
    </section>

    <section id="view-logs" class="view">
      <div id="log-toolbar">
        <input id="log-filter" type="text" placeholder="Filter logs..." />
        <select id="log-level">
          <option value="DEBUG">DEBUG+</option>
          <option value="INFO" selected>INFO+</option>
          <option value="WARNING">WARNING+</option>
          <option value="ERROR">ERROR+</option>
        </select>
        <button id="btn-clear-logs">Clear</button>
      </div>
      <pre id="log-output"></pre>
    </section>

    <section id="view-settings" class="view">
      <form id="settings-form">
        <fieldset>
          <legend>Telegram</legend>
          <label><input type="checkbox" id="tg-enabled" /> Enabled</label>
          <label>Token <input type="password" id="tg-token" placeholder="123456789:ABC..." /></label>
          <label>Allow From <input type="text" id="tg-allow" placeholder="user1, user2" /></label>
        </fieldset>
        <fieldset>
          <legend>Slack</legend>
          <label><input type="checkbox" id="slack-enabled" /> Enabled</label>
          <label>Bot Token <input type="password" id="slack-bot-token" placeholder="xoxb-..." /></label>
          <label>App Token <input type="password" id="slack-app-token" placeholder="xapp-..." /></label>
          <label>Allow From <input type="text" id="slack-allow" placeholder="U12345, U67890" /></label>
        </fieldset>
        <fieldset>
          <legend>Agent</legend>
          <label>Bot Name <input type="text" id="bot-name" value="companio" /></label>
          <label>Memory Window <input type="number" id="memory-window" value="200" /></label>
        </fieldset>
        <div class="form-actions">
          <button type="submit" id="btn-save">Save</button>
          <button type="button" id="btn-open-config">Edit as JSON</button>
        </div>
      </form>
    </section>

    <section id="view-files" class="view">
      <div id="file-tabs">
        <button class="file-tab active" data-file="memory">MEMORY.md</button>
        <button class="file-tab" data-file="history">HISTORY.md</button>
      </div>
      <pre id="file-content">Select a file to view.</pre>
      <button id="btn-open-workspace">Open Workspace</button>
    </section>
  </main>

  <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 5: Create `desktop/ui/style.css`**

```css
:root {
  --bg: #ffffff;
  --bg-section: #f5f5f7;
  --text: #1d1d1f;
  --text-muted: #86868b;
  --accent: #0071e3;
  --success: #34c759;
  --warning: #ff9f0a;
  --error: #ff3b30;
  --border: #d2d2d7;
  --font: -apple-system, "Segoe UI", system-ui, sans-serif;
  --mono: "SF Mono", "Cascadia Code", Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1c1e;
    --bg-section: #2c2c2e;
    --text: #f5f5f7;
    --text-muted: #98989d;
    --accent: #0a84ff;
    --success: #30d158;
    --warning: #ffd60a;
    --error: #ff453a;
    --border: #38383a;
  }
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--font);
  font-size: 13px;
  color: var(--text);
  background: var(--bg);
  overflow: hidden;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

#tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
  background: var(--bg-section);
  padding: 8px 12px 0;
  gap: 4px;
}

.tab {
  border: none;
  background: none;
  color: var(--text-muted);
  padding: 8px 16px;
  cursor: pointer;
  font-size: 13px;
  border-bottom: 2px solid transparent;
  font-family: var(--font);
}

.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

main {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

.view { display: none; }
.view.active { display: block; }

/* Status badge */
.status {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  border-radius: 10px;
  margin-bottom: 16px;
}

.status.stopped { background: var(--bg-section); }
.status.running { background: rgba(52, 199, 89, 0.1); }
.status.error { background: rgba(255, 59, 48, 0.1); }

#status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--text-muted);
}

.status.running #status-dot { background: var(--success); }
.status.error #status-dot { background: var(--error); }

#status-text { flex: 1; font-size: 15px; font-weight: 600; }

#btn-toggle {
  padding: 6px 20px;
  border: none;
  border-radius: 6px;
  background: var(--accent);
  color: white;
  cursor: pointer;
  font-size: 13px;
  font-family: var(--font);
}

/* Channels */
#channels-list { margin-bottom: 16px; }
#channels-list h3 { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; }

.channel-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
}

.channel-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.channel-dot.connected { background: var(--success); }
.channel-dot.stopped { background: var(--text-muted); }
.channel-dot.reconnecting { background: var(--warning); }
.channel-dot.error { background: var(--error); }

/* Stats */
.stat {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-top: 1px solid var(--border);
}

.stat .label { color: var(--text-muted); }

/* Logs */
#log-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

#log-filter {
  flex: 1;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-section);
  color: var(--text);
  font-family: var(--font);
  font-size: 13px;
}

#log-level {
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-section);
  color: var(--text);
  font-family: var(--font);
}

#log-output {
  background: #1a1a2e;
  color: #cdd6f4;
  padding: 12px;
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 11px;
  overflow-y: auto;
  height: calc(100vh - 140px);
  white-space: pre-wrap;
  word-break: break-all;
}

/* Settings */
fieldset {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}

legend {
  font-weight: 600;
  padding: 0 4px;
}

label {
  display: block;
  margin: 8px 0;
  font-size: 13px;
}

input[type="text"],
input[type="password"],
input[type="number"] {
  display: block;
  width: 100%;
  margin-top: 4px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-section);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
}

.form-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.form-actions button {
  padding: 8px 20px;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-family: var(--font);
  font-size: 13px;
}

#btn-save {
  background: var(--accent);
  color: white;
  border: none;
}

#btn-open-config {
  background: var(--bg-section);
  color: var(--text);
}

/* Files */
#file-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
}

.file-tab {
  border: 1px solid var(--border);
  background: var(--bg-section);
  color: var(--text-muted);
  padding: 6px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-family: var(--font);
  font-size: 13px;
}

.file-tab.active {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}

#file-content {
  background: var(--bg-section);
  padding: 12px;
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 12px;
  overflow-y: auto;
  height: calc(100vh - 180px);
  white-space: pre-wrap;
}

#btn-open-workspace {
  margin-top: 8px;
  padding: 6px 16px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-section);
  color: var(--text);
  cursor: pointer;
  font-family: var(--font);
}

.muted { color: var(--text-muted); font-style: italic; }

#btn-clear-logs {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-section);
  color: var(--text);
  cursor: pointer;
  font-family: var(--font);
}
```

- [ ] **Step 6: Create a minimal `desktop/ui/app.js`**

```javascript
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

// Tab routing
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`view-${tab.dataset.view}`).classList.add('active');
  });
});

// State
let gatewayRunning = false;
let startTime = null;

// Dashboard
const btnToggle = document.getElementById('btn-toggle');
const statusBadge = document.getElementById('status-badge');
const statusText = document.getElementById('status-text');

btnToggle.addEventListener('click', async () => {
  try {
    if (gatewayRunning) {
      await invoke('stop_gateway');
    } else {
      await invoke('start_gateway');
    }
  } catch (e) {
    console.error('Gateway toggle failed:', e);
  }
});

// Listen for status changes from Rust backend
listen('gateway-status', (event) => {
  const { status, channels, cron } = event.payload;
  gatewayRunning = status === 'running';

  statusBadge.className = `status ${status}`;
  statusText.textContent = status === 'running' ? 'Gateway Running'
    : status === 'error' ? 'Gateway Error'
    : 'Gateway Stopped';
  btnToggle.textContent = gatewayRunning ? 'Stop' : 'Start';

  if (gatewayRunning && !startTime) startTime = Date.now();
  if (!gatewayRunning) startTime = null;

  // Update channels
  const container = document.getElementById('channels-container');
  if (channels && Object.keys(channels).length > 0) {
    container.innerHTML = Object.entries(channels).map(([name, info]) =>
      `<div class="channel-row">
        <span class="channel-dot ${info.status}"></span>
        <span>${name}</span>
        <span class="muted">${info.status}</span>
      </div>`
    ).join('');
  }

  // Update cron
  if (cron) {
    document.getElementById('cron-info').textContent =
      `${cron.jobs} jobs`;
  }
});

// Uptime ticker
setInterval(() => {
  if (startTime) {
    const secs = Math.floor((Date.now() - startTime) / 1000);
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    document.getElementById('uptime').textContent =
      h > 0 ? `${h}h ${m}m` : `${m}m`;
  }
}, 5000);

// Logs
const logOutput = document.getElementById('log-output');
const logFilter = document.getElementById('log-filter');
const logLevel = document.getElementById('log-level');
const LOG_LEVELS = { TRACE: 0, DEBUG: 1, INFO: 2, WARNING: 3, ERROR: 4, CRITICAL: 5 };
const LOG_COLORS = { DEBUG: '#6c7086', INFO: '#cdd6f4', WARNING: '#f9e2af', ERROR: '#f38ba8', CRITICAL: '#f38ba8' };
let logLines = [];

listen('log-line', (event) => {
  logLines.push(event.payload);
  if (logLines.length > 500) logLines.shift();
  renderLogs();
});

function renderLogs() {
  const minLevel = LOG_LEVELS[logLevel.value] || 0;
  const filter = logFilter.value.toLowerCase();
  const filtered = logLines.filter(l => {
    if (LOG_LEVELS[l.level] < minLevel) return false;
    if (filter && !l.message.toLowerCase().includes(filter)) return false;
    return true;
  });
  logOutput.innerHTML = filtered.map(l => {
    const color = LOG_COLORS[l.level] || '#cdd6f4';
    const time = l.timestamp ? l.timestamp.slice(11, 19) : '';
    return `<span style="color:${color}">[${time}] [${l.level}] ${escapeHtml(l.message)}</span>`;
  }).join('\n');
  logOutput.scrollTop = logOutput.scrollHeight;
}

logFilter.addEventListener('input', renderLogs);
logLevel.addEventListener('change', renderLogs);
document.getElementById('btn-clear-logs').addEventListener('click', () => {
  logLines = [];
  logOutput.innerHTML = '';
});

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Settings
document.getElementById('settings-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await invoke('save_config', { config: gatherFormData() });
  } catch (err) {
    console.error('Save failed:', err);
  }
});

document.getElementById('btn-open-config').addEventListener('click', async () => {
  try { await invoke('open_config_file'); } catch (e) { console.error(e); }
});

async function loadConfig() {
  try {
    const config = await invoke('load_config');
    if (config.channels?.telegram) {
      document.getElementById('tg-enabled').checked = config.channels.telegram.enabled;
      document.getElementById('tg-token').value = config.channels.telegram.token || '';
      document.getElementById('tg-allow').value = (config.channels.telegram.allowFrom || []).join(', ');
    }
    if (config.channels?.slack) {
      document.getElementById('slack-enabled').checked = config.channels.slack.enabled;
      document.getElementById('slack-bot-token').value = config.channels.slack.botToken || '';
      document.getElementById('slack-app-token').value = config.channels.slack.appToken || '';
      document.getElementById('slack-allow').value = (config.channels.slack.allowFrom || []).join(', ');
    }
    if (config.agents?.defaults) {
      document.getElementById('bot-name').value = config.agents.defaults.botName || 'companio';
      document.getElementById('memory-window').value = config.agents.defaults.memoryWindow || 200;
    }
  } catch (e) { console.error('Load config failed:', e); }
}

function gatherFormData() {
  return {
    channels: {
      telegram: {
        enabled: document.getElementById('tg-enabled').checked,
        token: document.getElementById('tg-token').value,
        allowFrom: document.getElementById('tg-allow').value.split(',').map(s => s.trim()).filter(Boolean),
      },
      slack: {
        enabled: document.getElementById('slack-enabled').checked,
        botToken: document.getElementById('slack-bot-token').value,
        appToken: document.getElementById('slack-app-token').value,
        allowFrom: document.getElementById('slack-allow').value.split(',').map(s => s.trim()).filter(Boolean),
      },
    },
    agents: {
      defaults: {
        botName: document.getElementById('bot-name').value,
        memoryWindow: parseInt(document.getElementById('memory-window').value),
      },
    },
  };
}

// Files
document.querySelectorAll('.file-tab').forEach(tab => {
  tab.addEventListener('click', async () => {
    document.querySelectorAll('.file-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    try {
      const content = await invoke('read_workspace_file', { name: tab.dataset.file });
      document.getElementById('file-content').textContent = content;
    } catch (e) {
      document.getElementById('file-content').textContent = `Error: ${e}`;
    }
  });
});

document.getElementById('btn-open-workspace').addEventListener('click', async () => {
  try { await invoke('open_workspace'); } catch (e) { console.error(e); }
});

// Init
loadConfig();
```

- [ ] **Step 7: Verify the scaffold compiles**

Run from `desktop/`:

```bash
cd desktop && cargo tauri build --debug 2>&1 | head -50
```

This may require creating placeholder icon files first. Create minimal 32x32 PNG icons:

```bash
mkdir -p desktop/src-tauri/icons
# Create a 1x1 pixel PNG as placeholder (replace with real icons later)
printf '\x89PNG\r\n\x1a\n' > desktop/src-tauri/icons/icon.png
cp desktop/src-tauri/icons/icon.png desktop/src-tauri/icons/32x32.png
cp desktop/src-tauri/icons/icon.png desktop/src-tauri/icons/128x128.png
cp desktop/src-tauri/icons/icon.png desktop/src-tauri/icons/128x128@2x.png
```

For now, just verify Rust compilation succeeds. The full build may fail on icons, which is expected.

- [ ] **Step 8: Commit**

```bash
git add desktop/
git commit -m "feat(desktop): scaffold Tauri app with UI shell (dashboard, logs, settings, files)"
```

---

## Task 8: Tauri Rust Backend — Gateway Process Management

**Files:**
- Create: `desktop/src-tauri/src/gateway.rs`
- Create: `desktop/src-tauri/src/logs.rs`
- Create: `desktop/src-tauri/src/platform.rs`
- Modify: `desktop/src-tauri/src/main.rs`

- [ ] **Step 1: Create `desktop/src-tauri/src/platform.rs`**

```rust
use std::process::Command;

/// Detect if `companio` CLI is available on PATH.
pub fn find_companio() -> Option<String> {
    let cmd = if cfg!(windows) { "where" } else { "which" };
    Command::new(cmd)
        .arg("companio")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
}

/// Detect if `claude` CLI is available on PATH.
pub fn find_claude_cli() -> Option<String> {
    let cmd = if cfg!(windows) { "where" } else { "which" };
    Command::new(cmd)
        .arg("claude")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
}

/// Get the default config path.
pub fn default_config_path() -> std::path::PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".companio")
        .join("config.json")
}
```

Note: Add `dirs = "6"` to `Cargo.toml` dependencies.

- [ ] **Step 2: Create `desktop/src-tauri/src/logs.rs`**

```rust
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::Mutex;

const MAX_LOG_LINES: usize = 500;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogLine {
    pub level: String,
    pub message: String,
    pub timestamp: String,
}

pub struct LogBuffer {
    lines: Mutex<VecDeque<LogLine>>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            lines: Mutex::new(VecDeque::with_capacity(MAX_LOG_LINES)),
        }
    }

    pub fn push(&self, line: LogLine) {
        let mut buf = self.lines.lock().unwrap();
        if buf.len() >= MAX_LOG_LINES {
            buf.pop_front();
        }
        buf.push_back(line);
    }

    pub fn get_all(&self) -> Vec<LogLine> {
        self.lines.lock().unwrap().iter().cloned().collect()
    }

    pub fn clear(&self) {
        self.lines.lock().unwrap().clear();
    }
}

/// Parse a loguru JSON serialized line from stderr into a LogLine.
pub fn parse_loguru_json(raw: &str) -> Option<LogLine> {
    let v: serde_json::Value = serde_json::from_str(raw).ok()?;
    let record = v.get("record")?;
    Some(LogLine {
        level: record.get("level")?.get("name")?.as_str()?.to_string(),
        message: record.get("message")?.as_str()?.to_string(),
        timestamp: record.get("time")?.get("repr")?.as_str()?.unwrap_or("").to_string(),
    })
}
```

- [ ] **Step 3: Create `desktop/src-tauri/src/gateway.rs`**

```rust
use crate::logs::{parse_loguru_json, LogBuffer, LogLine};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum GatewayStatus {
    Stopped,
    Starting,
    Running,
    Error,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusPayload {
    pub status: GatewayStatus,
    pub channels: serde_json::Value,
    pub cron: serde_json::Value,
}

pub struct GatewayState {
    pub child: Mutex<Option<Child>>,
    pub status: Mutex<GatewayStatus>,
    pub log_buffer: Arc<LogBuffer>,
}

impl GatewayState {
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
            status: Mutex::new(GatewayStatus::Stopped),
            log_buffer: Arc::new(LogBuffer::new()),
        }
    }
}

pub async fn start_gateway(
    app: AppHandle,
    state: Arc<GatewayState>,
    config_path: String,
) -> Result<(), String> {
    {
        let status = state.status.lock().unwrap();
        if *status == GatewayStatus::Running || *status == GatewayStatus::Starting {
            return Err("Gateway is already running".into());
        }
    }

    *state.status.lock().unwrap() = GatewayStatus::Starting;
    emit_status(&app, &state);

    let mut cmd = Command::new("companio");
    cmd.arg("gateway")
        .arg("--tauri")
        .arg("--config")
        .arg(&config_path)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("Failed to spawn gateway: {}", e))?;

    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();

    *state.child.lock().unwrap() = Some(child);

    // Spawn stdout reader (IPC events)
    let app_clone = app.clone();
    let state_clone = state.clone();
    tokio::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            handle_ipc_event(&app_clone, &state_clone, &line);
        }
        // stdout closed = process exited
        let mut status = state_clone.status.lock().unwrap();
        if *status == GatewayStatus::Running {
            *status = GatewayStatus::Error;
        }
        drop(status);
        emit_status(&app_clone, &state_clone);
    });

    // Spawn stderr reader (logs)
    let app_clone2 = app.clone();
    let log_buffer = state.log_buffer.clone();
    tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(log_line) = parse_loguru_json(&line) {
                log_buffer.push(log_line.clone());
                let _ = app_clone2.emit("log-line", log_line);
            } else {
                let fallback = LogLine {
                    level: "INFO".into(),
                    message: line,
                    timestamp: String::new(),
                };
                log_buffer.push(fallback.clone());
                let _ = app_clone2.emit("log-line", fallback);
            }
        }
    });

    Ok(())
}

pub async fn stop_gateway(state: Arc<GatewayState>) -> Result<(), String> {
    let mut child_guard = state.child.lock().unwrap();
    if let Some(ref mut child) = *child_guard {
        let _ = child.kill().await;
        *state.status.lock().unwrap() = GatewayStatus::Stopped;
        *child_guard = None;
        Ok(())
    } else {
        Err("Gateway is not running".into())
    }
}

fn handle_ipc_event(app: &AppHandle, state: &Arc<GatewayState>, line: &str) {
    if let Ok(event) = serde_json::from_str::<serde_json::Value>(line) {
        let event_type = event.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let data = event.get("data").cloned().unwrap_or(serde_json::Value::Null);

        match event_type {
            "ready" => {
                *state.status.lock().unwrap() = GatewayStatus::Running;
                emit_status(app, state);
            }
            "health" => {
                let channels = data.get("channels").cloned().unwrap_or_default();
                let cron = data.get("cron").cloned().unwrap_or_default();
                let payload = StatusPayload {
                    status: GatewayStatus::Running,
                    channels,
                    cron,
                };
                let _ = app.emit("gateway-status", payload);
            }
            "error" => {
                let fatal = data.get("fatal").and_then(|f| f.as_bool()).unwrap_or(false);
                if fatal {
                    *state.status.lock().unwrap() = GatewayStatus::Error;
                    emit_status(app, state);
                }
            }
            "shutdown" => {
                *state.status.lock().unwrap() = GatewayStatus::Stopped;
                emit_status(app, state);
            }
            _ => {}
        }
    }
}

fn emit_status(app: &AppHandle, state: &Arc<GatewayState>) {
    let status = state.status.lock().unwrap().clone();
    let payload = StatusPayload {
        status,
        channels: serde_json::Value::Object(Default::default()),
        cron: serde_json::Value::Object(Default::default()),
    };
    let _ = app.emit("gateway-status", payload);
}
```

- [ ] **Step 4: Create `desktop/src-tauri/src/main.rs`**

```rust
mod gateway;
mod logs;
mod platform;

use gateway::{GatewayState, start_gateway, stop_gateway};
use std::sync::Arc;
use tauri::{
    Manager,
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
};

#[tauri::command]
async fn cmd_start_gateway(
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<GatewayState>>,
) -> Result<(), String> {
    let config_path = platform::default_config_path()
        .to_string_lossy()
        .to_string();
    start_gateway(app, state.inner().clone(), config_path).await
}

#[tauri::command]
async fn cmd_stop_gateway(
    state: tauri::State<'_, Arc<GatewayState>>,
) -> Result<(), String> {
    stop_gateway(state.inner().clone()).await
}

#[tauri::command]
async fn load_config() -> Result<serde_json::Value, String> {
    let path = platform::default_config_path();
    if !path.exists() {
        return Ok(serde_json::json!({}));
    }
    let content = std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read config: {}", e))?;
    serde_json::from_str(&content)
        .map_err(|e| format!("Failed to parse config: {}", e))
}

#[tauri::command]
async fn save_config(config: serde_json::Value) -> Result<(), String> {
    let path = platform::default_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create config dir: {}", e))?;
    }
    // Read existing config and merge (preserve fields not in the form)
    let mut existing: serde_json::Value = if path.exists() {
        let content = std::fs::read_to_string(&path).unwrap_or_default();
        serde_json::from_str(&content).unwrap_or(serde_json::json!({}))
    } else {
        serde_json::json!({})
    };
    merge_json(&mut existing, &config);
    let content = serde_json::to_string_pretty(&existing)
        .map_err(|e| format!("Failed to serialize config: {}", e))?;
    std::fs::write(&path, content)
        .map_err(|e| format!("Failed to write config: {}", e))
}

fn merge_json(base: &mut serde_json::Value, patch: &serde_json::Value) {
    if let (Some(base_obj), Some(patch_obj)) = (base.as_object_mut(), patch.as_object()) {
        for (key, value) in patch_obj {
            if value.is_object() && base_obj.get(key).map_or(false, |v| v.is_object()) {
                merge_json(base_obj.get_mut(key).unwrap(), value);
            } else {
                base_obj.insert(key.clone(), value.clone());
            }
        }
    }
}

#[tauri::command]
async fn read_workspace_file(name: String) -> Result<String, String> {
    let workspace = dirs::home_dir()
        .unwrap_or_default()
        .join(".companio")
        .join("workspace")
        .join("memory");
    let filename = match name.as_str() {
        "memory" => "MEMORY.md",
        "history" => "HISTORY.md",
        _ => return Err("Unknown file".into()),
    };
    std::fs::read_to_string(workspace.join(filename))
        .map_err(|e| format!("Failed to read {}: {}", filename, e))
}

#[tauri::command]
async fn open_config_file() -> Result<(), String> {
    let path = platform::default_config_path();
    open::that(&path).map_err(|e| format!("Failed to open: {}", e))
}

#[tauri::command]
async fn open_workspace() -> Result<(), String> {
    let path = dirs::home_dir()
        .unwrap_or_default()
        .join(".companio")
        .join("workspace");
    open::that(&path).map_err(|e| format!("Failed to open: {}", e))
}

#[tauri::command]
async fn get_logs(
    state: tauri::State<'_, Arc<GatewayState>>,
) -> Result<Vec<logs::LogLine>, String> {
    Ok(state.log_buffer.get_all())
}

fn main() {
    let gateway_state = Arc::new(GatewayState::new());

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .manage(gateway_state)
        .invoke_handler(tauri::generate_handler![
            cmd_start_gateway,
            cmd_stop_gateway,
            load_config,
            save_config,
            read_workspace_file,
            open_config_file,
            open_workspace,
            get_logs,
        ])
        .setup(|app| {
            // System tray
            let quit = MenuItemBuilder::new("Quit compan.io").id("quit").build(app)?;
            let show = MenuItemBuilder::new("Open Dashboard").id("show").build(app)?;
            let menu = MenuBuilder::new(app)
                .item(&show)
                .separator()
                .item(&quit)
                .build()?;

            TrayIconBuilder::new()
                .menu(&menu)
                .on_menu_event(move |app, event| {
                    match event.id().as_ref() {
                        "quit" => {
                            app.exit(0);
                        }
                        "show" => {
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                        _ => {}
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            // Hide to tray on close instead of quitting
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

Note: Add `open = "5"` and `dirs = "6"` to `Cargo.toml` dependencies:

```toml
[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-single-instance = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["process", "io-util", "sync", "time"] }
dirs = "6"
open = "5"
```

- [ ] **Step 5: Verify compilation**

```bash
cd desktop && cargo check 2>&1 | tail -20
```

Expected: Compilation succeeds (or only icon-related warnings)

- [ ] **Step 6: Commit**

```bash
git add desktop/
git commit -m "feat(desktop): implement Tauri Rust backend — gateway management, log capture, config I/O"
```

---

## Task 9: Update `.gitignore` and Verify Full Build

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add Tauri build artifacts to `.gitignore`**

Append to `.gitignore`:

```
# Tauri
desktop/src-tauri/target/
desktop/src-tauri/gen/
```

- [ ] **Step 2: Run existing Python tests to verify zero regression**

```bash
python -m pytest tests/ -v
```

Expected: All existing tests PASS. No Python behavior changed when `--tauri` flag is not used.

- [ ] **Step 3: Run the new tests**

```bash
python -m pytest tests/test_ipc.py tests/test_gateway_health.py tests/test_config_validate.py -v
```

Expected: All new tests PASS.

- [ ] **Step 4: Verify Tauri dev mode works**

```bash
cd desktop && cargo tauri dev
```

Expected: App window opens with the Dashboard view. Start button is visible. (Gateway won't start unless `companio` is on PATH and configured.)

- [ ] **Step 5: Commit**

```bash
git add .gitignore
git commit -m "chore: add Tauri build artifacts to gitignore"
```

---

## Task 10: Generate App Icons

**Files:**
- Modify: `desktop/src-tauri/icons/`

- [ ] **Step 1: Create a simple app icon**

Use the existing robot emoji (🤖) from `companio/__init__.py` as the design basis. Create a 1024x1024 PNG source icon. If no design tool is available, use a solid blue circle with a white robot emoji as a placeholder.

You can use Tauri's icon generator:

```bash
cd desktop && cargo tauri icon path/to/icon-1024x1024.png
```

This generates all required sizes (32x32, 128x128, 128x128@2x, icon.ico, icon.icns) in `src-tauri/icons/`.

- [ ] **Step 2: Verify icons are in place**

```bash
ls desktop/src-tauri/icons/
```

Expected: `32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.ico`, `icon.icns`, `icon.png`

- [ ] **Step 3: Commit**

```bash
git add desktop/src-tauri/icons/
git commit -m "chore(desktop): add app icons"
```

---

## Task 11: End-to-End Integration Test

This task verifies the full flow works: Tauri launches → Python gateway starts → IPC events flow → UI updates.

- [ ] **Step 1: Manual test — start gateway from Tauri**

1. Ensure `companio` is installed and on PATH: `which companio`
2. Ensure config exists: `cat ~/.companio/config.json`
3. Run: `cd desktop && cargo tauri dev`
4. Click "Start" in the Dashboard
5. Verify: status changes to "Gateway Running" (green badge)
6. Verify: channel health dots appear
7. Switch to Logs tab — verify log lines appear
8. Switch to Settings tab — verify config values are loaded
9. Click "Stop" — verify status returns to "Gateway Stopped"
10. Close window (X button) — verify app stays in system tray
11. Click tray icon → "Open Dashboard" — verify window reappears
12. Click tray icon → "Quit compan.io" — verify app exits

- [ ] **Step 2: Test on the other platform**

If developing on macOS, test on Windows (or vice versa). Key differences to verify:
- System tray icon appears correctly
- Process spawn/kill works
- Window hide-to-tray behavior
- Config path resolution (`~/.companio/config.json`)

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat(desktop): compan.io Tauri desktop app MVP"
```

---

## Summary

| Task | Description | Est. Time |
|------|-------------|-----------|
| 1 | IPC module (`companio/ipc.py`) | 15 min |
| 2 | Channel health reporting | 30 min |
| 3 | `active_tasks_count` property | 10 min |
| 4 | `--tauri` flag + signal handlers | 45 min |
| 5 | Error events before `sys.exit` | 15 min |
| 6 | `config validate` subcommand | 30 min |
| 7 | Tauri project scaffold (UI) | 45 min |
| 8 | Tauri Rust backend | 60 min |
| 9 | Gitignore + regression tests | 15 min |
| 10 | App icons | 15 min |
| 11 | End-to-end integration test | 30 min |
| **Total** | | **~5-6 hours** |

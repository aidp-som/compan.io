"""CLI commands for companio."""

import asyncio
import json
import os
import select
import signal
import sys
from pathlib import Path

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from loguru import logger
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from companio import __logo__, __version__
from companio.config.schema import Config
from companio.helpers import sync_workspace_templates

app = typer.Typer(
    name="companio",
    help=f"{__logo__} companio - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from companio.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} companio[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} companio v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """companio - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize companio configuration and workspace."""
    from companio.config.loader import get_config_path, load_config, save_config
    from companio.config.schema import Config
    from companio.core.claude_cli import verify_claude_cli

    config_path = get_config_path()

    # If config already exists, ask what to do
    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Re-run setup? (existing values will be used as defaults)", default=False):
            console.print("Aborted.")
            return
        config = load_config()
    else:
        config = Config()

    console.print(f"\n{__logo__} [bold]companio setup[/bold]\n")

    # --- Check Claude CLI ---
    console.print("[bold cyan]Step 1:[/bold cyan] Claude CLI")
    try:
        version = verify_claude_cli()
        console.print(f"  [green]✓[/green] Claude CLI found: {version}")
    except RuntimeError as exc:
        console.print(f"  [red]✗[/red] {exc}")
        console.print("  Install Claude CLI: https://claude.ai/code")
        console.print("  [dim]companio requires Claude CLI to be installed and authenticated.[/dim]\n")

    # --- Optional dependency check ---
    import shutil

    _OPTIONAL_DEPS = [
        {
            "name": "Node.js (npx)",
            "check": "npx",
            "install": "brew install node  (or https://nodejs.org/)",
            "used_by": "MCP servers (Playwright, GitHub, Slack, Filesystem)",
        },
        {
            "name": "Google Workspace CLI",
            "check": "gws",
            "install": "npm install -g @googleworkspace/cli",
            "used_by": "Google Workspace skill (Gmail, Drive, Calendar, Sheets)",
        },
    ]

    missing_deps = []
    for dep in _OPTIONAL_DEPS:
        if not shutil.which(dep["check"]):
            missing_deps.append(dep)

    if missing_deps:
        console.print("[bold yellow]Optional dependencies not found:[/bold yellow]\n")
        for dep in missing_deps:
            console.print(f"  [yellow]•[/yellow] [bold]{dep['name']}[/bold]")
            console.print(f"    Used by: {dep['used_by']}")
            console.print(f"    Install: [cyan]{dep['install']}[/cyan]")
        console.print("\n  [dim]These are optional — companio works without them, but related features will be unavailable.[/dim]\n")
    else:
        console.print("[green]✓[/green] All optional dependencies found.\n")

    # --- Step 2: Claude CLI settings ---
    console.print("\n[bold cyan]Step 2:[/bold cyan] Claude CLI Settings")

    config.claude.max_turns = int(typer.prompt(
        "  Max turns per request",
        default=str(config.claude.max_turns),
    ))
    config.claude.timeout = int(typer.prompt(
        "  Timeout (seconds)",
        default=str(config.claude.timeout),
    ))
    config.claude.max_concurrent = int(typer.prompt(
        "  Max concurrent sessions",
        default=str(config.claude.max_concurrent),
    ))

    # --- Step 3: Telegram ---
    console.print("\n[bold cyan]Step 3:[/bold cyan] Telegram Integration")
    if typer.confirm("  Enable Telegram bot?", default=config.channels.telegram.enabled):
        config.channels.telegram.enabled = True
        token = typer.prompt(
            "  Bot token (from @BotFather)",
            default=config.channels.telegram.token or "",
            show_default=False,
        )
        if token:
            config.channels.telegram.token = token

        allow_from_str = typer.prompt(
            "  Allowed usernames (comma-separated)",
            default=",".join(config.channels.telegram.allow_from) if config.channels.telegram.allow_from else "",
            show_default=False,
        )
        if allow_from_str:
            config.channels.telegram.allow_from = [u.strip() for u in allow_from_str.split(",") if u.strip()]

        config.channels.telegram.reply_to_message = typer.confirm(
            "  Reply with quote?", default=config.channels.telegram.reply_to_message
        )
    else:
        config.channels.telegram.enabled = False

    # --- Step 4: Channel behavior ---
    console.print("\n[bold cyan]Step 4:[/bold cyan] Channel Behavior")
    config.channels.send_progress = typer.confirm(
        "  Stream text progress to channel?", default=config.channels.send_progress
    )

    # --- Save config ---
    save_config(config)
    console.print(f"\n[green]✓[/green] Config saved to {config_path}")

    # Create workspace
    workspace = config.workspace_path
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    # Done
    console.print(f"\n{__logo__} [bold green]companio is ready![/bold green]")
    console.print(f"\n  Config: [cyan]{config_path}[/cyan]")
    console.print(f"  Workspace: [cyan]{workspace}[/cyan]")

    if config.channels.telegram.enabled:
        console.print('\n  Start gateway: [cyan]companio gateway[/cyan]')
    console.print('  Chat: [cyan]companio agent -m "Hello!"[/cyan]')


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from companio.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    tauri: bool = typer.Option(False, "--tauri", help="Enable structured IPC output for Tauri desktop wrapper", hidden=True),
):
    """Start the companio gateway."""
    from companio.bus import MessageBus
    from companio.channels.manager import ChannelManager
    from companio.config.paths import get_cron_dir
    from companio.core.claude_cli import ClaudeCLI, verify_claude_cli
    from companio.core.loop import AgentLoop
    from companio.cron import CronJob, CronService
    from companio.session import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    # Warn if running from home directory — Claude CLI would have access to everything under ~/
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    if cwd == home or cwd == home / ".":
        console.print(
            "[bold yellow]Warning:[/bold yellow] Running gateway from your home directory (~/).\n"
            "  Claude CLI will use this as its working directory, giving it access to all files under ~/.\n"
            "  Consider running from a more specific directory (e.g., your workspace)."
        )
        if not typer.confirm("  Continue anyway?", default=False):
            raise typer.Exit(0)

    config = _load_runtime_config(config, workspace)

    if tauri:
        out = Console(stderr=True)
        logger.remove()
        logger.add(sys.stderr, serialize=True, level="INFO")
    else:
        out = console

    verify_claude_cli()  # fails fast if claude not installed

    out.print(f"{__logo__} Starting companio gateway on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()

    from companio.config.paths import get_claude_project_dir, sync_user_mcp_servers

    # Sync user-scope MCP servers from ~/.claude.json to project dir
    if sync_user_mcp_servers():
        out.print("[green]✓[/green] Synced user MCP servers to project")

    # Register crosspost MCP server if enabled
    if config.crosspost.enabled:
        from companio.config.paths import get_data_dir

        project_dir = get_claude_project_dir()
        mcp_json_path = project_dir / ".mcp.json"
        mcp_config = json.loads(mcp_json_path.read_text()) if mcp_json_path.exists() else {}
        mcp_servers = mcp_config.setdefault("mcpServers", {})

        crosspost_config_path = get_data_dir() / "crosspost_routes.json"
        crosspost_config_path.write_text(
            json.dumps(
                {"routes": {k: v.model_dump() for k, v in config.crosspost.routes.items()}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        slack_token = ""
        slack_cfg = getattr(config.channels, "slack", None)
        if slack_cfg and getattr(slack_cfg, "bot_token", None):
            slack_token = slack_cfg.bot_token

        mcp_servers["crosspost"] = {
            "type": "stdio",
            "command": str(Path(sys.executable)),
            "args": [str(Path(__file__).resolve().parent / "mcp" / "crosspost_server.py")],
            "env": {
                "CROSSPOST_TOKEN": slack_token,
                "CROSSPOST_CONFIG_PATH": str(crosspost_config_path),
            },
        }
        mcp_json_path.write_text(json.dumps(mcp_config, indent=2, ensure_ascii=False), encoding="utf-8")
        out.print(f"[green]✓[/green] Crosspost MCP: {len(config.crosspost.routes)} channels registered")

    claude = ClaudeCLI(
        project_dir=get_claude_project_dir(),
        workspace_dir=config.workspace_path,
        max_turns=config.claude.max_turns,
        timeout=config.claude.timeout,
        max_concurrent=config.claude.max_concurrent,
        model=config.claude.model,
    )

    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        claude=claude,
        workspace=config.workspace_path,
        memory_window=config.agents.defaults.memory_window,
        bot_name=config.agents.defaults.bot_name,
        cron_service=cron,
        session_manager=session_manager,
        config=config,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        # Direct message delivery — bypasses Claude CLI entirely
        if job.payload.kind == "message":
            if job.payload.deliver and job.payload.to and job.payload.message:
                from companio.bus import OutboundMessage

                await bus.publish_outbound(
                    OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=job.payload.message,
                        metadata=job.payload.metadata or {},
                    )
                )
            return job.payload.message

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_metadata: dict = {}

        # 채널 히스토리 자동 주입 (payload.metadata.fetchChannelContext == true)
        if (
            job.payload.metadata.get("fetchChannelContext")
            and job.payload.channel == "slack"
            and job.payload.to
        ):
            slack_ch = channels.get_channel("slack")
            if slack_ch and hasattr(slack_ch, "fetch_channel_context"):
                limit = job.payload.metadata.get("channelContextLimit", 30)
                ctx = await slack_ch.fetch_channel_context(job.payload.to, limit=limit)
                if ctx:
                    cron_metadata["_channel_context_text"] = ctx
                    logger.info(
                        "Cron {} injected channel context for {} ({} chars)",
                        job.id, job.payload.to, len(ctx),
                    )

        response = await agent.process_direct(
            reminder_note,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            ephemeral=True,
            metadata=cron_metadata or None,
            sender_id=job.created_by,
        )

        # NOTE: MessageSender cannot be injected into Claude CLI subprocess.
        # _sent_in_turn is always False. The fallback below is the only delivery path.
        # If message tool injection is implemented later, review for double-send risk.
        if agent.message_sender._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from companio.bus import OutboundMessage

            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                    metadata=job.payload.metadata or {},
                )
            )
        return response

    # Create channel manager (before assigning cron callback — on_cron_job
    # references `channels` for channel context fetch)
    channels = ChannelManager(config, bus)
    cron.on_job = on_cron_job

    if channels.enabled_channels:
        out.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        out.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        out.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    async def run():
        shutdown_event = asyncio.Event()

        if tauri:
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

    asyncio.run(run())


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
        # Explicitly check JSON syntax before calling load_config (which swallows errors)
        if config_path is not None and config_path.exists():
            with open(config_path, encoding="utf-8") as _f:
                _json.load(_f)
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


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(
        True, "--markdown/--no-markdown", help="Render assistant output as Markdown"
    ),
    logs: bool = typer.Option(
        False, "--logs/--no-logs", help="Show companio runtime logs during chat"
    ),
):
    """Interact with the agent directly."""
    from loguru import logger

    from companio.bus import MessageBus
    from companio.config.paths import get_cron_dir
    from companio.core.claude_cli import ClaudeCLI, verify_claude_cli
    from companio.core.loop import AgentLoop
    from companio.cron import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    verify_claude_cli()  # fails fast if claude not installed

    bus = MessageBus()

    from companio.config.paths import get_claude_project_dir, sync_user_mcp_servers

    sync_user_mcp_servers()

    claude = ClaudeCLI(
        project_dir=get_claude_project_dir(),
        workspace_dir=config.workspace_path,
        max_turns=config.claude.max_turns,
        timeout=config.claude.timeout,
        max_concurrent=config.claude.max_concurrent,
        model=config.claude.model,
    )

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("companio")
    else:
        logger.disable("companio")

    agent_loop = AgentLoop(
        bus=bus,
        claude=claude,
        workspace=config.workspace_path,
        memory_window=config.agents.defaults.memory_window,
        bot_name=config.agents.defaults.bot_name,
        cron_service=cron,
        config=config,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext

            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]companio is thinking...[/dim]", spinner="dots")

    if message:
        # Single message mode -- direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(
                    message, session_id
                )
            _print_agent_response(response, render_markdown=markdown)

        asyncio.run(run_once())
    else:
        # Interactive mode -- route through bus like other channels
        from companio.bus import InboundMessage

        _init_prompt_session()
        console.print(
            f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n"
        )

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(
                            InboundMessage(
                                channel=cli_channel,
                                sender_id="user",
                                chat_id=cli_chat_id,
                                content=user_input,
                            )
                        )

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from companio.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row("Telegram", "✓" if tg.enabled else "✗", tg_config)

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show companio status."""
    import shutil
    import subprocess

    from companio.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} companio Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        # Claude CLI status
        claude_path = shutil.which("claude")
        if claude_path:
            try:
                proc = subprocess.run(
                    [claude_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                claude_version = proc.stdout.strip() or proc.stderr.strip() or "unknown"
                console.print(f"Claude CLI: [green]✓ {claude_version}[/green]")
            except Exception:
                console.print("Claude CLI: [yellow]found but version check failed[/yellow]")
        else:
            console.print("Claude CLI: [red]✗ not found[/red]")
            console.print("  Install: https://claude.ai/code")


if __name__ == "__main__":
    app()

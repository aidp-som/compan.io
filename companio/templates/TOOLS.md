# Tool Usage Notes

companio delegates all tool execution to Claude CLI (`claude -p`). Claude CLI's built-in tools (Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, etc.) are available automatically — no configuration required.

## companio-Specific Tools

Two additional tools are injected by companio:

### `message`
- Sends a message to a specific chat channel (e.g., Telegram)
- Use this to proactively notify the user, such as from cron tasks
- Requires a channel name and chat ID

### `cron`
- Schedules reminders and recurring tasks, managed by companio's CronService
- Three modes: reminder (direct message), task (agent executes), one-time (auto-deletes after firing)
- Scheduling options: `every_seconds`, `cron_expr` (with optional `tz`), `at` (ISO datetime)
- Refer to the cron skill for detailed usage

## Channel Auto-Context

### Slack thread auto-fetch

When you are mentioned (`@<bot>`) inside an existing Slack thread, the channel adapter automatically fetches up to N prior messages from that thread (default 20, capped per `SlackConfig.thread_context_max_limit`) and prepends them to your input as a `<external-context trust="low">` block. You receive that block as part of the user message — no tool call needed. Required Slack OAuth scopes: `channels:history`, `groups:history`, `im:history`, `mpim:history`, plus `users:read` for display-name resolution. The fetch is silently disabled at runtime if the workspace is missing any of these scopes.

Phrases like "전체 다 읽어줘", "처음부터", "이전 30개" in the user message escalate the fetch limit (shadow-detected via regex; logged as `slack.thread_context.shadow_match`).

## Workspace Files

The following files in the workspace directory are managed by companio:
- `MEMORY.md` — persistent notes across sessions
- `HISTORY.md` — conversation history log
- `skills/` — skill definitions loaded into the system prompt

## Security

- **Secret filtering**: companio strips sensitive environment variables (API keys, tokens, passwords) before spawning Claude CLI, and also filters secrets from Claude CLI's output
- companio does not enforce workspace path restrictions — Claude CLI manages its own tool permissions

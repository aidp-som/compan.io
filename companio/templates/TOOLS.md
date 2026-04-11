# Tool Usage Notes

companio delegates all tool execution to Claude CLI (`claude -p`). Claude CLI's built-in tools (Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, etc.) are available automatically — no configuration required.

## What companio does NOT inject

⚠️ **There are no companio-specific LLM-callable tools.** Earlier versions of this document advertised `message`, `cron`, and `share_to_channel` as "companio-injected tools", but Claude CLI subprocess cannot reach companio's Python objects — those are in-process artifacts, not MCP tools. If you try to call them, the call will silently fail and you will hallucinate an explanation. **Do not invent or claim such tools in your responses.**

What this means for you:
- **You only have Claude CLI's built-in tools** (Read, Write, Edit, Glob, Grep, WebFetch, WebSearch — and Bash if your role allows it) plus any MCP servers registered in `<project_dir>/.mcp.json`.
- **Channel actions (send a message, schedule a cron, broadcast to channel root)** are not initiated by you. They are triggered by:
  - **The user's natural-language request being post-analyzed by companio** (regex-based shadow detection on the inbound text). For example, the user saying *"채널 본문에 공지해줘"* will cause companio to broadcast your reply to the channel root automatically — you do not call any tool for this. Just write your reply normally.
  - **Cron schedules**, where companio invokes you on a fixed schedule and forwards your reply to a configured channel.
  - **The channel adapter** (e.g. Slack `_on_app_mention`) handling reactions, ack, progress pulse, and thread-context fetch automatically based on inbound events and config flags — entirely outside your awareness.

If a user asks you to do something that would require an LLM-callable tool you don't have (e.g. *"DM Bob the result"*, *"set a reminder for tomorrow"*), the honest answer is to explain you cannot trigger those actions yourself, and either:
- Ask the user to phrase the request in a way the channel adapter recognizes (e.g. *"채널에 공지해줘"* for broadcast), OR
- Suggest they use the companio CLI directly for cron / direct messaging.

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

---
name: companio-init
description: >
  Bootstrap a new companio bot instance — directory creation, config setup, and process manager
  registration (systemd on Linux, PM2 on macOS). Use this skill whenever the user wants to create
  a new companio instance, set up a new bot, or deploy companio to a new environment.
  Trigger on: "인스턴스 생성", "새 봇 만들어", "companio init", "봇 셋업", "instance create",
  "new bot setup", "봇 추가", "새 인스턴스", "deploy new bot", or when the user mentions
  creating a new Telegram/Slack bot instance for a project.
---

# Companio Instance Init

Create and register a new companio bot instance. This skill handles the full lifecycle:
gather parameters, scaffold the directory, configure the channel, register with the OS process manager, and verify.

## Step 1: Gather Parameters

Collect these from the user (ask for anything missing):

| Parameter | Required | Example | Notes |
|-----------|----------|---------|-------|
| **Instance name** | Yes | `companio-myproject` | Auto-prefix `companio-` if user omits it |
| **Channel type** | Yes | `telegram` or `slack` | Determines config shape |
| **Bot token** | Yes | Telegram: `123456:ABCdef` / Slack: bot + app tokens | |
| **Port** | Yes | `18798` | Check for conflicts against existing instances |
| **allowFrom** | No | `["holmes3488"]` | Defaults to template value; Slack can use `["*"]` |
| **Bash access** | No | `true`/`false` | Default: blocked (project bot). Only enable for trusted/personal bots |
| **maxTurns** | No | `50` | Default: 50. Personal bots may want 200 |
| **timeout** | No | `300` | Default: 300s. Increase for heavy workloads |

### Port Conflict Check

Before proceeding, verify the chosen port is not already in use:

```bash
# List all ports currently configured
grep -r '"port"' /home/holmes/comapnio-projects/companio-*/config.json | grep -oP '\d{5}'
```

If there's a conflict, suggest the next available port.

## Step 2: Detect OS and Validate Prerequisites

```bash
OS=$(uname -s)  # "Linux" or "Darwin"
echo "OS: $OS"
```

### Linux prerequisites
- systemd user session: `systemctl --user status` should work
- linger enabled: `loginctl show-user holmes -p Linger` (should be `yes`)
- companio installed: `/home/holmes/compan.io/.venv/bin/companio --version`

### macOS prerequisites
- PM2 installed: `pm2 --version` (if missing, suggest `npm install -g pm2`)
- companio installed: check the venv path (may differ from Linux — ask user for path if `compan.io` not at expected location)
- PM2 startup configured: `pm2 startup` (only needs to run once per machine)

## Step 3: Create Instance Directory

Run the template setup script:

```bash
cd /home/holmes/comapnio-projects/_template
./setup.sh companio-{name} "{bot-token}" {port}
```

This creates the directory structure at `/home/holmes/comapnio-projects/companio-{name}/` with:
- `config.json` — gateway/channel/claude settings
- `workspace/` — memory, skills, companio.db
- `project/` — CLAUDE.md (system prompt), .mcp.json
- `cron/` — jobs.json
- `repos/` — for git-cloned projects

### Slack Instance: Post-Setup Config

The template defaults to Telegram. For Slack instances, edit `config.json` after setup:

```json
{
  "channels": {
    "sendProgress": true,
    "telegram": { "enabled": false },
    "slack": {
      "enabled": true,
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "allowFrom": ["*"],
      "respondInThread": true
    }
  }
}
```

Use Edit tool to replace the telegram section with slack config.

### Custom Settings

Apply any non-default settings the user requested:
- **Bash access**: remove `"Bash"` from `roles.default.disallowedTools` in config.json
- **maxTurns / timeout**: update `claude.maxTurns` and `claude.timeout`
- **allowFrom**: update the appropriate channel's `allowFrom` array

## Step 4: Register with Process Manager

### Linux — systemd

Create the unit file:

```bash
cat > ~/.config/systemd/user/companio-{name}.service << 'UNIT'
[Unit]
Description=Companio Gateway - {name}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/home/holmes/compan.io/.venv/bin/companio gateway --config /home/holmes/comapnio-projects/companio-{name}/config.json
Restart=always
RestartSec=5
KillMode=process
Environment=HOME=/home/holmes
Environment=TMPDIR=/tmp
Environment=PATH=/home/holmes/compan.io/.venv/bin:/home/holmes/.local/bin:/home/holmes/.npm-global/bin:/home/holmes/bin:/home/holmes/.volta/bin:/home/holmes/.asdf/shims:/home/holmes/.bun/bin:/home/holmes/.nvm/current/bin:/home/holmes/.fnm/current/bin:/home/holmes/.local/share/pnpm:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=/home/holmes/comapnio-projects/companio-{name}

[Install]
WantedBy=default.target
UNIT
```

Then activate:

```bash
systemctl --user daemon-reload
systemctl --user enable --now companio-{name}
```

### macOS — PM2

```bash
# Get the companio venv python path (adjust if different on this Mac)
COMPANIO_BIN="/path/to/compan.io/.venv/bin/companio"
CONFIG_PATH="/path/to/comapnio-projects/companio-{name}/config.json"
CWD="/path/to/comapnio-projects/companio-{name}"

pm2 start "$COMPANIO_BIN" \
  --name "companio-{name}" \
  --interpreter python3 \
  -- gateway --config "$CONFIG_PATH"

# Tune PM2 settings for companio
pm2 set companio-{name}:kill_timeout 30000
pm2 set companio-{name}:treekill false
pm2 set companio-{name}:restart_delay 5000

pm2 save
```

If this is the first instance on the Mac, also run:
```bash
pm2 startup launchd
pm2 save
```

## Step 5: Verify

Wait a few seconds for the process to start, then check:

### Linux
```bash
sleep 3
systemctl --user status companio-{name} --no-pager
journalctl --user -u companio-{name} -n 20 --no-pager
```

### macOS
```bash
sleep 3
pm2 status companio-{name}
pm2 logs companio-{name} --lines 20 --nostream
```

Look for:
- Service is `active (running)` / `online`
- No crash loops (check restart count)
- Gateway started log line
- Channel connected (Telegram polling started / Slack socket connected)

## Step 6: Report Summary

Present results to the user:

```
Instance created: companio-{name}
  Channel:  telegram / slack
  Port:     {port}
  Status:   running
  Config:   /home/holmes/comapnio-projects/companio-{name}/config.json
  Logs:     journalctl --user -u companio-{name} -f  (Linux)
            pm2 logs companio-{name}                  (macOS)

Next steps:
  - Edit project/CLAUDE.md to set the bot's system prompt
  - Add MCP servers in project/.mcp.json if needed
  - Clone repos into repos/ if this bot manages a project
  - Add users to allowFrom in config.json
```

## Updating companio-manage

After creating a new instance, remind the user to update the instance registry in the
companio-manage skill at `.claude/skills/companio-manage/SKILL.md` so that the management
skill knows about the new instance.

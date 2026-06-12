# Tauri Desktop App Onboarding Flow

## Goal

When a user installs and launches the compan.io desktop app for the first time, they should be guided through dependency installation, Claude CLI authentication, and channel configuration — all from within the app. No prior terminal setup required.

## Constraints

- Python changes: zero. All onboarding logic lives in Rust + frontend.
- Onboarding re-triggers automatically on every app launch if any dependency is missing.
- Terminal-based steps (install commands, Claude CLI auth) use system terminal — the app opens it for the user.
- First-run ends at Settings tab for channel config; subsequent launches go to Dashboard.

---

## Preflight Check System

### Checks (executed sequentially on app launch)

| Check | Command | Pass Condition |
|-------|---------|----------------|
| Python 3.11+ | `python3 --version` (macOS) / `python --version` (Windows) | stdout contains version >= 3.11 |
| companio CLI | `companio --help` | exit code 0 |
| Claude CLI installed | `claude --version` | exit code 0 |

Each check returns: `{ name, status: "pass" | "fail" | "warn", detail }`.

"warn" is used for Claude CLI when installed but unauthenticated (detected by running `claude --version` succeeding but a separate auth check failing — for MVP, we skip auth detection and just check installation; users will see the error when gateway starts if unauthed).

### Rust Implementation

Add to `platform.rs`:

```rust
struct PreflightResult {
    python: CheckResult,
    companio: CheckResult,
    claude_cli: CheckResult,
}

struct CheckResult {
    status: String,   // "pass", "fail", "warn"
    version: String,  // detected version or ""
    detail: String,   // error message or install command
}
```

Add Tauri command `preflight_check` to `main.rs` that calls `platform::run_preflight()` and returns `PreflightResult` as JSON.

Add `open_terminal_with_command(cmd: String)` to `platform.rs`:
- macOS: `open -a Terminal.app <script>` or `osascript -e 'tell app "Terminal" to do script "<cmd>"'`
- Windows: `cmd /k <cmd>`

### Install Commands (per platform)

| Dependency | macOS | Windows |
|-----------|-------|---------|
| Python 3.11+ | `brew install python@3.11` | `winget install Python.Python.3.11` |
| companio | `pip install companio` | `pip install companio` |
| Claude CLI | `npm install -g @anthropic-ai/claude-code` | `npm install -g @anthropic-ai/claude-code` |

---

## Setup View (UI)

### Layout

A new `view-setup` section in `index.html` that overlays the normal tab interface. When active, the tab bar is hidden and Setup fills the window.

```
+-------------------------------------------+
| [icon] compan.io Setup                    |
+-------------------------------------------+
|                                           |
|  Step 1: Dependencies                     |
|                                           |
|  [*] Python 3.11+         v3.11.15  OK   |
|  [!] companio              Missing        |
|      pip install companio                 |
|      [Copy]  [Open Terminal]              |
|                                           |
|  [!] Claude CLI            Missing        |
|      npm install -g @anthropic-ai/...     |
|      [Copy]  [Open Terminal]              |
|                                           |
|           [Re-check Dependencies]         |
|                                           |
+-------------------------------------------+
```

When all checks pass, the view transitions:
- If `~/.companio/config.json` does not exist or has no enabled channels: switch to Settings tab.
- If config exists with enabled channels: switch to Dashboard.

### Interaction Details

- **Copy button**: copies the install command to clipboard via `navigator.clipboard.writeText()`.
- **Open Terminal button**: calls Tauri command `open_terminal_with_command` with the appropriate install command.
- **Re-check button**: calls `preflight_check` again and updates the status indicators.
- **Auto-transition**: when all checks pass after a re-check, show a brief "All set!" message (1 second) then transition.

### Status Indicators

- Pass: green dot + "OK" + version number
- Fail: red dot + "Missing" + install command + action buttons
- Warn: yellow dot + warning message + action button

---

## App Launch Flow

```
App starts
  |
  v
preflight_check()
  |
  +-- Any check failed? --> Show Setup view
  |                           |
  |                           v
  |                     User installs, clicks [Re-check]
  |                           |
  |                           +-- Still failing? --> Stay on Setup
  |                           |
  |                           +-- All pass --> (continue below)
  |
  +-- All checks pass
        |
        v
  config.json exists with enabled channel?
        |
        +-- No  --> Show Settings tab (first-run config)
        |
        +-- Yes --> Show Dashboard
                      |
                      v
                    Auto-start gateway? (if desktop-state.json says so)
```

---

## File Changes

| File | Change |
|------|--------|
| `desktop/src-tauri/src/platform.rs` | Add `run_preflight()`, `check_python()`, `check_companio()`, `check_claude_cli()`, `open_terminal_with_command()` |
| `desktop/src-tauri/src/main.rs` | Add `preflight_check` and `open_terminal` Tauri commands |
| `desktop/ui/index.html` | Add `view-setup` section before `view-dashboard` |
| `desktop/ui/style.css` | Add setup view styles (status dots, action buttons, overlay) |
| `desktop/ui/app.js` | Add `init()` function that runs preflight on load, setup view logic, re-check handler |
| Python code | No changes |

Estimated scope: ~200 lines Rust, ~80 lines HTML, ~60 lines CSS, ~80 lines JS.

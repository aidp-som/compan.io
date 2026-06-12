# Tauri Onboarding Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dependency-checking Setup screen to the Tauri desktop app that guides first-time users through installing Python, companio, and Claude CLI — so the app is usable immediately after install without prior terminal setup.

**Architecture:** On every app launch, `platform.rs` runs three shell commands to detect Python 3.11+, companio, and Claude CLI. If any check fails, a full-screen Setup overlay replaces the normal tab UI. Users get copy-to-clipboard install commands and an "Open Terminal" button. A "Re-check" button re-runs the checks; when all pass, the overlay dismisses and the app routes to Settings (first run) or Dashboard (returning user).

**Tech Stack:** Rust (Tauri commands, process spawning), vanilla HTML/CSS/JS (no framework)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `desktop/src-tauri/src/platform.rs` | Modify | Add `run_preflight()`, `check_python()`, `check_companio()`, `check_claude_cli()`, `open_terminal_with_command()` |
| `desktop/src-tauri/src/main.rs` | Modify | Add `preflight_check` and `open_terminal` Tauri commands, register them |
| `desktop/ui/index.html` | Modify | Add `view-setup` section before `view-dashboard` |
| `desktop/ui/style.css` | Modify | Add setup overlay, check-row, and action button styles |
| `desktop/ui/app.js` | Modify | Add `runPreflight()` init logic, setup view handlers, re-check, auto-transition |

---

## Task 1: Preflight Check Functions in Rust

**Files:**
- Modify: `desktop/src-tauri/src/platform.rs`

- [ ] **Step 1: Add the CheckResult and PreflightResult structs**

Add to the top of `desktop/src-tauri/src/platform.rs`, after the existing `use` statement:

```rust
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CheckResult {
    pub name: String,
    pub status: String,
    pub version: String,
    pub detail: String,
    pub install_cmd: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PreflightResult {
    pub python: CheckResult,
    pub companio: CheckResult,
    pub claude_cli: CheckResult,
    pub all_pass: bool,
}
```

- [ ] **Step 2: Implement `check_python()`**

Add to `platform.rs`:

```rust
fn check_python() -> CheckResult {
    let cmd = if cfg!(windows) { "python" } else { "python3" };
    match Command::new(cmd).arg("--version").output() {
        Ok(output) if output.status.success() => {
            let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();
            // "Python 3.11.15" -> extract version
            let version = raw.strip_prefix("Python ").unwrap_or(&raw).to_string();
            let parts: Vec<&str> = version.split('.').collect();
            let major: u32 = parts.first().and_then(|v| v.parse().ok()).unwrap_or(0);
            let minor: u32 = parts.get(1).and_then(|v| v.parse().ok()).unwrap_or(0);
            if major >= 3 && minor >= 11 {
                CheckResult {
                    name: "Python 3.11+".into(),
                    status: "pass".into(),
                    version,
                    detail: String::new(),
                    install_cmd: String::new(),
                }
            } else {
                CheckResult {
                    name: "Python 3.11+".into(),
                    status: "fail".into(),
                    version: version.clone(),
                    detail: format!("Found Python {} but 3.11+ is required", version),
                    install_cmd: if cfg!(target_os = "macos") {
                        "brew install python@3.11".into()
                    } else {
                        "winget install Python.Python.3.11".into()
                    },
                }
            }
        }
        _ => CheckResult {
            name: "Python 3.11+".into(),
            status: "fail".into(),
            version: String::new(),
            detail: "Python not found on PATH".into(),
            install_cmd: if cfg!(target_os = "macos") {
                "brew install python@3.11".into()
            } else {
                "winget install Python.Python.3.11".into()
            },
        },
    }
}
```

- [ ] **Step 3: Implement `check_companio()`**

Add to `platform.rs`:

```rust
fn check_companio() -> CheckResult {
    match Command::new("companio").arg("--help").output() {
        Ok(output) if output.status.success() => CheckResult {
            name: "companio".into(),
            status: "pass".into(),
            version: "installed".into(),
            detail: String::new(),
            install_cmd: String::new(),
        },
        _ => CheckResult {
            name: "companio".into(),
            status: "fail".into(),
            version: String::new(),
            detail: "companio CLI not found on PATH".into(),
            install_cmd: "pip install companio".into(),
        },
    }
}
```

- [ ] **Step 4: Implement `check_claude_cli()`**

Add to `platform.rs`:

```rust
fn check_claude_cli() -> CheckResult {
    match Command::new("claude").arg("--version").output() {
        Ok(output) if output.status.success() => {
            let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
            CheckResult {
                name: "Claude CLI".into(),
                status: "pass".into(),
                version,
                detail: String::new(),
                install_cmd: String::new(),
            }
        }
        _ => CheckResult {
            name: "Claude CLI".into(),
            status: "fail".into(),
            version: String::new(),
            detail: "Claude CLI not found on PATH".into(),
            install_cmd: "npm install -g @anthropic-ai/claude-code".into(),
        },
    }
}
```

- [ ] **Step 5: Implement `run_preflight()` public function**

Add to `platform.rs`:

```rust
pub fn run_preflight() -> PreflightResult {
    let python = check_python();
    let companio = check_companio();
    let claude_cli = check_claude_cli();
    let all_pass =
        python.status == "pass" && companio.status == "pass" && claude_cli.status == "pass";
    PreflightResult {
        python,
        companio,
        claude_cli,
        all_pass,
    }
}
```

- [ ] **Step 6: Implement `open_terminal_with_command()`**

Add to `platform.rs`:

```rust
pub fn open_terminal_with_command(cmd: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        Command::new("osascript")
            .arg("-e")
            .arg(format!(
                "tell application \"Terminal\"\n  activate\n  do script \"{}\"\nend tell",
                cmd.replace('\\', "\\\\").replace('"', "\\\"")
            ))
            .spawn()
            .map_err(|e| format!("Failed to open Terminal: {}", e))?;
    }

    #[cfg(target_os = "windows")]
    {
        Command::new("cmd")
            .args(["/c", "start", "cmd", "/k", cmd])
            .spawn()
            .map_err(|e| format!("Failed to open terminal: {}", e))?;
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        return Err("Unsupported platform".into());
    }

    Ok(())
}
```

- [ ] **Step 7: Verify compilation**

Run from project root:

```bash
cd desktop/src-tauri && cargo check 2>&1 | tail -10
```

Expected: Compiles with possibly unused function warnings (the public functions aren't called from main.rs yet).

- [ ] **Step 8: Commit**

```bash
git add desktop/src-tauri/src/platform.rs
git commit -m "feat(desktop): add preflight dependency checks and terminal opener"
```

---

## Task 2: Tauri Commands for Preflight and Terminal

**Files:**
- Modify: `desktop/src-tauri/src/main.rs:111-131`

- [ ] **Step 1: Add `preflight_check` command**

Add after the `get_logs` command (after line 109 of `main.rs`):

```rust
#[tauri::command]
async fn preflight_check() -> Result<platform::PreflightResult, String> {
    Ok(platform::run_preflight())
}

#[tauri::command]
async fn open_terminal(cmd: String) -> Result<(), String> {
    platform::open_terminal_with_command(&cmd)
}

#[tauri::command]
async fn has_config() -> Result<bool, String> {
    let path = platform::default_config_path();
    if !path.exists() {
        return Ok(false);
    }
    let content = std::fs::read_to_string(&path).unwrap_or_default();
    let config: serde_json::Value = serde_json::from_str(&content).unwrap_or_default();
    let tg_enabled = config
        .pointer("/channels/telegram/enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let slack_enabled = config
        .pointer("/channels/slack/enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    Ok(tg_enabled || slack_enabled)
}
```

- [ ] **Step 2: Register the new commands in the invoke handler**

Modify the `invoke_handler` block (around line 122-131) to include the new commands:

```rust
        .invoke_handler(tauri::generate_handler![
            cmd_start_gateway,
            cmd_stop_gateway,
            load_config,
            save_config,
            read_workspace_file,
            open_config_file,
            open_workspace,
            get_logs,
            preflight_check,
            open_terminal,
            has_config,
        ])
```

- [ ] **Step 3: Verify compilation**

```bash
cd desktop/src-tauri && cargo check 2>&1 | tail -10
```

Expected: Compiles successfully. The `find_companio` unused warning may disappear since `platform` module is now more fully used.

- [ ] **Step 4: Commit**

```bash
git add desktop/src-tauri/src/main.rs
git commit -m "feat(desktop): add preflight_check, open_terminal, has_config Tauri commands"
```

---

## Task 3: Setup View HTML

**Files:**
- Modify: `desktop/ui/index.html:9-15` (before `<main>`)

- [ ] **Step 1: Add the setup overlay section**

In `desktop/ui/index.html`, add a new section BEFORE the `<nav id="tabs">` element (before line 10). The setup view sits outside the normal tab flow:

```html
  <section id="view-setup" class="setup-overlay" style="display: none;">
    <div class="setup-content">
      <h1 class="setup-title">compan.io Setup</h1>
      <p class="setup-subtitle">Checking dependencies...</p>

      <div id="setup-checks">
        <div class="check-row" id="check-python">
          <span class="check-dot"></span>
          <div class="check-info">
            <span class="check-name">Python 3.11+</span>
            <span class="check-detail"></span>
          </div>
          <div class="check-actions" style="display: none;">
            <code class="check-cmd"></code>
            <button class="btn-copy" title="Copy command">Copy</button>
            <button class="btn-terminal" title="Open Terminal">Open Terminal</button>
          </div>
        </div>

        <div class="check-row" id="check-companio">
          <span class="check-dot"></span>
          <div class="check-info">
            <span class="check-name">companio</span>
            <span class="check-detail"></span>
          </div>
          <div class="check-actions" style="display: none;">
            <code class="check-cmd"></code>
            <button class="btn-copy" title="Copy command">Copy</button>
            <button class="btn-terminal" title="Open Terminal">Open Terminal</button>
          </div>
        </div>

        <div class="check-row" id="check-claude">
          <span class="check-dot"></span>
          <div class="check-info">
            <span class="check-name">Claude CLI</span>
            <span class="check-detail"></span>
          </div>
          <div class="check-actions" style="display: none;">
            <code class="check-cmd"></code>
            <button class="btn-copy" title="Copy command">Copy</button>
            <button class="btn-terminal" title="Open Terminal">Open Terminal</button>
          </div>
        </div>
      </div>

      <button id="btn-recheck" class="btn-recheck">Re-check Dependencies</button>
    </div>
  </section>
```

- [ ] **Step 2: Commit**

```bash
git add desktop/ui/index.html
git commit -m "feat(desktop): add setup overlay HTML for onboarding flow"
```

---

## Task 4: Setup View CSS

**Files:**
- Modify: `desktop/ui/style.css` (append at end, after line 445)

- [ ] **Step 1: Add setup overlay styles**

Append to the end of `desktop/ui/style.css`:

```css
/* === Setup Overlay === */
.setup-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.setup-content {
  width: 100%;
  max-width: 440px;
}

.setup-title {
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 4px;
}

.setup-subtitle {
  font-size: 14px;
  color: var(--muted);
  margin-bottom: 24px;
}

/* Check rows */
.check-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
}

.check-row:last-child {
  border-bottom: none;
}

.check-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--muted);
  flex-shrink: 0;
  margin-top: 4px;
}

.check-dot.pass {
  background: var(--success);
}

.check-dot.fail {
  background: var(--error);
}

.check-dot.checking {
  background: var(--warning);
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.check-info {
  flex: 1;
  min-width: 0;
}

.check-name {
  font-weight: 600;
  font-size: 14px;
  display: block;
}

.check-detail {
  font-size: 12px;
  color: var(--muted);
}

.check-detail.version {
  color: var(--success);
}

.check-detail.error {
  color: var(--error);
}

/* Check action row */
.check-actions {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  padding-left: 20px;
}

.check-cmd {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 12px;
  background: var(--bg-section);
  padding: 6px 10px;
  border-radius: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.btn-copy,
.btn-terminal {
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-section);
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
  font-family: var(--font-sans);
  white-space: nowrap;
  transition: background 0.2s;
}

.btn-copy:hover,
.btn-terminal:hover {
  background: var(--border);
}

.btn-recheck {
  display: block;
  width: 100%;
  margin-top: 20px;
  padding: 10px;
  border: none;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  font-family: var(--font-sans);
  transition: opacity 0.2s;
}

.btn-recheck:hover {
  opacity: 0.85;
}

.btn-recheck:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
```

- [ ] **Step 2: Commit**

```bash
git add desktop/ui/style.css
git commit -m "feat(desktop): add setup overlay styles for onboarding"
```

---

## Task 5: Setup Logic in JavaScript

**Files:**
- Modify: `desktop/ui/app.js:200-201` (replace init section at bottom)

- [ ] **Step 1: Add the setup/preflight logic**

Replace the last two lines of `desktop/ui/app.js` (lines 200-201: `// Init` and `loadConfig();`) with:

```javascript
// === Setup / Onboarding ===

const setupOverlay = document.getElementById('view-setup');
const setupSubtitle = document.querySelector('.setup-subtitle');
const btnRecheck = document.getElementById('btn-recheck');

function updateCheckRow(id, result) {
  const row = document.getElementById(id);
  const dot = row.querySelector('.check-dot');
  const detail = row.querySelector('.check-detail');
  const actions = row.querySelector('.check-actions');

  dot.className = `check-dot ${result.status}`;

  if (result.status === 'pass') {
    detail.textContent = result.version || 'OK';
    detail.className = 'check-detail version';
    actions.style.display = 'none';
  } else {
    detail.textContent = result.detail;
    detail.className = 'check-detail error';
    if (result.install_cmd) {
      actions.style.display = 'flex';
      actions.querySelector('.check-cmd').textContent = result.install_cmd;
    }
  }
}

async function runPreflight() {
  setupSubtitle.textContent = 'Checking dependencies...';
  btnRecheck.disabled = true;

  // Set all dots to checking state
  document.querySelectorAll('.check-dot').forEach(d => d.className = 'check-dot checking');
  document.querySelectorAll('.check-detail').forEach(d => { d.textContent = ''; d.className = 'check-detail'; });
  document.querySelectorAll('.check-actions').forEach(a => a.style.display = 'none');

  try {
    const result = await invoke('preflight_check');

    updateCheckRow('check-python', result.python);
    updateCheckRow('check-companio', result.companio);
    updateCheckRow('check-claude', result.claude_cli);

    if (result.all_pass) {
      setupSubtitle.textContent = 'All dependencies found!';
      setTimeout(async () => {
        setupOverlay.style.display = 'none';
        const configured = await invoke('has_config').catch(() => false);
        if (configured) {
          // Returning user — go to Dashboard
          loadConfig();
        } else {
          // First run — go to Settings tab
          document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
          document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
          document.querySelector('[data-view="settings"]').classList.add('active');
          document.getElementById('view-settings').classList.add('active');
          loadConfig();
        }
      }, 800);
    } else {
      const failCount = [result.python, result.companio, result.claude_cli]
        .filter(c => c.status !== 'pass').length;
      setupSubtitle.textContent = `${failCount} missing — install and re-check`;
    }
  } catch (e) {
    setupSubtitle.textContent = 'Check failed — are you running inside the Tauri app?';
    console.error('Preflight failed:', e);
  }

  btnRecheck.disabled = false;
}

// Re-check button
btnRecheck.addEventListener('click', runPreflight);

// Copy buttons
document.querySelectorAll('.btn-copy').forEach(btn => {
  btn.addEventListener('click', () => {
    const cmd = btn.parentElement.querySelector('.check-cmd').textContent;
    navigator.clipboard.writeText(cmd).then(() => {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    });
  });
});

// Open Terminal buttons
document.querySelectorAll('.btn-terminal').forEach(btn => {
  btn.addEventListener('click', async () => {
    const cmd = btn.parentElement.querySelector('.check-cmd').textContent;
    try {
      await invoke('open_terminal', { cmd });
    } catch (e) {
      console.error('Failed to open terminal:', e);
    }
  });
});

// === Init ===
async function init() {
  // Always show setup overlay and run preflight first
  setupOverlay.style.display = 'flex';
  await runPreflight();
}

init();
```

- [ ] **Step 2: Verify compilation of the full Tauri app**

```bash
cd desktop/src-tauri && cargo check 2>&1 | tail -10
```

Expected: Compiles. (JS changes don't affect Rust compilation, but this verifies nothing else broke.)

- [ ] **Step 3: Commit**

```bash
git add desktop/ui/app.js
git commit -m "feat(desktop): add onboarding preflight check logic with setup UI"
```

---

## Task 6: Integration Test

- [ ] **Step 1: Run full Rust compilation**

```bash
cd desktop/src-tauri && cargo build 2>&1 | tail -10
```

Expected: Build succeeds.

- [ ] **Step 2: Manual test — launch the app**

```bash
cd desktop/src-tauri && cargo run
```

Expected behavior:
1. Window opens with Setup overlay
2. Three check rows appear with "checking" pulse animation
3. Each row resolves to pass (green) or fail (red)
4. If all pass: overlay dismisses after ~1 second, lands on Dashboard or Settings
5. If any fail: install command shown with Copy and Open Terminal buttons
6. Click Copy — command copied to clipboard
7. Click Open Terminal — system terminal opens with the command
8. Click Re-check — checks run again
9. Once all pass — overlay auto-dismisses

- [ ] **Step 3: Test edge case — all dependencies present**

If Python, companio, and Claude CLI are all installed:
1. App should show Setup briefly (~1s with green dots) then auto-dismiss
2. If config.json exists with an enabled channel → Dashboard
3. If no config.json → Settings tab

- [ ] **Step 4: Commit if any fixes were needed**

```bash
git add -A && git commit -m "fix(desktop): onboarding integration fixes"
```

(Skip if no fixes needed.)

---

## Summary

| Task | Description | Est. Time |
|------|-------------|-----------|
| 1 | Preflight check functions in Rust | 20 min |
| 2 | Tauri commands for preflight + terminal | 10 min |
| 3 | Setup view HTML | 10 min |
| 4 | Setup view CSS | 10 min |
| 5 | Setup logic in JavaScript | 15 min |
| 6 | Integration test | 15 min |
| **Total** | | **~80 min** |

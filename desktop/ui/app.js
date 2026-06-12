// Tauri API - graceful fallback if not in Tauri context
const invoke = window.__TAURI__?.core?.invoke || ((...args) => {
  console.log('invoke (stub):', ...args);
  return Promise.reject('Not in Tauri context');
});
const listen = window.__TAURI__?.event?.listen || ((...args) => {
  console.log('listen (stub):', ...args);
  return Promise.resolve(() => {});
});

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
  btnToggle.disabled = true;
  try {
    if (gatewayRunning) {
      btnToggle.textContent = 'Stopping...';
      await invoke('cmd_stop_gateway');
    } else {
      btnToggle.textContent = 'Starting...';
      await invoke('cmd_start_gateway');
    }
  } catch (e) {
    console.error('Gateway toggle failed:', e);
    statusText.textContent = `Error: ${e}`;
    statusBadge.className = 'status error';
    btnToggle.textContent = gatewayRunning ? 'Stop' : 'Start';
    btnToggle.disabled = false;
  }
});

listen('gateway-status', (event) => {
  const { status, channels, cron } = event.payload;
  gatewayRunning = status === 'running';

  statusBadge.className = `status ${status}`;
  statusText.textContent = status === 'running' ? 'Gateway Running'
    : status === 'error' ? 'Gateway Error'
    : 'Gateway Stopped';
  btnToggle.textContent = gatewayRunning ? 'Stop' : 'Start';
  btnToggle.disabled = false;

  if (gatewayRunning && !startTime) startTime = Date.now();
  if (!gatewayRunning) startTime = null;

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

  if (cron) {
    document.getElementById('cron-info').textContent = `${cron.jobs || 0} jobs`;
  }
});

// Uptime ticker
setInterval(() => {
  if (startTime) {
    const secs = Math.floor((Date.now() - startTime) / 1000);
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    document.getElementById('uptime').textContent = h > 0 ? `${h}h ${m}m` : `${m}m`;
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
    if ((LOG_LEVELS[l.level] || 0) < minLevel) return false;
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
    alert('Settings saved. Restart gateway for changes to take effect.');
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
      document.getElementById('tg-enabled').checked = config.channels.telegram.enabled || false;
      document.getElementById('tg-token').value = config.channels.telegram.token || '';
      document.getElementById('tg-allow').value = (config.channels.telegram.allowFrom || []).join(', ');
    }
    if (config.channels?.slack) {
      document.getElementById('slack-enabled').checked = config.channels.slack.enabled || false;
      document.getElementById('slack-bot-token').value = config.channels.slack.botToken || '';
      document.getElementById('slack-app-token').value = config.channels.slack.appToken || '';
      document.getElementById('slack-allow').value = (config.channels.slack.allowFrom || []).join(', ');
    }
    if (config.agents?.defaults) {
      document.getElementById('bot-name').value = config.agents.defaults.botName || 'companio';
      document.getElementById('memory-window').value = config.agents.defaults.memoryWindow || 200;
    }
    if (config.desktop) {
      document.getElementById('path-python').value = config.desktop.pythonPath || '';
      document.getElementById('path-companio').value = config.desktop.companioPath || '';
      document.getElementById('path-claude').value = config.desktop.claudePath || '';
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
        memoryWindow: parseInt(document.getElementById('memory-window').value) || 200,
      },
    },
    desktop: {
      pythonPath: document.getElementById('path-python').value || undefined,
      companioPath: document.getElementById('path-companio').value || undefined,
      claudePath: document.getElementById('path-claude').value || undefined,
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

    const btnInstallCompanio = document.getElementById('btn-install-companio');
    if (result.python.status !== 'pass') {
      btnInstallCompanio.disabled = true;
      btnInstallCompanio.textContent = 'Install Python first';
    } else if (result.companio.status !== 'pass') {
      btnInstallCompanio.disabled = false;
      btnInstallCompanio.textContent = 'Install';
    }

    if (result.all_pass) {
      setupSubtitle.textContent = 'All dependencies found!';
      setTimeout(async () => {
        setupOverlay.style.display = 'none';
        const configured = await invoke('has_config').catch(() => false);
        if (configured) {
          loadConfig();
        } else {
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

// Install companio button
document.getElementById('btn-install-companio').addEventListener('click', async () => {
  const btn = document.getElementById('btn-install-companio');
  const detail = document.querySelector('#check-companio .check-detail');
  btn.disabled = true;
  btn.textContent = 'Installing...';
  detail.textContent = 'Installing companio...';
  detail.className = 'check-detail';
  try {
    const msg = await invoke('install_companio');
    detail.textContent = msg;
    detail.className = 'check-detail version';
    btn.textContent = 'Installed!';
    setTimeout(runPreflight, 500);
  } catch (e) {
    detail.textContent = String(e);
    detail.className = 'check-detail error';
    btn.textContent = 'Install';
    btn.disabled = false;
  }
});

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
  setupOverlay.style.display = 'flex';
  await runPreflight();
}

init();

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
    resolved_path: String,
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
        .env("PATH", &resolved_path)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Failed to spawn gateway: {}", e))?;

    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();

    *state.child.lock().unwrap() = Some(child);

    let app_clone = app.clone();
    let state_clone = state.clone();
    tokio::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            handle_ipc_event(&app_clone, &state_clone, &line);
        }
        let mut status = state_clone.status.lock().unwrap();
        if *status == GatewayStatus::Running || *status == GatewayStatus::Starting {
            *status = GatewayStatus::Error;
        }
        drop(status);
        emit_status(&app_clone, &state_clone);
    });

    let app_clone2 = app.clone();
    let log_buffer = state.log_buffer.clone();
    tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let log_line = if let Some(parsed) = parse_loguru_json(&line) {
                parsed
            } else {
                LogLine {
                    level: "INFO".into(),
                    message: line,
                    timestamp: String::new(),
                }
            };
            log_buffer.push(log_line.clone());
            let _ = app_clone2.emit("log-line", &log_line);
        }
    });

    Ok(())
}

pub async fn stop_gateway(app: AppHandle, state: Arc<GatewayState>) -> Result<(), String> {
    let mut child = {
        let mut child_guard = state.child.lock().unwrap();
        child_guard.take().ok_or("Gateway is not running")?
    };
    let _ = child.kill().await;
    *state.status.lock().unwrap() = GatewayStatus::Stopped;
    emit_status(&app, &state);
    Ok(())
}

fn handle_ipc_event(app: &AppHandle, state: &Arc<GatewayState>, line: &str) {
    if let Ok(event) = serde_json::from_str::<serde_json::Value>(line) {
        let event_type = event.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let data = event
            .get("data")
            .cloned()
            .unwrap_or(serde_json::Value::Null);

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
                let _ = app.emit("gateway-status", &payload);
            }
            "error" => {
                let fatal = data
                    .get("fatal")
                    .and_then(|f| f.as_bool())
                    .unwrap_or(false);
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
    let _ = app.emit("gateway-status", &payload);
}

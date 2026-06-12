#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod gateway;
mod logs;
mod platform;

use gateway::{start_gateway, stop_gateway, GatewayState};
use std::sync::Arc;
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Manager,
};

#[tauri::command]
async fn cmd_start_gateway(
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<GatewayState>>,
) -> Result<(), String> {
    let config_path = platform::default_config_path()
        .to_string_lossy()
        .to_string();
    let resolved_path = platform::resolve_path_with_overrides();
    start_gateway(app, state.inner().clone(), config_path, resolved_path).await
}

#[tauri::command]
async fn cmd_stop_gateway(
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<GatewayState>>,
) -> Result<(), String> {
    stop_gateway(app, state.inner().clone()).await
}

#[tauri::command]
async fn load_config() -> Result<serde_json::Value, String> {
    let path = platform::default_config_path();
    if !path.exists() {
        return Ok(serde_json::json!({}));
    }
    let content =
        std::fs::read_to_string(&path).map_err(|e| format!("Failed to read config: {}", e))?;
    serde_json::from_str(&content).map_err(|e| format!("Failed to parse config: {}", e))
}

#[tauri::command]
async fn save_config(config: serde_json::Value) -> Result<(), String> {
    let path = platform::default_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create config dir: {}", e))?;
    }
    let mut existing: serde_json::Value = if path.exists() {
        let content = std::fs::read_to_string(&path).unwrap_or_default();
        serde_json::from_str(&content).unwrap_or(serde_json::json!({}))
    } else {
        serde_json::json!({})
    };
    merge_json(&mut existing, &config);
    let content = serde_json::to_string_pretty(&existing)
        .map_err(|e| format!("Failed to serialize config: {}", e))?;
    std::fs::write(&path, content).map_err(|e| format!("Failed to write config: {}", e))
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

#[tauri::command]
async fn preflight_check() -> Result<platform::PreflightResult, String> {
    Ok(platform::run_preflight())
}

#[tauri::command]
async fn open_terminal(cmd: String) -> Result<(), String> {
    platform::open_terminal_with_command(&cmd)
}

#[tauri::command]
async fn install_companio(app: tauri::AppHandle) -> Result<String, String> {
    let resolved_path = platform::resolve_user_path();

    // Find the bundled wheel in resources
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("Failed to get resource dir: {}", e))?;
    let resources = resource_dir.join("resources");

    // Find any companio*.whl file
    let whl_path = std::fs::read_dir(&resources)
        .map_err(|e| format!("Failed to read resources dir {:?}: {}", resources, e))?
        .flatten()
        .find(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("companio")
                && entry.file_name().to_string_lossy().ends_with(".whl")
        })
        .map(|e| e.path())
        .ok_or_else(|| "No companio wheel found in resources".to_string())?;

    // Strategy 1: try uv tool install (best — isolated env, no PEP 668 issues)
    let uv_result = std::process::Command::new("uv")
        .args(["tool", "install", "--force",
               "--with", "slack-bolt>=1.18",
               "--with", "aiohttp>=3.9"])
        .arg(&whl_path)
        .env("PATH", &resolved_path)
        .output();
    if let Ok(output) = uv_result {
        if output.status.success() {
            return Ok("Installed via uv tool install".into());
        }
    }

    // Strategy 2: try pipx install
    let pipx_result = std::process::Command::new("pipx")
        .args(["install", "--force"])
        .arg(&whl_path)
        .env("PATH", &resolved_path)
        .output();
    if let Ok(output) = pipx_result {
        if output.status.success() {
            return Ok("Installed via pipx".into());
        }
    }

    // Strategy 3: pip install --user --break-system-packages (fallback)
    #[cfg(target_os = "windows")]
    {
        let result = std::process::Command::new("py")
            .args(["-3", "-m", "pip", "install", "--user", "--break-system-packages"])
            .arg(&whl_path)
            .args(["slack-bolt>=1.18", "aiohttp>=3.9"])
            .env("PATH", &resolved_path)
            .output();
        if let Ok(output) = result {
            if output.status.success() {
                return Ok("Installed via py launcher pip".into());
            }
        }
    }

    let candidates = if cfg!(target_os = "windows") {
        vec!["python", "python3"]
    } else {
        vec!["python3.13", "python3.12", "python3.11", "python3"]
    };

    for python in &candidates {
        let result = std::process::Command::new(python)
            .args(["-m", "pip", "install", "--user", "--break-system-packages"])
            .arg(&whl_path)
            .args(["slack-bolt>=1.18", "aiohttp>=3.9"])
            .env("PATH", &resolved_path)
            .output();

        if let Ok(output) = result {
            if output.status.success() {
                return Ok(format!("Installed via {} pip", python));
            }
        }
    }

    Err(format!(
        "Failed to install companio from {:?}. Try manually: pip install {:?}",
        whl_path, whl_path
    ))
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
            preflight_check,
            open_terminal,
            install_companio,
            has_config,
        ])
        .setup(|app| {
            let quit = MenuItemBuilder::new("Quit compan.io")
                .id("quit")
                .build(app)?;
            let show = MenuItemBuilder::new("Open Dashboard")
                .id("show")
                .build(app)?;
            let menu = MenuBuilder::new(app)
                .item(&show)
                .separator()
                .item(&quit)
                .build()?;

            TrayIconBuilder::new()
                .menu(&menu)
                .on_menu_event(move |app, event| match event.id().as_ref() {
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
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

use std::process::Command;
use serde::Serialize;

pub fn find_companio() -> Option<String> {
    let cmd = if cfg!(windows) { "where" } else { "which" };
    Command::new(cmd)
        .arg("companio")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().lines().next().unwrap_or("").to_string())
}

fn find_in_path(name: &str, path: &str) -> Option<std::path::PathBuf> {
    let names = if cfg!(windows) {
        vec![
            format!("{}.exe", name),
            format!("{}.cmd", name),
            format!("{}.bat", name),
            name.to_string(),
        ]
    } else {
        vec![name.to_string()]
    };
    let sep = if cfg!(windows) { ';' } else { ':' };
    for dir in path.split(sep) {
        let dir = dir.trim();
        if dir.is_empty() { continue; }
        for n in &names {
            let p = std::path::Path::new(dir).join(n);
            if p.exists() {
                return Some(p);
            }
        }
    }
    None
}

pub fn default_config_path() -> std::path::PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".companio")
        .join("config.json")
}

#[cfg(target_os = "windows")]
fn get_python_paths_from_registry() -> Vec<String> {
    let mut paths = Vec::new();
    let versions = ["3.14", "3.13", "3.12", "3.11"];
    let hives = ["HKCU", "HKLM"];
    for hive in &hives {
        for ver in &versions {
            let key = format!(r"{}\SOFTWARE\Python\PythonCore\{}\InstallPath", hive, ver);
            if let Ok(output) = Command::new("reg").args(["query", &key, "/ve"]).output() {
                if output.status.success() {
                    let stdout = String::from_utf8_lossy(&output.stdout);
                    if let Some(line) = stdout.lines().find(|l| l.contains("REG_SZ")) {
                        if let Some(val) = line.split("REG_SZ").last() {
                            let dir = val.trim().trim_end_matches('\\');
                            if !dir.is_empty() {
                                paths.push(dir.to_string());
                                paths.push(format!(r"{}\Scripts", dir));
                            }
                        }
                    }
                }
            }
        }
    }
    paths
}

#[cfg(target_os = "windows")]
fn expand_windows_vars(s: &str) -> String {
    let mut result = s.to_string();
    while let Some(start) = result.find('%') {
        if let Some(end) = result[start + 1..].find('%') {
            let var_name = &result[start + 1..start + 1 + end];
            let replacement = std::env::var(var_name).unwrap_or_default();
            result = format!("{}{}{}", &result[..start], replacement, &result[start + 2 + end..]);
        } else {
            break;
        }
    }
    result
}

pub fn resolve_user_path() -> String {
    #[cfg(target_os = "windows")]
    {
        // GUI apps inherit system env PATH, which may miss user-level entries.
        // Read the user PATH directly from the registry and merge.
        let system_path = std::env::var("PATH").unwrap_or_default();
        let mut path = system_path.clone();

        // Merge user PATH from registry (HKCU\Environment\Path)
        if let Ok(output) = Command::new("reg")
            .args(["query", "HKCU\\Environment", "/v", "Path"])
            .output()
        {
            let stdout = String::from_utf8_lossy(&output.stdout);
            // Output format: "    Path    REG_EXPAND_SZ    <value>"
            if let Some(line) = stdout.lines().find(|l| l.contains("REG_EXPAND_SZ") || l.contains("REG_SZ")) {
                if let Some(val) = line.split("REG_EXPAND_SZ").last()
                    .or_else(|| line.split("REG_SZ").last())
                {
                    let user_path = val.trim();
                    // Expand %VARS% in the registry value
                    let expanded = expand_windows_vars(user_path);
                    for entry in expanded.split(';') {
                        let entry = entry.trim();
                        if !entry.is_empty() && !path.split(';').any(|p| p.eq_ignore_ascii_case(entry)) {
                            path.push(';');
                            path.push_str(entry);
                        }
                    }
                }
            }
        }

        // Also add well-known fallback paths
        let home = dirs::home_dir().unwrap_or_default();
        let extras = [
            home.join(".cargo\\bin").to_string_lossy().to_string(),
            home.join(".local\\bin").to_string_lossy().to_string(),
        ];
        for extra in &extras {
            if !extra.is_empty() && !path.split(';').any(|p| p.eq_ignore_ascii_case(extra)) {
                path.push(';');
                path.push_str(extra);
            }
        }

        // pip install --user puts scripts in %APPDATA%\Python\PythonXX\Scripts
        let appdata = std::env::var("APPDATA").unwrap_or_default();
        if !appdata.is_empty() {
            let py_user_dir = std::path::Path::new(&appdata).join("Python");
            if py_user_dir.is_dir() {
                if let Ok(entries) = std::fs::read_dir(&py_user_dir) {
                    for entry in entries.flatten() {
                        let scripts = entry.path().join("Scripts");
                        if scripts.is_dir() {
                            let s = scripts.to_string_lossy().to_string();
                            if !path.split(';').any(|p| p.eq_ignore_ascii_case(&s)) {
                                path.push(';');
                                path.push_str(&s);
                            }
                        }
                    }
                }
            }
        }

        for py_path in get_python_paths_from_registry() {
            if !py_path.is_empty() && !path.split(';').any(|p| p.eq_ignore_ascii_case(&py_path)) {
                path.push(';');
                path.push_str(&py_path);
            }
        }

        return path;
    }

    #[cfg(not(target_os = "windows"))]
    {
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());

        // Try login shell first to get full PATH
        if let Ok(output) = Command::new(&shell)
            .args(["-l", "-c", "echo $PATH"])
            .output()
        {
            let stdout = String::from_utf8_lossy(&output.stdout);
            // Take only the last line (in case shell prints warnings)
            if let Some(path_line) = stdout.trim().lines().last() {
                if !path_line.is_empty() {
                    let mut path = path_line.to_string();
                    // Append well-known fallback paths
                    let home = dirs::home_dir().unwrap_or_default();
                    for extra in &[
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        &format!("{}/.local/bin", home.display()),
                        &format!("{}/.pyenv/shims", home.display()),
                    ] {
                        if !path.contains(extra) {
                            path.push(':');
                            path.push_str(extra);
                        }
                    }
                    // Add uv-managed Python directories
                    let uv_python_dir = home.join(".local/share/uv/python");
                    if uv_python_dir.is_dir() {
                        if let Ok(entries) = std::fs::read_dir(&uv_python_dir) {
                            for entry in entries.flatten() {
                                let bin_dir = entry.path().join("bin");
                                if bin_dir.is_dir() {
                                    let bin_str = bin_dir.to_string_lossy().to_string();
                                    if !path.contains(&bin_str) {
                                        path.push(':');
                                        path.push_str(&bin_str);
                                    }
                                }
                            }
                        }
                    }
                    return path;
                }
            }
        }

        // Fallback: system PATH + well-known directories
        let mut path = std::env::var("PATH").unwrap_or_default();
        let home = dirs::home_dir().unwrap_or_default();
        for extra in &[
            "/opt/homebrew/bin",
            "/usr/local/bin",
            &format!("{}/.local/bin", home.display()),
            &format!("{}/.pyenv/shims", home.display()),
        ] {
            if !path.contains(extra) {
                path.push(':');
                path.push_str(extra);
            }
        }
        // Add uv-managed Python directories
        let uv_python_dir = home.join(".local/share/uv/python");
        if uv_python_dir.is_dir() {
            if let Ok(entries) = std::fs::read_dir(&uv_python_dir) {
                for entry in entries.flatten() {
                    let bin_dir = entry.path().join("bin");
                    if bin_dir.is_dir() {
                        let bin_str = bin_dir.to_string_lossy().to_string();
                        if !path.contains(&bin_str) {
                            path.push(':');
                            path.push_str(&bin_str);
                        }
                    }
                }
            }
        }
        path
    }
}

pub fn resolve_path_with_overrides() -> String {
    let config_path = default_config_path();
    if config_path.exists() {
        if let Ok(content) = std::fs::read_to_string(&config_path) {
            if let Ok(config) = serde_json::from_str::<serde_json::Value>(&content) {
                // Check for cached resolved_path
                if let Some(cached) = config.pointer("/desktop/resolvedPath").and_then(|v| v.as_str()) {
                    if !cached.is_empty() {
                        return cached.to_string();
                    }
                }
            }
        }
    }
    resolve_user_path()
}

pub fn save_resolved_path(resolved_path: &str) {
    let config_path = default_config_path();
    let mut config: serde_json::Value = if config_path.exists() {
        std::fs::read_to_string(&config_path)
            .ok()
            .and_then(|c| serde_json::from_str(&c).ok())
            .unwrap_or(serde_json::json!({}))
    } else {
        serde_json::json!({})
    };

    if let Some(obj) = config.as_object_mut() {
        let desktop = obj.entry("desktop").or_insert(serde_json::json!({}));
        if let Some(d) = desktop.as_object_mut() {
            d.insert("resolvedPath".to_string(), serde_json::json!(resolved_path));
        }
    }

    if let Some(parent) = config_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(content) = serde_json::to_string_pretty(&config) {
        let _ = std::fs::write(&config_path, content);
    }
}

#[derive(Serialize)]
pub struct CheckResult {
    pub name: String,
    pub status: String,
    pub version: String,
    pub detail: String,
    pub install_cmd: String,
}

#[derive(Serialize)]
pub struct PreflightResult {
    pub python: CheckResult,
    pub companio: CheckResult,
    pub claude_cli: CheckResult,
    pub all_pass: bool,
    pub resolved_path: String,
}

#[cfg(target_os = "windows")]
fn try_py_launcher(path: &str) -> Option<(String, u32, u32)> {
    let output = Command::new("py").args(["-3", "--version"]).env("PATH", path).output().ok()?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let version_line = if !stdout.trim().is_empty() { stdout.trim().to_string() } else { stderr.trim().to_string() };
    let version_str = version_line.strip_prefix("Python ").unwrap_or("").trim().to_string();
    let parts: Vec<&str> = version_str.splitn(3, '.').collect();
    let major: u32 = parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
    let minor: u32 = parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    if major > 0 { Some((version_str, major, minor)) } else { None }
}

fn try_python_cmd(cmd: &str, path: &str) -> Option<(String, u32, u32)> {
    let output = Command::new(cmd).arg("--version").env("PATH", path).output().ok()?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let version_line = if !stdout.trim().is_empty() { stdout.trim().to_string() } else { stderr.trim().to_string() };
    let version_str = version_line.strip_prefix("Python ").unwrap_or("").trim().to_string();
    let parts: Vec<&str> = version_str.splitn(3, '.').collect();
    let major: u32 = parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
    let minor: u32 = parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    if major > 0 { Some((version_str, major, minor)) } else { None }
}

fn check_python(path: &str) -> CheckResult {
    let fail_install_cmd = if cfg!(target_os = "windows") {
        "winget install Python.Python.3.11"
    } else {
        "brew install python@3.11"
    };

    #[cfg(target_os = "windows")]
    {
        if let Some((version_str, major, minor)) = try_py_launcher(path) {
            if major > 3 || (major == 3 && minor >= 11) {
                return CheckResult {
                    name: "python".to_string(),
                    status: "pass".to_string(),
                    version: version_str,
                    detail: String::new(),
                    install_cmd: String::new(),
                };
            }
        }
    }

    let candidates = if cfg!(target_os = "windows") {
        vec!["python", "python3"]
    } else {
        vec!["python3", "python3.13", "python3.12", "python3.11"]
    };

    for cmd in &candidates {
        if let Some((version_str, major, minor)) = try_python_cmd(cmd, path) {
            if major > 3 || (major == 3 && minor >= 11) {
                return CheckResult {
                    name: "python".to_string(),
                    status: "pass".to_string(),
                    version: version_str,
                    detail: String::new(),
                    install_cmd: String::new(),
                };
            }
        }
    }

    // All candidates failed or too old — report the best info we have
    if let Some((version_str, _, _)) = try_python_cmd(candidates[0], path) {
        CheckResult {
            name: "python".to_string(),
            status: "fail".to_string(),
            version: version_str.clone(),
            detail: format!("Python >= 3.11 required, found {}", version_str),
            install_cmd: fail_install_cmd.to_string(),
        }
    } else {
        CheckResult {
            name: "python".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: "Python not found".to_string(),
            install_cmd: fail_install_cmd.to_string(),
        }
    }
}

fn check_companio(path: &str) -> CheckResult {
    let binary = find_in_path("companio", path);
    let output = match &binary {
        Some(p) => Command::new(p).arg("--help").env("PATH", path).output(),
        None => return CheckResult {
            name: "companio".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: "Could not run companio: program not found".to_string(),
            install_cmd: "pip install companio".to_string(),
        },
    };

    match output {
        Ok(out) if out.status.success() => CheckResult {
            name: "companio".to_string(),
            status: "pass".to_string(),
            version: String::new(),
            detail: String::new(),
            install_cmd: String::new(),
        },
        Ok(out) => CheckResult {
            name: "companio".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: format!(
                "companio --help exited with status {}",
                out.status.code().unwrap_or(-1)
            ),
            install_cmd: "pip install companio".to_string(),
        },
        Err(e) => CheckResult {
            name: "companio".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: format!("Could not run companio: {}", e),
            install_cmd: "pip install companio".to_string(),
        },
    }
}

fn check_claude_cli(path: &str) -> CheckResult {
    let binary = find_in_path("claude", path);
    let output = match &binary {
        Some(p) => Command::new(p).arg("--version").env("PATH", path).output(),
        None => return CheckResult {
            name: "claude_cli".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: "Could not run claude: program not found".to_string(),
            install_cmd: "npm install -g @anthropic-ai/claude-code".to_string(),
        },
    };

    match output {
        Ok(out) if out.status.success() => {
            let version = String::from_utf8_lossy(&out.stdout).trim().to_string();
            CheckResult {
                name: "claude_cli".to_string(),
                status: "pass".to_string(),
                version,
                detail: String::new(),
                install_cmd: String::new(),
            }
        }
        Ok(out) => CheckResult {
            name: "claude_cli".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: format!(
                "claude --version exited with status {}",
                out.status.code().unwrap_or(-1)
            ),
            install_cmd: "npm install -g @anthropic-ai/claude-code".to_string(),
        },
        Err(e) => CheckResult {
            name: "claude_cli".to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: format!("Could not run claude: {}", e),
            install_cmd: "npm install -g @anthropic-ai/claude-code".to_string(),
        },
    }
}

fn check_binary_version(binary_path: &str, flag: &str, name: &str, min_version: &str) -> CheckResult {
    match Command::new(binary_path).arg(flag).output() {
        Ok(out) if out.status.success() => {
            let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
            let version_line = if !stdout.is_empty() { stdout } else { stderr };
            let version_str = version_line.strip_prefix("Python ").unwrap_or(&version_line).trim().to_string();

            let parts: Vec<&str> = min_version.splitn(2, '.').collect();
            let req_major: u32 = parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
            let req_minor: u32 = parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);

            let ver_parts: Vec<&str> = version_str.splitn(3, '.').collect();
            let major: u32 = ver_parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
            let minor: u32 = ver_parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);

            if major > req_major || (major == req_major && minor >= req_minor) {
                CheckResult {
                    name: name.to_string(),
                    status: "pass".to_string(),
                    version: version_str,
                    detail: String::new(),
                    install_cmd: String::new(),
                }
            } else {
                CheckResult {
                    name: name.to_string(),
                    status: "fail".to_string(),
                    version: version_str.clone(),
                    detail: format!("Version >= {} required, found {}", min_version, version_str),
                    install_cmd: String::new(),
                }
            }
        }
        _ => CheckResult {
            name: name.to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: format!("Could not run {}", binary_path),
            install_cmd: String::new(),
        },
    }
}

fn check_binary_exists(binary_path: &str, flag: &str, name: &str) -> CheckResult {
    match Command::new(binary_path).arg(flag).output() {
        Ok(out) if out.status.success() => CheckResult {
            name: name.to_string(),
            status: "pass".to_string(),
            version: "installed".to_string(),
            detail: String::new(),
            install_cmd: String::new(),
        },
        _ => CheckResult {
            name: name.to_string(),
            status: "fail".to_string(),
            version: String::new(),
            detail: format!("Could not run {}", binary_path),
            install_cmd: String::new(),
        },
    }
}

pub fn run_preflight() -> PreflightResult {
    let path = resolve_user_path();

    // Check for manual overrides in config
    let config_path = default_config_path();
    let config: serde_json::Value = if config_path.exists() {
        std::fs::read_to_string(&config_path)
            .ok()
            .and_then(|c| serde_json::from_str(&c).ok())
            .unwrap_or_default()
    } else {
        serde_json::Value::Null
    };

    let python_override = config.pointer("/desktop/pythonPath").and_then(|v| v.as_str()).unwrap_or("");
    let companio_override = config.pointer("/desktop/companioPath").and_then(|v| v.as_str()).unwrap_or("");
    let claude_override = config.pointer("/desktop/claudePath").and_then(|v| v.as_str()).unwrap_or("");

    let python = if !python_override.is_empty() {
        check_binary_version(python_override, "--version", "python", "3.11")
    } else {
        check_python(&path)
    };

    let companio = if !companio_override.is_empty() {
        check_binary_exists(companio_override, "--help", "companio")
    } else {
        check_companio(&path)
    };

    let claude_cli = if !claude_override.is_empty() {
        check_binary_exists(claude_override, "--version", "claude_cli")
    } else {
        check_claude_cli(&path)
    };

    let all_pass = python.status == "pass"
        && companio.status == "pass"
        && claude_cli.status == "pass";

    // Cache resolved path when preflight passes
    if all_pass {
        save_resolved_path(&path);
    }

    PreflightResult {
        python,
        companio,
        claude_cli,
        all_pass,
        resolved_path: path,
    }
}

pub fn open_terminal_with_command(cmd: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        // Escape double quotes inside the command for embedding in osascript string
        let escaped = cmd.replace('\\', "\\\\").replace('"', "\\\"");
        let script = format!(
            r#"tell application "Terminal"
    activate
    do script "{}"
end tell"#,
            escaped
        );
        Command::new("osascript")
            .arg("-e")
            .arg(&script)
            .spawn()
            .map_err(|e| format!("osascript failed: {}", e))?;
        return Ok(());
    }

    #[cfg(target_os = "windows")]
    {
        Command::new("cmd")
            .args(["/c", "start", "cmd", "/k", cmd])
            .spawn()
            .map_err(|e| format!("cmd failed: {}", e))?;
        return Ok(());
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        Err(format!("open_terminal_with_command is not supported on this platform"))
    }
}

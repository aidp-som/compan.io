use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::Mutex;

const MAX_LOG_LINES: usize = 500;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogLine {
    pub level: String,
    pub message: String,
    pub timestamp: String,
}

pub struct LogBuffer {
    lines: Mutex<VecDeque<LogLine>>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            lines: Mutex::new(VecDeque::with_capacity(MAX_LOG_LINES)),
        }
    }

    pub fn push(&self, line: LogLine) {
        let mut buf = self.lines.lock().unwrap();
        if buf.len() >= MAX_LOG_LINES {
            buf.pop_front();
        }
        buf.push_back(line);
    }

    pub fn get_all(&self) -> Vec<LogLine> {
        self.lines.lock().unwrap().iter().cloned().collect()
    }

    pub fn clear(&self) {
        self.lines.lock().unwrap().clear();
    }
}

pub fn parse_loguru_json(raw: &str) -> Option<LogLine> {
    let v: serde_json::Value = serde_json::from_str(raw).ok()?;
    let record = v.get("record")?;
    Some(LogLine {
        level: record.get("level")?.get("name")?.as_str()?.to_string(),
        message: record.get("message")?.as_str()?.to_string(),
        timestamp: record
            .get("time")
            .and_then(|t| t.get("repr"))
            .and_then(|r| r.as_str())
            .unwrap_or("")
            .to_string(),
    })
}

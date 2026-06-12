"""IPC event emission for Tauri desktop wrapper."""

import json
import sys
import threading
from datetime import datetime, timezone
from typing import IO, Any

_lock = threading.Lock()


def emit_event(event_type: str, data: dict[str, Any], stream: IO[str] | None = None) -> None:
    """Write a JSON Lines event to the given stream (default: stdout)."""
    target = stream or sys.stdout
    event = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
    with _lock:
        target.write(line)
        target.flush()

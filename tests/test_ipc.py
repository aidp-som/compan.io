"""Tests for IPC event emission."""

import io
import json

from companio.ipc import emit_event


class TestEmitEvent:
    def test_emits_json_line_to_stream(self):
        buf = io.StringIO()
        emit_event("ready", {"version": "0.1.2", "pid": 123}, stream=buf)
        line = buf.getvalue()
        assert line.endswith("\n")
        parsed = json.loads(line)
        assert parsed["type"] == "ready"
        assert parsed["data"]["version"] == "0.1.2"
        assert parsed["data"]["pid"] == 123
        assert "ts" in parsed

    def test_emits_multiple_events(self):
        buf = io.StringIO()
        emit_event("health", {"ok": True}, stream=buf)
        emit_event("error", {"code": "FAIL"}, stream=buf)
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "health"
        assert json.loads(lines[1])["type"] == "error"

    def test_timestamp_is_iso_format(self):
        buf = io.StringIO()
        emit_event("test", {}, stream=buf)
        parsed = json.loads(buf.getvalue())
        assert "T" in parsed["ts"]

    def test_handles_non_serializable_gracefully(self):
        buf = io.StringIO()
        emit_event("test", {"path": "/tmp/foo"}, stream=buf)
        parsed = json.loads(buf.getvalue())
        assert parsed["data"]["path"] == "/tmp/foo"

#!/usr/bin/env python3
"""Tests for Slack pipe table → Block Kit table block conversion."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companio.channels.slack import (
    _markdown_to_blocks,
    _markdown_to_mrkdwn,
    _pipe_table_to_block,
    _render_slack_table,
)


def test_pipe_table_to_block_basic():
    lines = [
        "| Name | Status |",
        "|------|--------|",
        "| Alice | done |",
        "| Bob | open |",
    ]
    block = _pipe_table_to_block(lines)
    assert block is not None
    assert block["type"] == "table"
    assert len(block["rows"]) == 3  # header + 2 data rows
    assert block["rows"][0][0]["text"] == "Name"
    assert block["rows"][1][0]["text"] == "Alice"


def test_pipe_table_to_block_korean():
    lines = [
        "| 코드 | 제목 | 상태 |",
        "|------|------|------|",
        "| REQ-001 | 버그 수정 | open |",
    ]
    block = _pipe_table_to_block(lines)
    assert block is not None
    assert block["rows"][0][0]["text"] == "코드"
    assert block["rows"][1][0]["text"] == "REQ-001"


def test_pipe_table_to_block_no_separator_returns_none():
    lines = [
        "| Name | Status |",
        "| Alice | done |",
    ]
    block = _pipe_table_to_block(lines)
    assert block is None


def test_markdown_to_blocks_with_table():
    md = "# Report\n\nSome text\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nMore text"
    blocks = _markdown_to_blocks(md)
    assert blocks is not None
    block_types = [b["type"] for b in blocks]
    assert "table" in block_types
    assert "header" in block_types


def test_markdown_to_blocks_table_has_correct_data():
    md = "| Code | Title |\n|------|-------|\n| R-001 | Bug |\n| R-002 | Feature |"
    blocks = _markdown_to_blocks(md)
    table_blocks = [b for b in blocks if b["type"] == "table"]
    assert len(table_blocks) == 1
    tbl = table_blocks[0]
    assert len(tbl["rows"]) == 3  # header + 2 data
    assert tbl["rows"][0][0]["text"] == "Code"
    assert tbl["rows"][1][0]["text"] == "R-001"
    assert tbl["rows"][2][1]["text"] == "Feature"


def test_markdown_to_blocks_no_table():
    md = "# Title\n\nJust text\n- bullet 1\n- bullet 2"
    blocks = _markdown_to_blocks(md)
    block_types = [b["type"] for b in blocks]
    assert "table" not in block_types


def test_render_slack_table_still_works():
    """_render_slack_table fallback for non-block-kit contexts."""
    lines = [
        "| Name | Status |",
        "|------|--------|",
        "| Alice | done |",
    ]
    result = _render_slack_table(lines)
    assert "|" not in result
    assert "Name" in result
    assert "─" in result


def test_pipe_table_to_block_empty_cells_replaced():
    """Empty cells must become a single space; Slack API rejects empty strings."""
    lines = [
        "| 시간 | 일정 | 비고 |",
        "|------|------|------|",
        "| 09:00 | | |",
        "| 10:00 | | |",
    ]
    block = _pipe_table_to_block(lines)
    assert block is not None
    # Header row should be normal
    assert block["rows"][0][0]["text"] == "시간"
    # Empty cells in data rows must be " " (space), not ""
    for row in block["rows"][1:]:
        for cell in row:
            assert len(cell["text"]) > 0, f"Cell text must not be empty: {cell}"
    assert block["rows"][1][1]["text"] == " "
    assert block["rows"][2][2]["text"] == " "


def test_markdown_to_blocks_empty_cell_table():
    """Full markdown-to-blocks pipeline with empty cells produces valid table."""
    md = "오늘 일정표\n\n| 시간 | 일정 |\n|------|------|\n| 09:00 | |\n| 10:00 | |"
    blocks = _markdown_to_blocks(md)
    assert blocks is not None
    table_blocks = [b for b in blocks if b["type"] == "table"]
    assert len(table_blocks) == 1
    tbl = table_blocks[0]
    # All cells must have non-empty text
    for row in tbl["rows"]:
        for cell in row:
            assert cell["text"], f"Cell text must not be empty: {cell}"


if __name__ == "__main__":
    test_pipe_table_to_block_basic()
    test_pipe_table_to_block_korean()
    test_pipe_table_to_block_no_separator_returns_none()
    test_markdown_to_blocks_with_table()
    test_markdown_to_blocks_table_has_correct_data()
    test_markdown_to_blocks_no_table()
    test_render_slack_table_still_works()
    test_pipe_table_to_block_empty_cells_replaced()
    test_markdown_to_blocks_empty_cell_table()
    print("ALL TESTS PASSED")

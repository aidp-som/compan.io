"""Tests for outbound media auto-attach feature."""

from pathlib import Path

import pytest


@pytest.fixture
def outbound_dir(tmp_path):
    """Create a temporary outbound media directory."""
    d = tmp_path / "media" / "outbound"
    d.mkdir(parents=True)
    return d


def test_collect_outbound_media_finds_files(outbound_dir):
    """Files in outbound dir are collected and returned as sorted list."""
    from companio.core.loop import _collect_outbound_media

    (outbound_dir / "report.xlsx").write_bytes(b"fake excel")
    (outbound_dir / "data.csv").write_text("a,b\n1,2")

    result = _collect_outbound_media(outbound_dir)

    assert len(result) == 2
    assert all(isinstance(p, str) for p in result)
    filenames = [Path(p).name for p in result]
    assert "data.csv" in filenames
    assert "report.xlsx" in filenames


def test_collect_outbound_media_empty_dir(outbound_dir):
    """Empty directory returns empty list."""
    from companio.core.loop import _collect_outbound_media

    result = _collect_outbound_media(outbound_dir)
    assert result == []


def test_collect_outbound_media_missing_dir(tmp_path):
    """Non-existent directory returns empty list without error."""
    from companio.core.loop import _collect_outbound_media

    result = _collect_outbound_media(tmp_path / "does_not_exist")
    assert result == []


def test_cleanup_outbound_media_removes_files(outbound_dir):
    """Sent files are deleted after collection."""
    from companio.core.loop import _cleanup_outbound_media

    f1 = outbound_dir / "report.xlsx"
    f2 = outbound_dir / "data.csv"
    f1.write_bytes(b"fake")
    f2.write_text("a,b")

    _cleanup_outbound_media([str(f1), str(f2)])

    assert not f1.exists()
    assert not f2.exists()


def test_cleanup_outbound_media_ignores_missing():
    """Cleanup does not raise if files are already gone."""
    from companio.core.loop import _cleanup_outbound_media

    _cleanup_outbound_media(["/tmp/does_not_exist_12345.csv"])

#!/usr/bin/env python3
"""Tests for zip archive → markdown conversion."""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companio.converters import convert_if_needed, ConversionResult


def _make_zip(files: dict[str, str | bytes], path: Path) -> Path:
    """Create a zip file with the given {filename: content} mapping."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            if isinstance(content, str):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content)
    return path


def test_basic_text_files():
    with tempfile.TemporaryDirectory() as tmp:
        zp = _make_zip({"readme.txt": "Hello World", "data.csv": "a,b\n1,2"}, Path(tmp) / "test.zip")
        result = convert_if_needed(zp)
        assert result.converted
        md = Path(result.path).read_text()
        assert "Hello World" in md
        assert "a,b" in md
        assert "readme.txt" in md


def test_binary_files_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        zp = _make_zip({"image.png": b"\x89PNG\r\n", "notes.txt": "hello"}, Path(tmp) / "test.zip")
        result = convert_if_needed(zp)
        assert result.converted
        md = Path(result.path).read_text()
        assert "바이너리 파일 생략" in md
        assert "hello" in md


def test_path_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "evil.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("../../../etc/passwd", "root:x:0:0")
            zf.writestr("safe.txt", "safe content")
        result = convert_if_needed(zp)
        assert result.converted
        md = Path(result.path).read_text()
        assert "safe content" in md
        assert "root:x:0:0" not in md


def test_empty_zip():
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "empty.zip"
        with zipfile.ZipFile(zp, "w"):
            pass
        result = convert_if_needed(zp)
        assert result.converted
        assert "0 files" in result.meta


def test_not_a_zip():
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake.zip"
        fake.write_text("not a zip file")
        result = convert_if_needed(fake)
        assert not result.converted
        assert "not a valid zip" in result.meta


def test_nested_office_files():
    with tempfile.TemporaryDirectory() as tmp:
        # Create a zip with a CSV inside (simplest convertible-like test)
        zp = _make_zip({
            "report.csv": "Name,Score\nAlice,95\nBob,87",
            "notes.md": "# Meeting Notes\n\nDiscussed Q3 targets.",
        }, Path(tmp) / "bundle.zip")
        result = convert_if_needed(zp)
        assert result.converted
        md = Path(result.path).read_text()
        assert "Alice" in md
        assert "Meeting Notes" in md


def test_file_count_limit():
    with tempfile.TemporaryDirectory() as tmp:
        files = {f"file_{i:03d}.txt": f"content {i}" for i in range(60)}
        zp = _make_zip(files, Path(tmp) / "many.zip")
        result = convert_if_needed(zp)
        assert result.converted
        assert "skipped" in result.meta
        md = Path(result.path).read_text()
        assert "제한 초과로 생략" in md


def test_zip_bomb_rejected():
    from companio.converters import _MAX_ZIP_EXTRACTED_SIZE
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "bomb.zip"
        # Create a file that exceeds the extracted size limit
        chunk = b"A" * (1024 * 1024)  # 1MB chunk
        with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Write enough 1MB files to exceed the 50MB limit
            for i in range(55):
                zf.writestr(f"big_{i:03d}.txt", chunk)
        result = convert_if_needed(zp)
        assert not result.converted
        assert "too large" in result.meta


def test_nested_directory_structure():
    with tempfile.TemporaryDirectory() as tmp:
        zp = _make_zip({
            "docs/readme.txt": "top level readme",
            "docs/sub/notes.md": "# Nested notes",
        }, Path(tmp) / "nested.zip")
        result = convert_if_needed(zp)
        assert result.converted
        md = Path(result.path).read_text()
        assert "top level readme" in md
        assert "Nested notes" in md
        assert "docs/readme.txt" in md


if __name__ == "__main__":
    test_basic_text_files()
    test_binary_files_skipped()
    test_path_traversal_blocked()
    test_empty_zip()
    test_not_a_zip()
    test_nested_office_files()
    test_file_count_limit()
    test_zip_bomb_rejected()
    test_nested_directory_structure()
    print("ALL TESTS PASSED")

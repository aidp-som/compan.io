"""Unit tests for `ContextBuilder.format_media_tags`.

Covers the prompt-injection-safe attachment block that `_process_message_inner`
appends after the user's content. The block must:
- be empty when no media, so media-less flows remain byte-for-byte unchanged
- wrap real attachments in `<external-context trust="medium" source="channel-upload">`
- classify images by case-insensitive extension
- filter empty entries and deduplicate preserving order
"""

from __future__ import annotations

from companio.core.context import ContextBuilder

OPEN = '<external-context trust="medium" source="channel-upload">'
CLOSE = "</external-context>"


def _expect(*lines: str) -> str:
    body = "\n".join(lines)
    return f"\n\n{OPEN}\n{body}\n{CLOSE}"


class TestFormatMediaTagsBasics:
    def test_empty_list_returns_empty_string(self):
        assert ContextBuilder.format_media_tags([]) == ""

    def test_all_falsy_entries_return_empty_string(self):
        # Download failures may leave empty strings; the block must stay empty
        # so the existing media=[] regression guard keeps holding.
        assert ContextBuilder.format_media_tags(["", "", ""]) == ""

    def test_single_image(self):
        assert ContextBuilder.format_media_tags(["a.png"]) == _expect("[image: a.png]")

    def test_single_non_image(self):
        assert ContextBuilder.format_media_tags(["a.txt"]) == _expect("[file: a.txt]")

    def test_noext_classified_as_file(self):
        assert ContextBuilder.format_media_tags(["noext"]) == _expect("[file: noext]")


class TestFormatMediaTagsImageClassification:
    def test_uppercase_extension_is_image(self):
        assert ContextBuilder.format_media_tags(["a.PNG"]) == _expect("[image: a.PNG]")

    def test_webp_is_image(self):
        assert ContextBuilder.format_media_tags(["x.webp"]) == _expect("[image: x.webp]")

    def test_mixed_images_and_files_preserve_order(self):
        result = ContextBuilder.format_media_tags(["a.PNG", "b.pdf", "c.JPG"])
        assert result == _expect("[image: a.PNG]", "[file: b.pdf]", "[image: c.JPG]")


class TestFormatMediaTagsPaths:
    def test_windows_backslash_and_hangul(self):
        path = "C:\\Users\\som_server_2\\workspace\\media\\스크린샷.PNG"
        assert ContextBuilder.format_media_tags([path]) == _expect(f"[image: {path}]")

    def test_path_with_spaces(self):
        path = "C:/Users/som/workspace/media/with space.jpg"
        assert ContextBuilder.format_media_tags([path]) == _expect(f"[image: {path}]")


class TestFormatMediaTagsFiltering:
    def test_empty_entry_is_filtered(self):
        assert ContextBuilder.format_media_tags(["", "ok.png"]) == _expect("[image: ok.png]")

    def test_duplicates_deduped_preserving_order(self):
        result = ContextBuilder.format_media_tags(["a.png", "a.png", "b.jpg"])
        assert result == _expect("[image: a.png]", "[image: b.jpg]")

    def test_fifty_entries_produce_fifty_tags(self):
        paths = [f"C:/media/{i}.png" for i in range(50)]
        result = ContextBuilder.format_media_tags(paths)
        assert result.count("[image: ") == 50
        assert result.startswith(f"\n\n{OPEN}\n[image: C:/media/0.png]")
        assert result.endswith(f"[image: C:/media/49.png]\n{CLOSE}")

"""Tests for text vs. binary file detection."""

from __future__ import annotations

import os

from repo_sync.strip.detect import is_binary


class TestIsBinary:
    """Tests covering the null-byte heuristic for binary detection."""

    def test_text_file(self, tmp_path: os.PathLike) -> None:
        """A file with no null bytes is classified as text."""
        p = tmp_path / "hello.txt"
        p.write_text("hello world\n", encoding="utf-8")
        assert is_binary(str(p)) is False

    def test_binary_file_null_in_first_chunk(self, tmp_path: os.PathLike) -> None:
        """A file with a null byte in the first 8192 bytes is classified as binary."""
        p = tmp_path / "data.bin"
        p.write_bytes(b"header\x00rest")
        assert is_binary(str(p)) is True

    def test_png_like_binary(self, tmp_path: os.PathLike) -> None:
        """A PNG-like binary file is classified as binary."""
        p = tmp_path / "image.png"
        # PNG magic bytes contain null bytes.
        p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        assert is_binary(str(p)) is True

    def test_null_byte_beyond_sniff_size(self, tmp_path: os.PathLike) -> None:
        """A file with a null byte past the 8192-byte boundary is text."""
        p = tmp_path / "tricky.txt"
        # 8192 bytes of 'a', then a null byte.
        p.write_bytes(b"a" * 8192 + b"\x00")
        assert is_binary(str(p)) is False

    def test_empty_file(self, tmp_path: os.PathLike) -> None:
        """An empty file is classified as text (no null bytes)."""
        p = tmp_path / "empty.txt"
        p.write_bytes(b"")
        assert is_binary(str(p)) is False

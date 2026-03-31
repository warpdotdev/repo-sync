"""Tests for the shared marker library (parsing, validation, stripping)."""

from __future__ import annotations

import pytest

from repo_sync.strip.markers import (
    MarkerError,
    strip_private_regions,
    validate_markers,
)


# ---------------------------------------------------------------------------
# Marker stripping -- happy paths
# ---------------------------------------------------------------------------


class TestStripPrivateRegions:
    """Tests covering correct stripping of private regions."""

    def test_single_region(self) -> None:
        """A single private-start/private-end region is stripped correctly."""
        lines = [
            "public line 1\n",
            "// !repo-sync: private-start\n",
            "secret line\n",
            "// !repo-sync: private-end\n",
            "public line 2\n",
        ]
        result = strip_private_regions(lines)
        assert result == ["public line 1\n", "public line 2\n"]

    def test_multiple_regions(self) -> None:
        """Multiple non-overlapping regions in the same file are all stripped."""
        lines = [
            "a\n",
            "# !repo-sync: private-start\n",
            "secret1\n",
            "# !repo-sync: private-end\n",
            "b\n",
            "# !repo-sync: private-start\n",
            "secret2\n",
            "# !repo-sync: private-end\n",
            "c\n",
        ]
        result = strip_private_regions(lines)
        assert result == ["a\n", "b\n", "c\n"]

    def test_no_blank_lines_left(self) -> None:
        """Stripping leaves no blank lines where the region was removed."""
        lines = [
            "before\n",
            "// !repo-sync: private-start\n",
            "private\n",
            "// !repo-sync: private-end\n",
            "after\n",
        ]
        result = strip_private_regions(lines)
        assert result == ["before\n", "after\n"]

    def test_comment_syntax_hash(self) -> None:
        """Markers work with # comment syntax."""
        lines = [
            "# !repo-sync: private-start\n",
            "private\n",
            "# !repo-sync: private-end\n",
        ]
        result = strip_private_regions(lines)
        assert result == []

    def test_comment_syntax_double_slash(self) -> None:
        """Markers work with // comment syntax."""
        lines = [
            "// !repo-sync: private-start\n",
            "private\n",
            "// !repo-sync: private-end\n",
        ]
        result = strip_private_regions(lines)
        assert result == []

    def test_comment_syntax_block(self) -> None:
        """Markers work with /* block comment syntax."""
        lines = [
            "/* !repo-sync: private-start */\n",
            "private\n",
            "/* !repo-sync: private-end */\n",
        ]
        result = strip_private_regions(lines)
        assert result == []

    def test_comment_syntax_dash_dash(self) -> None:
        """Markers work with -- comment syntax (SQL, Lua, etc.)."""
        lines = [
            "-- !repo-sync: private-start\n",
            "private\n",
            "-- !repo-sync: private-end\n",
        ]
        result = strip_private_regions(lines)
        assert result == []

    def test_leading_whitespace(self) -> None:
        """Markers work with leading whitespace (indented markers)."""
        lines = [
            "public\n",
            "    // !repo-sync: private-start\n",
            "    private\n",
            "    // !repo-sync: private-end\n",
            "public\n",
        ]
        result = strip_private_regions(lines)
        assert result == ["public\n", "public\n"]

    def test_trailing_content(self) -> None:
        """Markers work with trailing content after the marker string."""
        lines = [
            "// !repo-sync: private-start -- reason: internal API\n",
            "private\n",
            "// !repo-sync: private-end -- end internal API\n",
        ]
        result = strip_private_regions(lines)
        assert result == []

    def test_entire_file_stripped_becomes_empty(self) -> None:
        """A file where all content is inside a marker region becomes empty."""
        lines = [
            "// !repo-sync: private-start\n",
            "all private\n",
            "// !repo-sync: private-end\n",
        ]
        result = strip_private_regions(lines)
        assert result == []

    def test_partial_strip_preserves_public(self) -> None:
        """Stripping multiple regions leaves only public content."""
        lines = [
            "pub1\n",
            "# !repo-sync: private-start\n",
            "priv1\n",
            "# !repo-sync: private-end\n",
            "pub2\n",
            "# !repo-sync: private-start\n",
            "priv2\n",
            "# !repo-sync: private-end\n",
            "pub3\n",
        ]
        result = strip_private_regions(lines)
        assert result == ["pub1\n", "pub2\n", "pub3\n"]

    def test_no_markers_returns_all_lines(self) -> None:
        """A file with no markers returns all lines unchanged."""
        lines = ["line1\n", "line2\n"]
        result = strip_private_regions(lines)
        assert result == lines

    def test_empty_file(self) -> None:
        """An empty file returns an empty list."""
        result = strip_private_regions([])
        assert result == []


# ---------------------------------------------------------------------------
# Marker validation -- error cases
# ---------------------------------------------------------------------------


class TestValidateMarkers:
    """Tests covering marker validation error detection."""

    def test_unpaired_start(self) -> None:
        """private-start without a matching private-end is an error."""
        lines = [
            "// !repo-sync: private-start\n",
            "content\n",
        ]
        errors = validate_markers(lines, filepath="test.rs")
        assert len(errors) == 1
        assert "unterminated" in errors[0]

    def test_unpaired_end(self) -> None:
        """private-end without a preceding private-start is an error."""
        lines = [
            "content\n",
            "// !repo-sync: private-end\n",
        ]
        errors = validate_markers(lines, filepath="test.rs")
        assert len(errors) == 1
        assert "without matching" in errors[0]

    def test_nested_markers(self) -> None:
        """private-start inside an open region is an error."""
        lines = [
            "// !repo-sync: private-start\n",
            "// !repo-sync: private-start\n",
            "content\n",
            "// !repo-sync: private-end\n",
        ]
        errors = validate_markers(lines, filepath="test.rs")
        assert len(errors) == 1
        assert "nested" in errors[0]

    def test_markers_split_across_files(self) -> None:
        """Each file is validated independently; cross-file pairing errors."""
        # File 1: start without end.
        lines1 = ["// !repo-sync: private-start\n", "content\n"]
        errors1 = validate_markers(lines1, filepath="a.rs")
        assert len(errors1) == 1

        # File 2: end without start.
        lines2 = ["content\n", "// !repo-sync: private-end\n"]
        errors2 = validate_markers(lines2, filepath="b.rs")
        assert len(errors2) == 1

    def test_valid_markers_no_errors(self) -> None:
        """Correctly paired, non-nested markers produce no errors."""
        lines = [
            "// !repo-sync: private-start\n",
            "secret\n",
            "// !repo-sync: private-end\n",
        ]
        assert validate_markers(lines) == []

    def test_no_markers_no_errors(self) -> None:
        """A file with no markers produces no errors."""
        lines = ["public code\n"]
        assert validate_markers(lines) == []

    def test_both_markers_on_same_line(self) -> None:
        """A line with both markers is an error."""
        lines = ["// !repo-sync: private-start !repo-sync: private-end\n"]
        errors = validate_markers(lines, filepath="test.rs")
        assert len(errors) == 1
        assert "both" in errors[0]


class TestStripPrivateRegionsErrors:
    """strip_private_regions raises MarkerError on invalid markers."""

    def test_raises_on_unpaired_start(self) -> None:
        """MarkerError raised for unterminated private-start."""
        lines = ["// !repo-sync: private-start\n", "content\n"]
        with pytest.raises(MarkerError):
            strip_private_regions(lines)

    def test_raises_on_unpaired_end(self) -> None:
        """MarkerError raised for private-end without start."""
        lines = ["// !repo-sync: private-end\n"]
        with pytest.raises(MarkerError):
            strip_private_regions(lines)

    def test_raises_on_nested(self) -> None:
        """MarkerError raised for nested markers."""
        lines = [
            "// !repo-sync: private-start\n",
            "// !repo-sync: private-start\n",
            "// !repo-sync: private-end\n",
        ]
        with pytest.raises(MarkerError):
            strip_private_regions(lines)

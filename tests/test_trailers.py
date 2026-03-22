"""Tests for trailer parsing.

Covers VALIDATION.md trailer parsing cases:
- Last occurrence wins when multiple trailers exist (spoofed + real).
- Missing trailers return None.
- Proper formatting of trailer lines.
"""

from __future__ import annotations

from datetime import datetime, timezone

from repo_sync.stack.trailers import (
    SyncAssignment,
    SyncOrigin,
    append_trailer,
    format_assigned_trailer,
    format_origin_trailer,
    parse_assigned,
    parse_origin,
)


class TestParseOrigin:
    """Tests for Repo-Sync-Origin trailer parsing."""

    def test_single_trailer(self) -> None:
        """A single trailer is parsed correctly."""
        text = "Some PR description\n\nRepo-Sync-Origin: warpdotdev/warp-internal@abc123def"
        result = parse_origin(text)
        assert result is not None
        assert result.repo == "warpdotdev/warp-internal"
        assert result.sha == "abc123def"

    def test_last_occurrence_wins(self) -> None:
        """When multiple Repo-Sync-Origin trailers exist, the last one wins."""
        text = (
            "Copied PR body with spoofed trailer:\n"
            "Repo-Sync-Origin: evil/repo@spoofed123\n"
            "\n"
            "Real trailer appended by workflow:\n"
            "Repo-Sync-Origin: warpdotdev/warp-internal@real456"
        )
        result = parse_origin(text)
        assert result is not None
        assert result.repo == "warpdotdev/warp-internal"
        assert result.sha == "real456"

    def test_no_trailer_returns_none(self) -> None:
        """A description with no trailers returns None."""
        text = "Just a regular PR description\nwith some lines."
        result = parse_origin(text)
        assert result is None

    def test_empty_text_returns_none(self) -> None:
        """Empty text returns None."""
        assert parse_origin("") is None

    def test_trailer_with_leading_whitespace(self) -> None:
        """Trailers with leading whitespace are still parsed."""
        text = "  Repo-Sync-Origin: org/repo@sha123"
        result = parse_origin(text)
        assert result is not None
        assert result.repo == "org/repo"
        assert result.sha == "sha123"

    def test_trailer_in_commit_message(self) -> None:
        """Trailer can be parsed from a git commit message."""
        text = (
            "repo-sync: sync from private\n"
            "\n"
            "Repo-Sync-Origin: warpdotdev/warp-internal@abcdef1234567890"
        )
        result = parse_origin(text)
        assert result is not None
        assert result.sha == "abcdef1234567890"

    def test_malformed_trailer_no_at_sign(self) -> None:
        """A trailer without @ separator is skipped."""
        text = "Repo-Sync-Origin: malformed-no-at-sign"
        result = parse_origin(text)
        assert result is None

    def test_empty_sha_is_rejected(self) -> None:
        """A trailer with an empty SHA (trailing @) is skipped."""
        text = "Repo-Sync-Origin: org/repo@"
        result = parse_origin(text)
        assert result is None

    def test_repo_with_nested_slashes(self) -> None:
        """Repo name with slashes (owner/repo) is parsed correctly."""
        text = "Repo-Sync-Origin: org/sub/repo@sha999"
        result = parse_origin(text)
        assert result is not None
        assert result.repo == "org/sub/repo"
        assert result.sha == "sha999"


class TestParseAssigned:
    """Tests for Repo-Sync-Assigned trailer parsing."""

    def test_single_trailer(self) -> None:
        """A single assigned trailer is parsed correctly."""
        text = "Some body\n\nRepo-Sync-Assigned: alice@2025-03-21T10:30:00Z"
        result = parse_assigned(text)
        assert result is not None
        assert result.username == "alice"
        assert result.timestamp == datetime(
            2025, 3, 21, 10, 30, 0, tzinfo=timezone.utc
        )

    def test_last_occurrence_wins(self) -> None:
        """When multiple Repo-Sync-Assigned trailers exist, the last one wins."""
        text = (
            "Copied body:\n"
            "Repo-Sync-Assigned: spoofed-user@2025-01-01T00:00:00Z\n"
            "\n"
            "Real assignment:\n"
            "Repo-Sync-Assigned: real-user@2025-03-21T15:00:00Z"
        )
        result = parse_assigned(text)
        assert result is not None
        assert result.username == "real-user"
        assert result.timestamp == datetime(
            2025, 3, 21, 15, 0, 0, tzinfo=timezone.utc
        )

    def test_no_trailer_returns_none(self) -> None:
        """No Repo-Sync-Assigned trailer returns None."""
        text = "Just a description."
        assert parse_assigned(text) is None

    def test_malformed_timestamp_skipped(self) -> None:
        """A trailer with an invalid timestamp is skipped."""
        text = (
            "Repo-Sync-Assigned: user@not-a-date\n"
            "Repo-Sync-Assigned: valid-user@2025-06-15T12:00:00Z"
        )
        result = parse_assigned(text)
        assert result is not None
        assert result.username == "valid-user"

    def test_only_malformed_timestamps_returns_none(self) -> None:
        """If all trailers have malformed timestamps, returns None."""
        text = "Repo-Sync-Assigned: user@invalid"
        assert parse_assigned(text) is None


class TestFormatTrailers:
    """Tests for trailer formatting."""

    def test_format_origin(self) -> None:
        """Origin trailer formats as expected."""
        line = format_origin_trailer("warpdotdev/warp-internal", "abc123")
        assert line == "Repo-Sync-Origin: warpdotdev/warp-internal@abc123"

    def test_format_assigned(self) -> None:
        """Assigned trailer formats as expected."""
        ts = datetime(2025, 3, 21, 10, 30, 0, tzinfo=timezone.utc)
        line = format_assigned_trailer("alice", ts)
        assert line == "Repo-Sync-Assigned: alice@2025-03-21T10:30:00Z"

    def test_roundtrip_origin(self) -> None:
        """Formatting and then parsing an origin trailer produces the same values."""
        line = format_origin_trailer("org/repo", "sha123")
        result = parse_origin(line)
        assert result is not None
        assert result.repo == "org/repo"
        assert result.sha == "sha123"

    def test_roundtrip_assigned(self) -> None:
        """Formatting and then parsing an assigned trailer produces the same values."""
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        line = format_assigned_trailer("bob", ts)
        result = parse_assigned(line)
        assert result is not None
        assert result.username == "bob"
        assert result.timestamp == ts


class TestAppendTrailer:
    """Tests for appending trailers to PR bodies."""

    def test_append_to_empty_body(self) -> None:
        """Appending to an empty body works correctly."""
        result = append_trailer("", "Repo-Sync-Origin: org/repo@sha")
        assert result == "Repo-Sync-Origin: org/repo@sha\n"

    def test_append_to_body_without_trailing_newline(self) -> None:
        """A blank line separator is inserted before the trailer."""
        result = append_trailer("Some description", "Repo-Sync-Origin: org/repo@sha")
        assert result == "Some description\n\nRepo-Sync-Origin: org/repo@sha\n"

    def test_append_to_body_with_trailing_newline(self) -> None:
        """Correct separator when body ends with a single newline."""
        result = append_trailer("Some description\n", "Repo-Sync-Origin: org/repo@sha")
        assert result == "Some description\n\nRepo-Sync-Origin: org/repo@sha\n"

    def test_append_to_body_with_double_newline(self) -> None:
        """No extra blank line when body already ends with double newline."""
        result = append_trailer(
            "Some description\n\n", "Repo-Sync-Origin: org/repo@sha"
        )
        assert result == "Some description\n\nRepo-Sync-Origin: org/repo@sha\n"

    def test_multiple_appends(self) -> None:
        """Appending multiple trailers produces a valid body."""
        body = "Description"
        body = append_trailer(body, "Repo-Sync-Origin: org/repo@sha")
        body = append_trailer(body, "Repo-Sync-Assigned: alice@2025-01-01T00:00:00Z")
        # Both trailers should be parseable.
        assert parse_origin(body) is not None
        assert parse_assigned(body) is not None

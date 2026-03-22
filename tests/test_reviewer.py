"""Tests for reviewer assignment logic.

Covers VALIDATION.md reviewer assignment cases:
- Merger of source PR is requested first.
- Commit author is fallback when no merger found.
- Fallback team when neither merger nor author available.
- Repo-Sync-Assigned trailer is appended to PR description.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.reviewer import (
    DEFAULT_FALLBACK_TEAM,
    assign_reviewer,
    determine_reviewer,
)
from repo_sync.stack.trailers import parse_assigned


class TestDetermineReviewer:
    """Tests for the reviewer determination cascade."""

    def test_merger_is_preferred(self) -> None:
        """When a source PR exists, the merger is preferred as reviewer."""
        gh = MagicMock(spec=GhOps)
        gh.get_pr_merger.return_value = "alice"

        result = determine_reviewer(
            gh,
            source_pr_number=42,
            commit_author="bob",
        )
        assert result == "alice"

    def test_commit_author_when_no_merger(self) -> None:
        """When merger is unavailable, fall back to commit author."""
        gh = MagicMock(spec=GhOps)
        gh.get_pr_merger.return_value = None

        result = determine_reviewer(
            gh,
            source_pr_number=42,
            commit_author="bob",
        )
        assert result == "bob"

    def test_commit_author_for_direct_push(self) -> None:
        """For direct pushes (no source PR), commit author is used."""
        gh = MagicMock(spec=GhOps)

        result = determine_reviewer(
            gh,
            source_pr_number=None,
            commit_author="charlie",
        )
        assert result == "charlie"
        # get_pr_merger should not be called since there's no source PR.
        gh.get_pr_merger.assert_not_called()

    def test_fallback_team_when_nothing_else(self) -> None:
        """When no merger or author, the fallback team is used."""
        gh = MagicMock(spec=GhOps)

        result = determine_reviewer(
            gh,
            source_pr_number=None,
            commit_author=None,
        )
        assert result == DEFAULT_FALLBACK_TEAM

    def test_fallback_team_when_merger_and_author_unavailable(self) -> None:
        """Fallback team is used when merger returns None and author is None."""
        gh = MagicMock(spec=GhOps)
        gh.get_pr_merger.return_value = None

        result = determine_reviewer(
            gh,
            source_pr_number=42,
            commit_author=None,
        )
        assert result == DEFAULT_FALLBACK_TEAM

    def test_custom_fallback_team(self) -> None:
        """A custom fallback team can be specified."""
        gh = MagicMock(spec=GhOps)

        result = determine_reviewer(
            gh,
            source_pr_number=None,
            commit_author=None,
            fallback_team="my-custom-team",
        )
        assert result == "my-custom-team"

    def test_empty_string_author_falls_through_to_fallback(self) -> None:
        """An empty-string commit_author is treated as absent (falls through to fallback)."""
        gh = MagicMock(spec=GhOps)

        result = determine_reviewer(
            gh,
            source_pr_number=None,
            commit_author="",
        )
        assert result == DEFAULT_FALLBACK_TEAM


class TestAssignReviewer:
    """Tests for the assign_reviewer function."""

    def test_assigns_reviewer_and_appends_trailer(self) -> None:
        """A review is requested and the assigned trailer is appended to the body."""
        gh = MagicMock(spec=GhOps)
        now = datetime(2025, 3, 21, 10, 30, 0, tzinfo=timezone.utc)

        updated_body = assign_reviewer(
            gh,
            pr_number=42,
            reviewer="alice",
            current_body="PR description here",
            now=now,
        )

        # Verify the review request was made.
        gh.request_reviewer.assert_called_once_with(42, "alice")

        # Verify the body was updated with the trailer.
        gh.update_pr_body.assert_called_once()
        call_args = gh.update_pr_body.call_args
        assert call_args[0][0] == 42
        assert "Repo-Sync-Assigned: alice@2025-03-21T10:30:00Z" in call_args[0][1]

        # Verify the returned body is parseable.
        assigned = parse_assigned(updated_body)
        assert assigned is not None
        assert assigned.username == "alice"
        assert assigned.timestamp == now

    def test_assigns_team_reviewer(self) -> None:
        """Teams (like @oncall-client-primary) can be assigned as reviewers."""
        gh = MagicMock(spec=GhOps)
        now = datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)

        assign_reviewer(
            gh,
            pr_number=99,
            reviewer="oncall-client-primary",
            current_body="body",
            now=now,
        )

        gh.request_reviewer.assert_called_once_with(99, "oncall-client-primary")

    def test_preserves_existing_body(self) -> None:
        """The existing PR body content is preserved when appending the trailer."""
        gh = MagicMock(spec=GhOps)
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        existing_body = "This is the PR description.\n\nRepo-Sync-Origin: org/repo@sha123\n"

        updated = assign_reviewer(
            gh, pr_number=1, reviewer="dave", current_body=existing_body, now=now
        )

        # Both the original content and the new trailer should be present.
        assert "This is the PR description." in updated
        assert "Repo-Sync-Origin: org/repo@sha123" in updated
        assert "Repo-Sync-Assigned: dave@2025-01-01T00:00:00Z" in updated

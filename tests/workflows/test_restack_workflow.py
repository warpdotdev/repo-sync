"""Tests for repo_sync.workflows.restack_workflow."""

from __future__ import annotations

from repo_sync.workflows.restack_workflow import (
    detect_direction_from_branch,
    determine_direction,
    watermark_repo_is_self,
)


class TestDetectDirectionFromBranch:
    """Tests for direction detection from a branch name."""

    def test_private_to_public(self) -> None:
        assert (
            detect_direction_from_branch("repo-sync/private-to-public/abc1234")
            == "private-to-public"
        )

    def test_public_to_private(self) -> None:
        assert (
            detect_direction_from_branch("repo-sync/public-to-private/def5678")
            == "public-to-private"
        )

    def test_non_sync_branch(self) -> None:
        assert detect_direction_from_branch("feature/my-feature") is None

    def test_partial_match(self) -> None:
        # Just "repo-sync/" without a direction segment.
        assert detect_direction_from_branch("repo-sync/something-else") is None

    def test_empty_string(self) -> None:
        assert detect_direction_from_branch("") is None


class TestDetermineDirection:
    """Tests for the full direction determination logic."""

    def test_prefers_branch_detection(self) -> None:
        # Branch says private-to-public, even though source_is_private=False.
        result = determine_direction(
            merged_head_branch="repo-sync/private-to-public/abc1234",
            source_is_private=False,
        )
        assert result == "private-to-public"

    def test_fallback_source_is_private_true(self) -> None:
        # No branch info; source_is_private=True means target receives
        # private-to-public PRs.
        result = determine_direction(
            merged_head_branch=None,
            source_is_private=True,
        )
        assert result == "private-to-public"

    def test_fallback_source_is_private_false(self) -> None:
        result = determine_direction(
            merged_head_branch=None,
            source_is_private=False,
        )
        assert result == "public-to-private"

    def test_empty_branch_string_uses_fallback(self) -> None:
        result = determine_direction(
            merged_head_branch="",
            source_is_private=True,
        )
        assert result == "private-to-public"

    def test_non_sync_branch_uses_fallback(self) -> None:
        result = determine_direction(
            merged_head_branch="feature/unrelated",
            source_is_private=False,
        )
        assert result == "public-to-private"


class TestWatermarkRepoIsSelf:
    """Tests for the watermark-repo-is-self assertion."""

    def test_always_true(self) -> None:
        assert watermark_repo_is_self() is True

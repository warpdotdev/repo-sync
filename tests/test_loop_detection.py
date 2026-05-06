"""Tests for infinite loop prevention.

Covers VALIDATION.md infinite loop prevention cases:
- Sync commit with both trailer AND repo-sync/ branch is skipped.
- Commit with manually-added trailer but NOT from repo-sync/ branch is NOT skipped.
- Commit from repo-sync/ branch but WITHOUT trailer is NOT skipped.
- Commit with no trailer and no matching PR is NOT skipped.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.loop_detection import is_sync_originated
from repo_sync.stack.trailers import format_origin_trailer


class TestIsSyncOriginated:
    """Tests for the is_sync_originated check."""

    def test_sync_commit_with_trailer_and_sync_branch(self) -> None:
        """A commit with both trailer and repo-sync/ branch is sync-originated."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = (
            "repo-sync: sync from private\n\n"
            "Repo-Sync-Origin: warpdotdev/warp-internal@abc123"
        )

        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = PullRequest(
            number=10,
            head_branch="repo-sync/private-to-public/abc123",
            base_branch="main",
            title="sync",
            body="",
            url="https://github.com/org/repo/pull/10",
            state="MERGED",
            merged=True,
        )

        assert is_sync_originated(git, gh, "abc123") is True

    def test_spoofed_trailer_not_from_sync_branch(self) -> None:
        """A commit with a manually-added trailer but not from a repo-sync/ branch is NOT skipped."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = (
            "my regular commit\n\n"
            "Repo-Sync-Origin: warpdotdev/warp-internal@spoofed"
        )

        gh = MagicMock(spec=GhOps)
        # PR exists but its branch doesn't match repo-sync/.
        gh.get_pr_for_commit.return_value = PullRequest(
            number=20,
            head_branch="feature/my-feature",
            base_branch="main",
            title="my feature",
            body="",
            url="https://github.com/org/repo/pull/20",
            state="MERGED",
            merged=True,
        )

        assert is_sync_originated(git, gh, "spoofed") is False

    def test_sync_branch_without_trailer(self) -> None:
        """A commit from a repo-sync/ branch but without a trailer is NOT skipped."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = "some commit without trailers"

        gh = MagicMock(spec=GhOps)
        # gh.get_pr_for_commit should not even be called because the trailer
        # check fails first.

        assert is_sync_originated(git, gh, "notrailer") is False
        gh.get_pr_for_commit.assert_not_called()

    def test_no_trailer_no_pr(self) -> None:
        """A regular commit with no trailer and no matching PR is NOT skipped."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = "regular commit"

        gh = MagicMock(spec=GhOps)

        assert is_sync_originated(git, gh, "regular") is False

    def test_trailer_present_but_no_pr_found(self) -> None:
        """A commit with a trailer but no PR found (direct push with spoofed trailer) is NOT skipped."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = (
            "direct push\n\n"
            "Repo-Sync-Origin: org/repo@directpush"
        )

        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = None

        assert is_sync_originated(git, gh, "directpush") is False

    def test_api_failure_propagates_when_trailer_present(self) -> None:
        """An API failure during PR lookup aborts rather than failing-open."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = (
            "sync commit\n\n"
            "Repo-Sync-Origin: warpdotdev/warp-internal@abc123"
        )

        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api", "repos/org/repo/commits/abc123/pulls"]
        )

        with pytest.raises(subprocess.CalledProcessError):
            is_sync_originated(git, gh, "abc123")

    def test_public_to_private_sync_branch_recognized(self) -> None:
        """Public-to-private sync branches are also recognized."""
        git = MagicMock(spec=GitOps)
        git.commit_message.return_value = (
            "sync from public\n\n"
            "Repo-Sync-Origin: warpdotdev/warp-public@pub123"
        )

        gh = MagicMock(spec=GhOps)
        gh.get_pr_for_commit.return_value = PullRequest(
            number=30,
            head_branch="repo-sync/public-to-private/pub123",
            base_branch="main",
            title="sync",
            body="",
            url="https://github.com/org/repo/pull/30",
            state="MERGED",
            merged=True,
        )

        assert is_sync_originated(git, gh, "pub123") is True

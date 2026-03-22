"""Tests for PR management: creation, auto-merge, and descriptions.

Covers VALIDATION.md auto-merge cases:
- Bottom PR in stack (base = default branch) has auto-merge enabled.
- PRs deeper in the stack do NOT have auto-merge enabled.
- Conflict-resolved PRs never get auto-merge.
- PR body includes the Repo-Sync-Origin trailer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.prs import (
    create_sync_pr,
    set_auto_merge,
    should_enable_auto_merge,
)
from repo_sync.stack.trailers import parse_origin


class TestShouldEnableAutoMerge:
    """Tests for auto-merge eligibility check."""

    def test_bottom_of_stack_gets_auto_merge(self) -> None:
        """When base = default branch, auto-merge should be enabled."""
        assert should_enable_auto_merge("main", "main") is True

    def test_stacked_pr_does_not_get_auto_merge(self) -> None:
        """When base != default branch, auto-merge should NOT be enabled."""
        assert (
            should_enable_auto_merge(
                "repo-sync/private-to-public/aaa", "main"
            )
            is False
        )


class TestSetAutoMerge:
    """Tests for set_auto_merge behavior."""

    def test_enables_on_bottom(self) -> None:
        """Auto-merge is enabled when PR is at the bottom of the stack."""
        gh = MagicMock(spec=GhOps)
        set_auto_merge(gh, pr_number=1, base_branch="main", default_branch="main")
        gh.enable_auto_merge.assert_called_once_with(1)

    def test_does_not_enable_when_not_bottom(self) -> None:
        """Auto-merge is not enabled when PR is not at the bottom."""
        gh = MagicMock(spec=GhOps)
        set_auto_merge(
            gh,
            pr_number=2,
            base_branch="repo-sync/p2p/aaa",
            default_branch="main",
        )
        gh.enable_auto_merge.assert_not_called()

    def test_never_enables_on_conflict_resolved(self) -> None:
        """Even at the bottom of the stack, conflict-resolved PRs don't get auto-merge."""
        gh = MagicMock(spec=GhOps)
        set_auto_merge(
            gh,
            pr_number=1,
            base_branch="main",
            default_branch="main",
            conflict_resolved=True,
        )
        gh.enable_auto_merge.assert_not_called()


class TestCreateSyncPr:
    """Tests for create_sync_pr."""

    def test_creates_pr_with_origin_trailer(self) -> None:
        """The created PR body includes the Repo-Sync-Origin trailer."""
        gh = MagicMock(spec=GhOps)
        gh.create_pr.return_value = PullRequest(
            number=1,
            head_branch="repo-sync/p2p/abc",
            base_branch="main",
            title="sync",
            body="desc\n\nRepo-Sync-Origin: org/repo@abc123\n",
            url="https://github.com/org/repo/pull/1",
            state="OPEN",
        )

        pr = create_sync_pr(
            gh,
            head_branch="repo-sync/p2p/abc",
            base_branch="main",
            title="sync",
            body="desc",
            source_repo="org/repo",
            source_sha="abc123",
            default_branch="main",
        )

        # Verify create_pr was called with body containing trailer.
        create_args = gh.create_pr.call_args
        assert "Repo-Sync-Origin: org/repo@abc123" in create_args.kwargs["body"]

    def test_auto_merge_on_bottom_pr(self) -> None:
        """Auto-merge is enabled when the PR's base is the default branch."""
        gh = MagicMock(spec=GhOps)
        gh.create_pr.return_value = PullRequest(
            number=5,
            head_branch="repo-sync/p2p/abc",
            base_branch="main",
            title="sync",
            body="",
            url="https://github.com/org/repo/pull/5",
            state="OPEN",
        )

        create_sync_pr(
            gh,
            head_branch="repo-sync/p2p/abc",
            base_branch="main",
            title="sync",
            body="desc",
            source_repo="org/repo",
            source_sha="abc123",
            default_branch="main",
        )

        gh.enable_auto_merge.assert_called_once_with(5)

    def test_no_auto_merge_on_stacked_pr(self) -> None:
        """Auto-merge is NOT enabled when the PR is stacked (base != default)."""
        gh = MagicMock(spec=GhOps)
        gh.create_pr.return_value = PullRequest(
            number=6,
            head_branch="repo-sync/p2p/bbb",
            base_branch="repo-sync/p2p/aaa",
            title="sync",
            body="",
            url="https://github.com/org/repo/pull/6",
            state="OPEN",
        )

        create_sync_pr(
            gh,
            head_branch="repo-sync/p2p/bbb",
            base_branch="repo-sync/p2p/aaa",
            title="sync",
            body="desc",
            source_repo="org/repo",
            source_sha="bbb123",
            default_branch="main",
        )

        gh.enable_auto_merge.assert_not_called()

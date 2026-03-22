"""Tests for restack logic after squash merge.

Covers VALIDATION.md restacking cases:
- git rebase --onto correctly avoids duplicate-change conflicts.
- PR base branch is updated to the default branch.
- Auto-merge is enabled after successful restack.
- Conflicts are detected and reported.
- Conflict-resolved PRs do NOT get auto-merge.
- 3+ PR stack restacking (only next PR is touched, rest stays).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call

import pytest

from repo_sync.stack.gh_ops import GhOps
from repo_sync.stack.git_ops import GitOps
from repo_sync.stack.restack import (
    RestackOutcome,
    RestackResult,
    restack_after_conflict_resolution,
    restack_pr,
)
from tests.conftest import make_commit


class TestRestackPr:
    """Tests for the restack_pr function using real git repos."""

    def test_successful_restack_after_squash_merge(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """After squash-merge of PR A, PR B rebases cleanly onto main.

        Simulates:
        1. main has initial commit.
        2. Branch A is created on main with commit A.
        3. Branch B is stacked on A with commit B.
        4. A "squash merge" is simulated on main (new commit with A's changes).
        5. B is restacked onto main using --onto to skip A's commit.
        """
        source_git, _ = tmp_git_repo_pair
        gh = MagicMock(spec=GhOps)

        # Step 1-2: Create branch A from main with a commit.
        source_git.create_branch("sync/aaa", "main")
        sha_a = make_commit(source_git, "a.txt", "content-a", "commit A")
        tip_a = source_git.rev_parse("HEAD")

        # Step 3: Create branch B stacked on A with a commit.
        source_git.create_branch("sync/bbb", "sync/aaa")
        sha_b = make_commit(source_git, "b.txt", "content-b", "commit B")

        # Step 4: Simulate squash-merge on main (new commit with same changes as A).
        source_git.checkout("main")
        with open(os.path.join(source_git.repo_dir, "a.txt"), "w") as f:
            f.write("content-a")
        source_git._run(["add", "a.txt"])
        source_git._run(["commit", "-m", "squash merge A"])

        # Step 5: Restack B onto main.
        outcome = restack_pr(
            git=source_git,
            gh=gh,
            next_pr_branch="sync/bbb",
            merged_pr_branch_tip=tip_a,
            default_branch="main",
            next_pr_number=2,
            remote="origin",
        )

        assert outcome.result == RestackResult.SUCCESS
        assert outcome.conflicting_files is None

        # Verify B's branch now only contains B's changes on top of main.
        assert source_git.current_branch() == "sync/bbb"
        assert os.path.exists(os.path.join(source_git.repo_dir, "a.txt"))
        assert os.path.exists(os.path.join(source_git.repo_dir, "b.txt"))

        # Verify PR base was updated and auto-merge was enabled.
        gh.update_pr_base.assert_called_once_with(2, "main")
        gh.enable_auto_merge.assert_called_once_with(2)

    def test_restack_with_conflict(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """Restack detects conflicts and returns CONFLICT result."""
        source_git, _ = tmp_git_repo_pair
        gh = MagicMock(spec=GhOps)

        # Create branch A with a change to shared.txt.
        source_git.create_branch("sync/aaa", "main")
        sha_a = make_commit(source_git, "shared.txt", "version-a", "commit A")
        tip_a = source_git.rev_parse("HEAD")

        # Create branch B that also modifies shared.txt.
        source_git.create_branch("sync/bbb", "sync/aaa")
        make_commit(source_git, "shared.txt", "version-b", "commit B")

        # Simulate squash-merge on main with a DIFFERENT change to shared.txt.
        source_git.checkout("main")
        make_commit(source_git, "shared.txt", "version-conflict", "squash merge A")

        # Restack B onto main -- should conflict.
        outcome = restack_pr(
            git=source_git,
            gh=gh,
            next_pr_branch="sync/bbb",
            merged_pr_branch_tip=tip_a,
            default_branch="main",
            next_pr_number=2,
            remote="origin",
        )

        assert outcome.result == RestackResult.CONFLICT
        assert outcome.conflicting_files is not None
        assert "shared.txt" in outcome.conflicting_files
        # Auto-merge and base update should NOT be called on conflict.
        gh.update_pr_base.assert_not_called()
        gh.enable_auto_merge.assert_not_called()

    def test_three_pr_stack_only_next_is_restacked(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """In a stack A -> B -> C, restacking after A merges only touches B.

        C remains based on B's branch and is not modified.
        """
        source_git, _ = tmp_git_repo_pair
        gh = MagicMock(spec=GhOps)

        # Branch A.
        source_git.create_branch("sync/aaa", "main")
        make_commit(source_git, "a.txt", "a", "commit A")
        tip_a = source_git.rev_parse("HEAD")

        # Branch B on A.
        source_git.create_branch("sync/bbb", "sync/aaa")
        make_commit(source_git, "b.txt", "b", "commit B")
        tip_b_before = source_git.rev_parse("HEAD")

        # Branch C on B.
        source_git.create_branch("sync/ccc", "sync/bbb")
        make_commit(source_git, "c.txt", "c", "commit C")
        tip_c = source_git.rev_parse("HEAD")

        # Squash-merge A on main.
        source_git.checkout("main")
        make_commit(source_git, "a.txt", "a", "squash merge A")

        # Restack B (not C).
        outcome = restack_pr(
            git=source_git,
            gh=gh,
            next_pr_branch="sync/bbb",
            merged_pr_branch_tip=tip_a,
            default_branch="main",
            next_pr_number=2,
            remote="origin",
        )

        assert outcome.result == RestackResult.SUCCESS

        # C's SHA should be unchanged (we didn't touch it).
        c_sha = source_git.rev_parse("sync/ccc")
        assert c_sha == tip_c


class TestRestackAfterConflictResolution:
    """Tests for post-conflict-resolution metadata updates."""

    def test_no_auto_merge_after_conflict_resolution(self) -> None:
        """Conflict-resolved PRs get base updated but NOT auto-merge enabled."""
        git = MagicMock(spec=GitOps)
        gh = MagicMock(spec=GhOps)

        restack_after_conflict_resolution(
            git=git,
            gh=gh,
            pr_branch="sync/bbb",
            pr_number=2,
            default_branch="main",
            remote="origin",
        )

        # Base should be updated.
        gh.update_pr_base.assert_called_once_with(2, "main")
        # Auto-merge should NOT be enabled.
        gh.enable_auto_merge.assert_not_called()
        # Branch should be force-pushed.
        git.push.assert_called_once_with("origin", "sync/bbb", force=True)

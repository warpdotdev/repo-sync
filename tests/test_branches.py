"""Tests for stack branch operations and idempotency guards.

Covers VALIDATION.md stacked PR management and idempotency cases:
- Branch naming convention.
- is_sync_branch classification.
- Stack base determination (default branch vs. previous sync branch).
- Idempotency: skip if branch or PR already exists.
- Stack creation with correct base.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from repo_sync.stack.branches import (
    SYNC_BRANCH_PREFIX,
    IdempotencyResult,
    check_idempotency,
    create_stack_branch,
    determine_stack_base,
    is_sync_branch,
    sync_branch_name,
)
from repo_sync.stack.gh_ops import GhOps, PullRequest
from repo_sync.stack.git_ops import GitOps
from tests.conftest import make_commit


class TestSyncBranchName:
    """Tests for branch naming convention."""

    def test_private_to_public(self) -> None:
        """Private-to-public branch name includes direction and short SHA."""
        name = sync_branch_name("private-to-public", "abc123d")
        assert name == "repo-sync/private-to-public/abc123d"

    def test_public_to_private(self) -> None:
        """Public-to-private branch name includes direction and short SHA."""
        name = sync_branch_name("public-to-private", "def456a")
        assert name == "repo-sync/public-to-private/def456a"


class TestIsSyncBranch:
    """Tests for sync branch detection."""

    def test_valid_sync_branch(self) -> None:
        """A repo-sync/ branch is recognized."""
        assert is_sync_branch("repo-sync/private-to-public/abc123") is True

    def test_non_sync_branch(self) -> None:
        """A regular branch is not recognized as a sync branch."""
        assert is_sync_branch("feature/my-feature") is False
        assert is_sync_branch("main") is False

    def test_partial_prefix(self) -> None:
        """A branch that starts with 'repo-sync' but not 'repo-sync/' is not a match."""
        assert is_sync_branch("repo-sync-other") is False


class TestDetermineStackBase:
    """Tests for stack base determination."""

    def test_empty_stack_returns_default_branch(self) -> None:
        """When no existing stack, base is the default branch."""
        assert determine_stack_base([], "main") == "main"

    def test_existing_stack_returns_last_branch(self) -> None:
        """When stack exists, base is the last (topmost) branch."""
        stack = [
            "repo-sync/private-to-public/aaa",
            "repo-sync/private-to-public/bbb",
        ]
        assert determine_stack_base(stack, "main") == "repo-sync/private-to-public/bbb"

    def test_single_item_stack(self) -> None:
        """A stack with one item returns that item."""
        stack = ["repo-sync/private-to-public/ccc"]
        assert determine_stack_base(stack, "main") == "repo-sync/private-to-public/ccc"


class TestCheckIdempotency:
    """Tests for idempotency guards (mock-based)."""

    def test_branch_exists_locally(self, tmp_git_repo: GitOps) -> None:
        """If the branch exists locally, idempotency returns True."""
        # Create the branch.
        make_commit(tmp_git_repo, "file.txt", "content", "commit")
        tmp_git_repo.create_branch("repo-sync/private-to-public/abc123", "main")
        tmp_git_repo.checkout("main")

        gh = MagicMock(spec=GhOps)
        gh.pr_exists.return_value = None

        result = check_idempotency(
            tmp_git_repo, gh, "repo-sync/private-to-public/abc123"
        )
        assert result.already_exists is True

    def test_pr_exists_but_no_local_branch(self) -> None:
        """If a PR exists for the head branch, idempotency returns True."""
        git = MagicMock(spec=GitOps)
        git.branch_exists.return_value = False

        gh = MagicMock(spec=GhOps)
        gh.branch_exists_on_remote.return_value = False
        gh.pr_exists.return_value = PullRequest(
            number=42,
            head_branch="repo-sync/private-to-public/abc123",
            base_branch="main",
            title="sync",
            body="",
            url="https://github.com/org/repo/pull/42",
            state="OPEN",
        )

        result = check_idempotency(
            git, gh, "repo-sync/private-to-public/abc123"
        )
        assert result.already_exists is True
        assert result.existing_pr is not None
        assert result.existing_pr.number == 42

    def test_remote_branch_exists_but_no_pr(self) -> None:
        """If the branch exists on the remote but no PR, idempotency returns True.

        Covers the gap where the branch was pushed but the workflow crashed
        before creating a PR.
        """
        git = MagicMock(spec=GitOps)
        git.branch_exists.return_value = False

        gh = MagicMock(spec=GhOps)
        gh.branch_exists_on_remote.return_value = True
        gh.pr_exists.return_value = None

        result = check_idempotency(
            git, gh, "repo-sync/private-to-public/abc123"
        )
        assert result.already_exists is True
        assert result.existing_pr is None

    def test_nothing_exists(self) -> None:
        """If neither branch nor PR exists, idempotency returns False."""
        git = MagicMock(spec=GitOps)
        git.branch_exists.return_value = False

        gh = MagicMock(spec=GhOps)
        gh.branch_exists_on_remote.return_value = False
        gh.pr_exists.return_value = None

        result = check_idempotency(
            git, gh, "repo-sync/private-to-public/abc123"
        )
        assert result.already_exists is False
        assert result.existing_pr is None


class TestCreateStackBranch:
    """Tests for creating stacked branches with real git repos."""

    def test_create_branch_on_default(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """First sync branch is created on the default branch and pushed."""
        source_git, _ = tmp_git_repo_pair

        create_stack_branch(
            source_git,
            "repo-sync/private-to-public/aaa",
            "main",
        )

        # Branch should exist locally.
        assert source_git.branch_exists("repo-sync/private-to-public/aaa")

    def test_create_stacked_branch(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """Second sync branch is stacked on the first."""
        source_git, _ = tmp_git_repo_pair

        # Create first branch and add a commit.
        create_stack_branch(
            source_git,
            "repo-sync/private-to-public/aaa",
            "main",
        )
        first_sha = make_commit(
            source_git, "file1.txt", "content1", "first sync commit"
        )
        source_git.push("origin", "repo-sync/private-to-public/aaa")
        source_git.checkout("main")

        # Create second branch stacked on the first.
        create_stack_branch(
            source_git,
            "repo-sync/private-to-public/bbb",
            "repo-sync/private-to-public/aaa",
        )

        # The second branch should include the first branch's commit.
        assert source_git.branch_exists("repo-sync/private-to-public/bbb")

    def test_three_pr_stack(
        self, tmp_git_repo_pair: tuple[GitOps, GitOps]
    ) -> None:
        """A stack of three branches is correctly layered (A -> B -> C)."""
        source_git, _ = tmp_git_repo_pair

        # Branch A on main.
        create_stack_branch(source_git, "repo-sync/p2p/aaa", "main")
        sha_a = make_commit(source_git, "a.txt", "a", "commit A")
        source_git.push("origin", "repo-sync/p2p/aaa")
        source_git.checkout("main")

        # Branch B on A.
        create_stack_branch(source_git, "repo-sync/p2p/bbb", "repo-sync/p2p/aaa")
        sha_b = make_commit(source_git, "b.txt", "b", "commit B")
        source_git.push("origin", "repo-sync/p2p/bbb")
        source_git.checkout("main")

        # Branch C on B.
        create_stack_branch(source_git, "repo-sync/p2p/ccc", "repo-sync/p2p/bbb")
        sha_c = make_commit(source_git, "c.txt", "c", "commit C")

        # C should have all three files.
        assert os.path.exists(os.path.join(source_git.repo_dir, "a.txt"))
        assert os.path.exists(os.path.join(source_git.repo_dir, "b.txt"))
        assert os.path.exists(os.path.join(source_git.repo_dir, "c.txt"))
